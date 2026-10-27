# BrandGuard

BrandGuard is a source-backed review and approval system for marketing content. The product includes Django session authentication, role-aware workspaces, source authoring and human-verified TXT/DOCX/text-PDF ingestion, deterministic evidence-backed reviews, immutable content revisions, human approval of exact reviewed versions, and TXT/PDF exports guarded by current approval. Optional real AI advisory analysis is disabled by default and separate from deterministic findings and human decisions. Demo AI mode uses repository-controlled simulated output and makes no external provider request. Brand source approval publishes policy only; it does not approve marketing content.

## Requirements

- Python 3.14.6 is pinned for Render in `.python-version`; the existing local development environment is also Python 3.14.6. Check the selected interpreter with `python --version` rather than assuming the local and hosted runtimes match.
- Django 6.1.x (constrained in `requirements.txt`).
- SQLite by default for local development. Production/Render requires PostgreSQL through `DATABASE_URL` and the included psycopg 3 driver; missing or SQLite production URLs fail startup.
- `python-docx` and `pypdf` for bounded DOCX/PDF text extraction.
- `reportlab` for in-memory, paginated PDF review reports.
- `openai` Python SDK for optional real-provider advisory analysis through the Responses API. Real AI is disabled by default; Demo AI mode does not require the SDK key and makes no provider call.
- Gunicorn for production WSGI, WhiteNoise for static files, and `django-storages[s3]` for optional private S3-compatible brand-document storage.

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

The seed command also installs the synthetic LumaDesk policy sources used to exercise the source library. Imported documents are not required for setup. To create six seed-owned submissions and run the real deterministic engine against them, run `python manage.py seed_review_scenarios` after `seed_demo`. It is restricted to DEBUG + SQLite and skips existing scenario records.

`seed_demo` only runs with `DJANGO_DEBUG=true` and SQLite. On the first run it generates a separate high-entropy password for each account and prints each password once. Capture them securely; the command does not store or show generated passwords again. For repeatable passwords, set `BRANDGUARD_DEMO_PASSWORD` or a role-specific variable such as `BRANDGUARD_DEMO_REVIEWER_PASSWORD` to a strong value before the first seed. The command never prints environment-supplied passwords and never resets existing passwords. Do not use demo seeding in production.

| Username | Email | Workspace role |
| --- | --- | --- |
| `lumadesk_creator` | `creator@lumadesk.test` | Creator |
| `lumadesk_brand_manager` | `brand.manager@lumadesk.test` | Brand manager |
| `lumadesk_reviewer` | `reviewer@lumadesk.test` | Reviewer |
| `lumadesk_admin` | `admin@lumadesk.test` | Workspace admin |

Sign in at `/accounts/login/` with a username or case-insensitive email and the password captured/configured during seeding. Use the **Sign out** form in the workspace to log out; it submits POST with CSRF protection. There is no public registration.

If this database already contains seeded accounts and you no longer have their one-time generated passwords, set a local password interactively with Django's management command, for example `python manage.py changepassword lumadesk_admin`. Repeat for another demo username as needed. `seed_demo` deliberately leaves existing passwords unchanged and cannot recover them.

For a deployed judge workspace, use the separately guarded `bootstrap_demo` procedure in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Provide each judge a role-appropriate account through a private channel; never publish passwords in the repository, application UI, or submission materials. Demonstrate the creator and reviewer as separate sign-ins.

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
- `/app/reviews/` — submission list. Creators see their own submissions; reviewers, brand managers and workspace admins see submissions in the current workspace.
- `/app/reviews/new/` — creator-only plain-text submission form. Saving creates immutable content version 1.
- `/app/reviews/<submission-id>/` and `/app/reviews/<submission-id>/versions/<version>/` — workspace-scoped submission and version details, including saved review history.
- `/app/reviews/<submission-id>/revise/` — creator-only revision form that appends a new immutable version from the current version; stale-base edits are rejected.
- `/app/reviews/<submission-id>/compare/?from=<n>&to=<n>` — escaped line comparison of two stored versions.
- `/app/reviews/<submission-id>/submit/` — creator-only CSRF-protected submission for human approval after a fresh complete review.
- `/app/reviews/<submission-id>/versions/<version>/run/` — creator-only, CSRF-protected POST which runs deterministic checks against the approved policy snapshot captured at start.
- `/app/reviews/<submission-id>/versions/<version>/runs/<review-id>/` — persisted checks and findings with the exact policy identity and source evidence. Reviewers, brand managers and workspace admins may inspect workspace results; creators may inspect only their own.
- `/app/reviews/<submission-id>/versions/<version>/ai/analyze/` - creator-only, CSRF-protected POST. Real mode requires external-processing consent; Demo AI mode requires confirmation that the output is simulated. Findings are saved separately after evidence validation.
- `/app/reviews/<submission-id>/export/txt/` — UTF-8 attachment containing the exact current approved content; every request revalidates approval.
- `/app/reviews/<submission-id>/export/pdf/` — private in-memory attachment containing exact content, approval and review identities, actual checks/findings, verified evidence and any high-severity override.
- `/app/approvals/` — current-workspace approval inbox for reviewers and workspace admins.
- `/app/approvals/<request-id>/` — exact version/review detail with evidence and decision history. Creators can access only their own requests; managers can inspect, but only reviewers/admins may decide.

The `content_reviews` app stores immutable `ContentSubmission` and `ContentVersion` records, a `ReviewRun` for each distinct request, the exact `ReviewPolicyVersion` pins, a `ReviewCheck` per configured rule, and `ReviewFinding` evidence when a check produces a real finding. Phase 3A supports only the saved `PROHIBITED_TERM`, `REQUIRED_TERM`, `REQUIRED_DISCLAIMER` and structured `APPROVED_FACT` rule types. Term checks use explicit phrases, channel scope and optional triggers. Fact comparisons require a shared metric phrase, an explicit matching unit and supported structured comparison. The numeric bound comparison currently supports `UP_TO`; an exact approved statement may be classified supported, and a comparable claim above its stored upper bound may be classified contradicted. An unsupported or unit-mismatched claim is `NOT_VERIFIED`, not called false. Under-specified checks are not turned into violations. Phase 5A optionally adds subjective, source-grounded AI analysis separately; deterministic checks are not a universal guarantee of factual accuracy or compliance.

At run start, BrandGuard briefly locks the workspace while storing the current policy revision/hash and exact approved source-version identities. Evaluation then queries only those pinned, effective versions. Each finding stores a real rule/source version and exact evidence excerpt, validated against that immutable policy text; matched-content offsets are verified against the saved content version. Future-effective sources are recorded but excluded. Later policy changes do not rewrite past runs. Effective dates are treated as start dates only; expiry is not modeled. Synchronous review is bounded to 500 rules, 250 findings and 20,000 content characters. A failed check preserves other completed results and leaves the run partial or failed, never a complete pass.

The import accepts UTF-8/UTF-8 BOM TXT, DOCX and text PDFs up to 5 MiB; PDFs are limited to 10 pages and extracted text to 80,000 characters by default. The parser checks both extension and file structure, rejects encrypted PDFs, corrupted files and documents without readable text. DOCX archive entry count, decompressed size and compression ratios are bounded. Scanned PDFs are not OCR'd; the form directs users to enter their authoritative text manually. The source version created by import is the same `SourceVersion` queried by policy snapshots and the current deterministic review engine.

Extracted text is saved as an ordinary editable draft. A manager/admin must inspect it, save corrections, then submit the separate confirmation action. The approval service verifies that the confirmed SHA-256 matches the current source text, then runs the existing evidence validation and atomic policy publication. Import never creates rules/facts automatically and never approves a source by itself.

Original files use generated names in the `private_documents` storage alias. Local development defaults to `private_uploads/` outside `MEDIA_URL`, static serving and public download routes; it is ignored by Git. There is no file download endpoint. Production document imports are disabled unless a private S3-compatible bucket and region are configured; an absent/unsupported setup fails closed and the import page offers manual source entry. When configured, Django Storages uses a private/default-private object setup and signed URLs remain enabled, but uploads have no public download route. Operators must separately verify bucket public-access blocks, provider credentials, encryption, backup and retention policies. The production bucket name, region and optional HTTPS endpoint use `PRIVATE_DOCUMENT_BUCKET`, `PRIVATE_DOCUMENT_REGION`, and `PRIVATE_DOCUMENT_ENDPOINT_URL`; local filesystem paths remain development-only. Do not configure public media for these objects.

Limits can be lowered with `BRAND_DOCUMENT_MAX_UPLOAD_BYTES`, `BRAND_DOCUMENT_MAX_PDF_PAGES` and `BRAND_DOCUMENT_MAX_EXTRACTED_CHARS`; settings reject values above 5 MiB, 10 pages and 80,000 characters respectively. Set `PRIVATE_DOCUMENT_ROOT` to choose the local private file location. None of these values contain secrets. `.env.example` documents the variable names but is not loaded automatically.

## Configuration and PostgreSQL

SQLite is used when `DATABASE_URL` is unset in local development. Production requires a PostgreSQL URL; missing URLs and SQLite URLs fail configuration rather than falling back. PostgreSQL can be selected with an environment URL; create the database and account separately:

```powershell
$env:DATABASE_URL = 'postgresql://brandguard@localhost:5432/brandguard'
```

Replace these example connection components in your local environment, and keep credentials out of version control. `DB_SSLMODE` can set the PostgreSQL TLS mode. See `.env.example` for the setting names.

Render uses its PostgreSQL internal URL when the web service and database share a region. Production requires TLS (`sslmode=require` unless a stricter supported mode is supplied), sets a 5-second connection timeout and keeps connections for 60 seconds with health checks. `config/settings.py` trusts `X-Forwarded-Proto: https` only when Render sets its documented `RENDER=true` environment flag; `DJANGO_ENV=production` by itself does not trust forwarded headers. Render's public edge handles HTTP-to-HTTPS redirection; app-level SSL redirects remain off so its health probe works. HSTS stays at zero by default until the final HTTPS hostname is verified.

## Checks and tests

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py migrate
python manage.py test
```

The test suite covers Phase 0 landing/health routes, Phase 1A authentication and membership security, Phase 1B profile/password/navigation/member safeguards, Phase 2A source/rule/fact CRUD and policy snapshots, Phase 2B synthetic TXT/DOCX/PDF extraction and confirmation, Phase 3A deterministic reviews, Phase 3B immutable revisions and human decisions, Phase 4A current-approval-protected TXT/PDF exports, and Phase 5A/5D AI isolation, consent, evidence validation, provider failure handling and simulated Demo AI. `python manage.py collectstatic --noinput` verifies/builds deployment static assets; generated `staticfiles/` output is ignored.

## Render deployment preparation

Phase 4B-1 prepared a pinned Render Python runtime, Gunicorn/WhiteNoise configuration, fail-closed production PostgreSQL settings, secure proxy/cookie/host configuration, a `build.sh` that installs, collects static files and migrates, and a public `/health/` check. The operator reports the judge demo release at `9dc94c0b5f8ed0b4db4a70aee807a015462d4f1a` was manually deployed and tested. This local repository session did not inspect the Render dashboard. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for deployment settings, storage limitations and checks.

Brand-source document imports are intentionally disabled in production until a persistent private S3-compatible bucket, region and credentials are configured. The rest of the app—including existing source text, deterministic reviews, approvals and guarded TXT/PDF exports—does not depend on that bucket. Local imports continue using ignored `private_uploads/` storage.

`seed_demo` remains local-only (DEBUG + SQLite). The separate `bootstrap_demo` management command requires a PostgreSQL production environment, role-specific password environment variables, `BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP=YES`, and `--confirm-production-demo-bootstrap`. It does not run during builds, never prints or resets passwords, refuses conflicting non-demo data, and was not executed against production. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#explicit-production-demo-bootstrap).

## Architecture and limits

- `config/` — environment-backed settings, middleware, and named root routes.
- `core/` — public landing/health pages, reusable responsive application shell, local styles and accessible mobile-navigation script, plus the authenticated overview template.
- `accounts/` — a Django `ModelBackend` subclass, login and settings forms/views, and POST-only logout. Password hashes and session behavior use Django. The login identifier is an exact username or case-insensitive email; when an identifier ambiguously matches one account's username and another account's email, authentication is rejected. Django's `LoginView` validates `next` against the current host/scheme. Account changes are limited to first and last name plus Django-validated password changes.
- `workspaces/` — workspace and membership models, one-membership-per-user/workspace database constraint, server-side membership/role decorators, shared app page context, the role-aware `/app/` overview, the admin-only workspace member view, and guarded demo provisioning commands.
- `brand_sources/` — workspace-scoped `BrandSource`, immutable `SourceVersion`, structured `BrandRule` matcher schemas, `ApprovedFact` and `BrandDocumentImport`. Services own bounded extraction, private file persistence, hash provenance, explicit text confirmation, draft cloning, atomic approval/supersession, active-version queries and policy snapshots. Imported text uses the exact same `SourceVersion` and evidence records as manual authoring. Rule/fact evidence must occur verbatim in the source version; offsets are checked. Matcher types use an allowlisted schema, never executable expressions.
- `content_reviews/` — text-only submissions and immutable content versions, snapshot-pinned review runs, exact approved source-version pins, per-rule check outcomes and findings validated against real source evidence. It also owns immutable version/finding comments and append-only resolution events. It evaluates no policy outside the stored snapshot. Its local scenario seeder submits synthetic copy through the same review service rather than inserting hardcoded findings.
- `approvals/` — one immutable-version-bound approval request and immutable human decision records, including exact review/policy snapshot, reviewer identity, changes comments and high-severity override rationale. `current_valid_approval()` is the reusable current-approval gate used by every export request.
- `content_reviews/exports.py` — workspace/role/owner-scoped export eligibility, evidence integrity checks, UTF-8 TXT content and paginated ReportLab report generation from saved records.
- `ai_analysis/` — separate advisory run/finding records, a validated OpenAI Responses adapter for explicitly consented real analysis, and a repository-controlled Demo AI adapter that uses actual approved synthetic source excerpts and never contacts an external provider.
- `docs/` — phase plan and design system.

The overview displays the signed-in identity, active workspace and membership role, real workspace and membership status/dates, role-specific guidance, and workspace-scoped counts with recent submissions and deterministic policy reviews. Creator counts and records are limited to that creator's own submissions; reviewer and management roles see current-workspace records according to their permissions. It does not show analytics, notifications, compliance scores, or fabricated dashboard records. Workspace admins can inspect actual membership records scoped to their current workspace; inactive membership records are shown as inactive. Other roles cannot access the member route, even by direct URL. The profile form only saves first and last name. Django's built-in `PasswordChangeForm` validates password changes, and the current session is updated with `update_session_auth_hash`. Profile and password changes use POST with CSRF protection. Role decisions are resolved from database membership, never client fields. A user with no active membership, an inactive workspace, or ambiguous multiple active memberships is denied workspace access.

Phases 3A and 3B implement text submissions, bounded deterministic checks, immutable creator revisions, human request-changes/approval decisions and current-approval validation. Phase 4A adds only private approved-content TXT and evidence-backed PDF report downloads. The product still does not include campaign-document uploads, independent or universal fact verification, semantic interpretation/synonyms, OCR or image/poster review, automatic suggestion application, public registration, tamper-proof audit logging or new roles. Brand-source publishing remains distinct from marketing-content approval. Imported original brand documents have no download UI/API; scanned-image text is not extracted. Effective dates are treated as start dates but expiry is not modeled. Account settings cannot change email, username or role. Workspace administration remains limited to the existing read-only member list plus source-policy management for admins. The existing Django user model is retained.

### Phase 2A permissions and policy snapshots

| Role | View approved sources/evidence | View draft/superseded versions | Create/edit draft policy | Publish source versions |
| --- | --- | --- | --- | --- |
| Creator | Yes | No | No | No |
| Reviewer | Yes | No | No | No |
| Brand manager | Yes | Yes | Yes | Yes |
| Workspace admin | Yes | Yes | Yes | Yes |

Authorization is server-side and every source/version/evidence query is scoped to the active workspace membership. Publishing validates every rule/fact excerpt against the full source text inside one transaction, records approver/time, supersedes the prior source version, increments `Workspace.policy_revision` and recomputes `Workspace.policy_snapshot_hash`. Historical approved versions and evidence remain stored.

Phase 3A applies that design: `ReviewRun` persists the revision, hash and exact `ReviewPolicyVersion` pins captured when the run starts. Its checks query only those source-version IDs; the result page compares the stored identity to the current policy to warn about later changes without rewriting historical evidence. `active_policy_versions(workspace)` still supplies the current approved set. See `docs/REVIEW_ENGINE.md` for the locking strategy and limits.

`seed_demo` adds five synthetic LumaDesk sources: product naming with `TERM-001` (prohibited “Luma Desk”, preferred “LumaDesk”) and `TERM-002` (required “LumaDesk”); voice guidance; `DISC-001` (legacy disclaimer context) and `DISC-002` (explicit “internal pilot” trigger requiring “Results vary.”); and fact sheet `FACT-02` (fictional “Up to 20% faster task completion in our internal pilot; results vary.”, represented as `UP_TO`, `20`, `%`). Structured entries link exact source excerpts. The statement is fictional and not independently verified.

| Role | List workspace submissions | Create/run own content | Inspect findings |
| --- | --- | --- | --- |
| Creator | Own submissions only | Yes | Own submissions only |
| Reviewer | All current-workspace submissions | No | Current workspace |
| Brand manager | All current-workspace submissions | No | Current workspace |
| Workspace admin | All current-workspace submissions | No | Current workspace |

## PRD note

The requested path `docs/BrandGuard_Round2_PRD.pdf` is absent. The available source was `BrandGuard_Round2_Product_Requirements_Document.pdf` in the repository root and was read with `pypdf`. Its FR-09 requires approval bound to an immutable content version/hash and source snapshot, current-version/policy checks at decision time, override rationale for high-severity findings, self-approval prevention, and stale status after content or policy changes. The Phase 3B implementation meets these controls and the stricter role boundaries in this brief. See `docs/APPROVAL_WORKFLOW.md`.

## Phase 3B revision and human approval

Creators can append a new content version, compare saved versions, rerun deterministic checks, and submit only the latest version after a fresh complete review. Reviewers/admins can inspect the exact content, findings and evidence, then request changes with a written comment or approve. A high-severity finding requires a meaningful override reason regardless of its operational collaboration status; its original ID is recorded on the decision. Historical approvals remain intact. `approvals.services.current_valid_approval(submission)` checks exact content identity and active policy snapshot; Phase 4A export endpoints re-run this guard on each request and verify report evidence links.

There is no automatic application of suggested corrections, notification integration, tamper-proof auditing or general fact verification. Optional AI analysis is advisory only and requires explicit consent; see `docs/AI_INTEGRATION.md`. Export is limited to the current approved version as UTF-8 TXT and an evidence-backed PDF report. See `docs/APPROVAL_WORKFLOW.md` and `docs/EXPORTS.md` for decision and export guards.

### Collaborative review discussion and finding resolution

Version-level discussion and finding-specific comments are available on saved review content. Comments are immutable, workspace-scoped records with an author/role snapshot and timestamp; feeds are shown oldest first. Findings have operational states `OPEN`, `ACKNOWLEDGED`, `RESOLVED`, and `WONT_FIX`, with every transition recorded in an append-only event history. These annotations do not modify the original finding, classification, evidence, content version, or approval decision. `RESOLVED` requires an explanatory note, while `WONT_FIX` requires a reason. Creators may acknowledge their own findings and resolve them after saving a newer version; reviewers, managers and admins may participate in workspace discussion and update permitted transitions. A resolved or wont-fix label does not approve content or waive the existing high-severity override requirement. See `docs/APPROVAL_WORKFLOW.md` for transitions and permission rules.

## Optional AI advisory: real provider and simulated demo

- Added the optional `ai_analysis` Django app with separate, immutable `AIAnalysisRun` and `AIAnalysisFinding` records. The new migration is additive; deterministic review, approvals, workflow state and approved exports are unchanged.
- A creator may explicitly request analysis for their own saved content version using the POST route `/app/reviews/<submission-id>/versions/<version>/ai/analyze/`. The form requires a CSRF-protected consent checkbox disclosing that content and relevant approved source excerpts are sent to OpenAI. Ordinary page views make no provider calls.
- The real Responses API adapter uses strict JSON Schema output, `store=False`, no SDK retries, bounded timeout/output/context/findings/content length and per-user/version rate limiting. Default model is `gpt-5.4-mini`; the real-provider feature defaults off. Automated provider tests use mocks.
- Server validation checks response schema and classifications, exact workspace/policy/source identity, that evidence was sent to the provider and occurs in immutable approved source text, matched-content offsets, and numeric suggestion limits. Uncited claims may only be `NOT_VERIFIED` or `NEEDS_HUMAN_REVIEW`. Tone/messaging advisories remain subjective.
- Each run pins the content hash and approved policy snapshot. Old content/policy analyses remain historical. They never modify deterministic findings, content workflow state, approval eligibility, reviewer decisions or export validity.
- Demo AI mode is separate and uses a repository-controlled adapter on retrieved approved LumaDesk excerpts. It goes through the same server-side schema/evidence validator, stores provider identity `demo` / model `simulated-brandguard-demo`, and makes no network request. The UI clearly calls it simulated and requires explicit confirmation. Both flags cannot be active together.
- `docs/AI_INTEGRATION.md` covers setup, privacy/retention, provider billing, failure behavior and rollback. The `openai` SDK is only used by optional real-provider mode; Demo AI requires no key.

One separately authorized Phase 5D-B request with synthetic content was rate limited; no findings were saved and it was not retried. Demo AI is the zero-cost judge path and is not a live model response. For the presentation set `BRANDGUARD_AI_ENABLED=false` and `BRANDGUARD_AI_DEMO_MODE=true`; to disable all advisory analysis set both to `false`.

## Judge demo walkthrough

The public **Try the BrandGuard demo** action opens the existing sign-in page and returns an authenticated user to `/app/`; it does not create an account or bypass authentication. Demo credentials are shared privately. The workspace overview now reports actual approved-source, submission and pending-decision counts, plus saved recent submissions and policy reviews, scoped to the signed-in user's role.

Demo account passwords are provisioned separately and shared privately; they are never published in this repository or the UI. The login page names the separate Creator, Brand manager and Reviewer personas; there is no passwordless or public-registration path. Start at `/`, choose **Log in**, and sign in with the LumaDesk Creator account supplied with the demo. In **Brand sources**, open the approved LumaDesk guidance. From **New content submission**, choose **Load demo example** to fill the canonical synthetic copy without saving or starting a review:

```text
Up to 20% faster task completion in our internal pilot; results vary. LumaDesk is a productivity miracle. We guarantee 100% faster task completion for every team.
```

Review or edit the populated fields, then save; the sample button does not submit. Run **Run policy review**, inspect findings and cited approved evidence, then acknowledge the simulation disclosure and choose **Run demo AI analysis**. The saved result is clearly labeled simulated and no external provider is contacted. Revise the copy, compare versions, rerun the deterministic review, and submit the fresh complete review. Sign out and sign in as the separate LumaDesk Reviewer to inspect the exact version and evidence in **Approval inbox**, then approve or request changes. After approval, sign back in as the Creator to download the TXT and PDF. To avoid an override in a clean approval demo, resolve high-severity findings before submitting. Do not edit shared judge content or policy merely to show stale-export protection; use a disposable local workspace for that scenario.

**Judge operator note:** A free Render web service may sleep between visits. Open the public homepage shortly before judging so the service can wake; the first request may take longer than later requests. This does not change demo data or enable external AI.

### Phase 5D-C — zero-cost Demo AI mode

The optional `BRANDGUARD_AI_DEMO_MODE` flag enables repository-controlled, clearly simulated advisory output without any external provider request or API key. For the hackathon demo set `BRANDGUARD_AI_ENABLED=false` and `BRANDGUARD_AI_DEMO_MODE=true`. The existing approved synthetic LumaDesk source pack is used to construct a small advisory, which is passed through the same policy snapshot, citation, source-text and content-span validation as real provider output. Saved runs identify themselves as `demo` / `simulated-brandguard-demo`; the UI states that no external provider was contacted. If both mode flags are true, the feature fails closed. Demo findings do not alter deterministic review records, approvals or exports. This mode is simulated and must not be described as a live model response.
