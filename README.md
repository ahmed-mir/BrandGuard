# BrandGuard

BrandGuard is a source-backed review and approval system for marketing content. Phases 0, 1A, 1B, 2A and 2B provide the public introduction, Django session authentication, role-aware workspace shell, account management, workspace-scoped source authoring, and human-verified TXT/DOCX/text-PDF ingestion into the same versioned policy records. Brand source approval publishes policy only; it does not approve marketing content or run a review.

## Requirements

- Python 3.12 or newer.
- Django 6.1.x (constrained in `requirements.txt`).
- SQLite by default. PostgreSQL is supported through `DATABASE_URL` and the included psycopg driver.
- `python-docx` and `pypdf` for bounded DOCX/PDF text extraction.

## Windows PowerShell setup

Run from the repository directory. Create the local virtual environment once:

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Set a stable development secret in your per-user Windows environment. The first block creates it once without printing or storing it in the repository; keep the value private.

```powershell
if (-not [Environment]::GetEnvironmentVariable('DJANGO_SECRET_KEY', 'User')) {
    $localSecret = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    [Environment]::SetEnvironmentVariable('DJANGO_SECRET_KEY', $localSecret, 'User')
    Remove-Variable localSecret
}
$env:DJANGO_SECRET_KEY = [Environment]::GetEnvironmentVariable('DJANGO_SECRET_KEY', 'User')
$env:DJANGO_DEBUG = 'true'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
```

The secret persists across terminal and server restarts. Open a new PowerShell session and repeat the environment assignments when needed. `.env.example` lists supported settings, but is not automatically loaded. Never commit a secret or put it in a README command.

## Database and local demo accounts

Apply migrations, then create the four synthetic demo accounts:

```powershell
python manage.py migrate
python manage.py seed_demo
```

The seed command also installs the synthetic LumaDesk policy sources used to exercise the source library. Imported documents are not required for setup.

`seed_demo` only runs with `DJANGO_DEBUG=true` and SQLite. On the first run it generates a separate high-entropy password for each account and prints each password once. Capture them securely; the command does not store or show generated passwords again. For repeatable passwords, set `BRANDGUARD_DEMO_PASSWORD` or a role-specific variable such as `BRANDGUARD_DEMO_REVIEWER_PASSWORD` to a strong value before the first seed. The command never prints environment-supplied passwords and never resets existing passwords. Do not use demo seeding in production.

| Username | Email | Workspace role |
| --- | --- | --- |
| `lumadesk_creator` | `creator@lumadesk.test` | Creator |
| `lumadesk_brand_manager` | `brand.manager@lumadesk.test` | Brand manager |
| `lumadesk_reviewer` | `reviewer@lumadesk.test` | Reviewer |
| `lumadesk_admin` | `admin@lumadesk.test` | Workspace admin |

Sign in at `/accounts/login/` with a username or case-insensitive email and the password captured/configured during seeding. Use the **Sign out** form in the workspace to log out; it submits POST with CSRF protection. There is no public registration.

If this database already contains seeded accounts and you no longer have their one-time generated passwords, set a local password interactively with Django's management command, for example `python manage.py changepassword lumadesk_admin`. Repeat for another demo username as needed. `seed_demo` deliberately leaves existing passwords unchanged and cannot recover them.

Start the server:

```powershell
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. To test the full local path, visit `/accounts/login/`, sign in with one of the seeded accounts, open `/app/`, then use **Sign out** and confirm `/app/` sends you back to login.

## Routes

- `/` — public product landing page.
- `/health/` — process health response (does not check the database).
- `/accounts/login/` — Django session login by username or email.
- `/accounts/logout/` — POST-only session logout.
- `/app/` — authenticated, role-aware workspace overview. Requires exactly one active membership in an active workspace.
- `/app/settings/profile/` — authenticated account settings. A user may edit first and last name and change their password; username, email, workspace and role are read-only.
- `/app/members/` — read-only memberships for the current workspace. Workspace admins only; all other roles receive 403.
- `/app/brand-sources/` — source library. Every role can view published sources; brand managers and workspace admins also see drafts and history.
- `/app/brand-sources/new/` — create a source and its first draft (managers/admins).
- `/app/brand-sources/<source-id>/` — source details and version history, restricted to the current workspace.
- `/app/brand-sources/<source-id>/versions/new/` — create or resume a draft version (managers/admins).
- `/app/brand-sources/<source-id>/versions/<version>/` — view a source version and its policy/evidence.
- `/app/brand-sources/<source-id>/versions/<version>/edit/` — edit a draft source text, rules and facts (managers/admins).
- `/app/brand-sources/import/` — upload a new TXT, DOCX or text-based PDF as a private, unconfirmed source draft (managers/admins).
- `/app/brand-sources/<source-id>/versions/<version>/confirm-import/` — POST-only explicit confirmation of the currently saved imported text; editing the text makes the confirmation stale until repeated.
- Nested `/rules/` and `/facts/` routes — manage entries on drafts; publishing uses a CSRF-protected POST at `/approve/` (managers/admins).

The import accepts UTF-8/UTF-8 BOM TXT, DOCX and text PDFs up to 5 MiB; PDFs are limited to 10 pages and extracted text to 80,000 characters by default. The parser checks both extension and file structure, rejects encrypted PDFs, corrupted files and documents without readable text. DOCX archive entry count, decompressed size and compression ratios are bounded. Scanned PDFs are not OCR'd; the form directs users to enter their authoritative text manually. The source version created by import is the same `SourceVersion` queried by policy snapshots and the future review engine.

Extracted text is saved as an ordinary editable draft. A manager/admin must inspect it, save corrections, then submit the separate confirmation action. The approval service verifies that the confirmed SHA-256 matches the current source text, then runs the existing evidence validation and atomic policy publication. Import never creates rules/facts automatically and never approves a source by itself.

Original files are stored with generated names in a `private_documents` storage alias. Local development defaults to `private_uploads/` outside `MEDIA_URL`, static serving and public download routes; it is ignored by Git. There is no file download endpoint. The default local filesystem backend is private only from application URL routing; deployment operators must configure access controls, encryption, backups and retention for the chosen storage. For a private object storage backend, set `PRIVATE_DOCUMENT_STORAGE_BACKEND` to the installed backend import path and `PRIVATE_DOCUMENT_STORAGE_OPTIONS` to its JSON options. The backend must implement Django Storage APIs. Do not configure a public bucket or expose this location as public media.

Limits can be lowered with `BRAND_DOCUMENT_MAX_UPLOAD_BYTES`, `BRAND_DOCUMENT_MAX_PDF_PAGES` and `BRAND_DOCUMENT_MAX_EXTRACTED_CHARS`; settings reject values above 5 MiB, 10 pages and 80,000 characters respectively. Set `PRIVATE_DOCUMENT_ROOT` to choose the local private file location. None of these values contain secrets. `.env.example` documents the variable names but is not loaded automatically.

## Configuration and PostgreSQL

SQLite is used when `DATABASE_URL` is unset. PostgreSQL can be selected with an environment URL; create the database and account separately:

```powershell
$env:DATABASE_URL = 'postgresql://brandguard@localhost:5432/brandguard'
```

Replace these example connection components in your local environment, and keep credentials out of version control. `DB_SSLMODE` can set the PostgreSQL TLS mode. See `.env.example` for the setting names.

## Checks and tests

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py migrate
python manage.py test
```

The test suite covers Phase 0 landing/health routes, Phase 1A authentication and membership security, Phase 1B profile/password/navigation/member safeguards, Phase 2A source/rule/fact CRUD, exact evidence, role/workspace isolation, atomic publication, immutability, supersession, revision/hash snapshots and idempotent demo data, plus Phase 2B synthetic TXT/DOCX/PDF extraction, provenance, limits, permissions, confirmation and policy integration. `python manage.py collectstatic --noinput` can be used to verify/build deployment static assets; generated `staticfiles/` output is ignored.

## Architecture and limits

- `config/` — environment-backed settings, middleware, and named root routes.
- `core/` — public landing/health pages, reusable responsive application shell, local styles and accessible mobile-navigation script, plus the authenticated overview template.
- `accounts/` — a Django `ModelBackend` subclass, login and settings forms/views, and POST-only logout. Password hashes and session behavior use Django. The login identifier is an exact username or case-insensitive email; when an identifier ambiguously matches one account's username and another account's email, authentication is rejected. Django's `LoginView` validates `next` against the current host/scheme. Account changes are limited to first and last name plus Django-validated password changes.
- `workspaces/` — workspace and membership models, one-membership-per-user/workspace database constraint, server-side membership/role decorators, shared app page context, the role-aware `/app/` overview, the admin-only workspace member view, and a local-only `seed_demo` management command.
- `brand_sources/` — workspace-scoped `BrandSource`, immutable `SourceVersion`, structured `BrandRule` matcher schemas, `ApprovedFact` and `BrandDocumentImport`. Services own bounded extraction, private file persistence, hash provenance, explicit text confirmation, draft cloning, atomic approval/supersession, active-version queries and policy snapshots. Imported text uses the exact same `SourceVersion` and evidence records as manual authoring. Rule/fact evidence must occur verbatim in the source version; offsets are checked. Matcher types use an allowlisted schema, never executable expressions.
- `docs/` — phase plan and design system.

The overview displays the signed-in identity, active workspace and membership role, the real workspace and membership status/dates, and role-specific guidance. It does not query or display review counts, submissions, analytics, notifications, compliance scores, or fabricated dashboard records. Content review activity is explicitly marked unavailable because review records are not implemented. Role-specific future navigation items are noninteractive and marked unavailable. Workspace admins can inspect actual membership records scoped to their current workspace; inactive membership records are shown as inactive. Other roles cannot access the member route, even by direct URL. The profile form only saves first and last name. Django's built-in `PasswordChangeForm` validates password changes, and the current session is updated with `update_session_auth_hash`. Profile and password changes use POST with CSRF protection. Role decisions are resolved from database membership, never client fields. A user with no active membership, an inactive workspace, or ambiguous multiple active memberships is denied workspace access.

Content submissions, execution of rules against marketing copy, automated/general factual verification, creator revisions, reviewer content-approval decisions, audit-event subsystem, AI integration, OCR, image/poster analysis, exports, public registration, and workspace/member modification remain deferred. Brand-source publishing is not a content review or content approval. Imported original documents have no download UI/API; no parser can interpret scanned-image text. Effective dates are stored but expiration-window enforcement is not included. Account settings cannot change email, username or role. Workspace administration remains limited to the existing read-only member list plus source-management ability for admins. The existing Django user model is retained.

### Phase 2A permissions and policy snapshots

| Role | View approved sources/evidence | View draft/superseded versions | Create/edit draft policy | Publish source versions |
| --- | --- | --- | --- | --- |
| Creator | Yes | No | No | No |
| Reviewer | Yes | No | No | No |
| Brand manager | Yes | Yes | Yes | Yes |
| Workspace admin | Yes | Yes | Yes | Yes |

Authorization is server-side and every source/version/evidence query is scoped to the active workspace membership. Publishing validates every rule/fact excerpt against the full source text inside one transaction, records approver/time, supersedes the prior source version, increments `Workspace.policy_revision` and recomputes `Workspace.policy_snapshot_hash`. Historical approved versions and evidence remain stored.

For a future content review, persist `policy_snapshot(workspace)`'s `revision`, `hash` and exact `versions` (source ID, version ID/number and content hash) with the review when it starts. Resolve policy entries against those version IDs to reproduce evidence later; compare the saved hash/revision against the current workspace snapshot only to detect changed policy. `active_policy_versions(workspace)` queries the current approved set. Phase 2A does not create review records or decide what to do when policy changes during a review.

`seed_demo` adds four synthetic LumaDesk sources: product naming with `TERM-001` (prohibited “Luma Desk”, preferred “LumaDesk”) and `TERM-002` (required “LumaDesk”); voice guidance; `DISC-001` (required “Results vary.”); and fact sheet `FACT-02` (fictional “Up to 20% faster task completion in our internal pilot; results vary.”, represented as `UP_TO`, `20`, `%`). Structured entries link exact source excerpts. The statement is fictional and not independently verified.

## PRD note

The requested path `docs/BrandGuard_Round2_PRD.pdf` is absent. The available source was `BrandGuard_Round2_Product_Requirements_Document.pdf` in the repository root and was read with `pypdf`. The PRD calls for text-based document extraction and states a 5 MB upload limit, a 10-page PDF limit, and rejection of encrypted/oversize files; scanned-PDF understanding is out of scope. Phase 2B implements bounded text extraction with human verification and private provenance. It does not add OCR, AI, content review or approval. See `docs/PHASES.md` for phase boundaries.
