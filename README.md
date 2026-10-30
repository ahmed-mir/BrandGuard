# BrandGuard

BrandGuard is a source-backed review and approval system for marketing content. The product includes Django session authentication, role-aware workspaces, source authoring and human-verified TXT/DOCX/text-PDF ingestion, deterministic evidence-backed reviews, immutable content revisions, human approval of exact reviewed versions, and TXT/PDF exports guarded by current approval. Optional real AI advisory analysis is disabled by default and separate from deterministic findings and human decisions. Demo AI mode uses repository-controlled simulated output and makes no external provider request. Brand source approval publishes policy only; it does not approve marketing content.

## Requirements and local setup

- Python **3.14.6** is pinned for Render in `.python-version`; use Python 3.14 locally for the same dependency/runtime family. Verify with `python --version`.
- Django 6.1.x and all runtime packages are listed in `requirements.txt`.
- Local development defaults to SQLite. Render/production requires PostgreSQL through `DATABASE_URL` and the psycopg 3 driver; production fails closed if the database URL is missing or SQLite.
- TXT, DOCX and text-PDF source extraction uses `python-docx` and `pypdf`. PDF exports use ReportLab. Production uses Gunicorn and WhiteNoise; optional private S3-compatible document storage uses `django-storages[s3]`.

### Windows PowerShell quick start

From a new clone:

```powershell
git clone https://github.com/ahmed-mir/BrandGuard.git
cd BrandGuard
py -3.14 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
$env:DJANGO_ENV = 'development'
$env:DJANGO_SECRET_KEY = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
$env:DJANGO_DEBUG = 'true'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
$env:BRANDGUARD_AI_ENABLED = 'false'
$env:BRANDGUARD_AI_DEMO_MODE = 'true'
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in at `/accounts/login/`. `seed_demo` creates the synthetic `LumaDesk` workspace, five approved synthetic policy sources, and Creator, Brand manager, Reviewer and Workspace admin accounts. On a fresh local database it generates a distinct random password per account and prints each once; capture them securely at that time. Alternatively, before seeding, provide `BRANDGUARD_DEMO_PASSWORD` or a role-specific `BRANDGUARD_DEMO_CREATOR_PASSWORD`, `BRANDGUARD_DEMO_BRAND_MANAGER_PASSWORD`, `BRANDGUARD_DEMO_REVIEWER_PASSWORD` or `BRANDGUARD_DEMO_ADMIN_PASSWORD`. Supplied passwords are never printed, and existing account passwords are never reset. Do not publish or reuse local demo credentials.

To create six synthetic submissions and run the actual deterministic engine against them, open another terminal with the same Django environment and run `python manage.py seed_review_scenarios`. This command requires DEBUG + SQLite, preserves existing scenario records and is optional; `seed_demo` alone is enough to begin using the app. The production-only `bootstrap_demo` command has separate safeguards described in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#explicit-production-demo-bootstrap); it is never run by a normal build.

### macOS/Linux quick start

From a new clone, use Python 3.14, activate the virtual environment, then set the same process environment and run the same management commands:

```sh
git clone https://github.com/ahmed-mir/BrandGuard.git
cd BrandGuard
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
export DJANGO_ENV=development
export DJANGO_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"
export DJANGO_DEBUG=true
export DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
export BRANDGUARD_AI_ENABLED=false
export BRANDGUARD_AI_DEMO_MODE=true
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Django does **not** load `.env` files in this project. `.env.example` is a reference template, not an automatic loader; the commands above set the required values in the current shell. `DJANGO_SECRET_KEY` is required even in development. Never put a real key/password in Git. Leave `DATABASE_URL` unset for the clone-local `db.sqlite3` SQLite database. If an existing local database has seeded accounts but you lost a generated password, use `python manage.py changepassword <username>` deliberately; `seed_demo` cannot recover/reset an existing password. There is no public registration.

For the whole product journey, use separate Creator and Reviewer sign-ins; the Creator creates/reviews/revises/submits and the Reviewer inspects and decides. The landing page's **Try the BrandGuard demo** button still requires login. No demo password is embedded in the UI or README.

## Supported environment variables

`.env.example` contains safe names/defaults only. Django reads the process environment; Render supplies its own variables. “Optional” means settings have a default or the feature is not needed for that local workflow.

| Variable | Scope | Purpose/default |
| --- | --- | --- |
| `DJANGO_ENV` | Local/production | `development` by default; set `production` for deployment safeguards. Render's automatic `RENDER=true` also activates production safeguards. |
| `DJANGO_SECRET_KEY` | Required everywhere | Required to start Django. Use a random secret; production requires at least 50 characters. |
| `DJANGO_DEBUG` | Local/production | Defaults to `false`; set `true` for local development. Production rejects/enforces debug off. |
| `DJANGO_ALLOWED_HOSTS` | Optional locally; required production | Comma-separated hostnames, no scheme or wildcard. Local default is `localhost,127.0.0.1`. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Optional locally; required production | Exact HTTPS origins, comma-separated. Empty by default locally. |
| `DATABASE_URL` | Optional locally; required production | Omit locally to use the clone's `db.sqlite3`; may be a local SQLite or PostgreSQL URL. Production requires PostgreSQL. |
| `DB_CONNECT_TIMEOUT`, `DB_CONN_MAX_AGE` | Optional | PostgreSQL timeout/reuse defaults are 5 seconds/60 seconds. |
| `DB_SSLMODE` | Optional PostgreSQL | TLS mode override. Production requires `require`, `verify-ca` or `verify-full`; default is `require`. |
| `DJANGO_HSTS_SECONDS`, `DJANGO_HSTS_INCLUDE_SUBDOMAINS`, `DJANGO_HSTS_PRELOAD` | Production-only tuning | Defaults are `0`, `false`, `false`; do not enable HSTS until HTTPS/domain configuration is verified. |
| `PORT` | Host supplied | Render supplies the web-server port. The Gunicorn command has a local fallback of `10000`; Django settings do not consume this variable. |
| `BRANDGUARD_AI_ENABLED` | AI-only | Real provider path; defaults to `false`. A key alone does not enable calls. |
| `BRANDGUARD_AI_DEMO_MODE` | Demo AI | Defaults to `false`. Set true with real AI false for simulated repository-controlled advisories; no API key/provider call. Both flags true fails closed. |
| `BRANDGUARD_AI_PROVIDER` | Real AI only | `openai` (default) or `groq`; ignored when real AI is disabled or Demo AI is selected. Unsupported values fail closed. |
| `OPENAI_API_KEY` | OpenAI only | Server-side credential required only for enabled OpenAI analysis; never needed for Demo AI or ordinary startup. |
| `GROQ_API_KEY` | Groq only | Server-side credential required only for enabled Groq analysis; never needed for Demo AI or ordinary startup. |
| `BRANDGUARD_AI_MODEL` | OpenAI only | Defaults to `gpt-5.4-mini`; configurable OpenAI model identifier. |
| `BRANDGUARD_GROQ_MODEL` | Groq only | Defaults to `openai/gpt-oss-20b`; configurable Groq model identifier. |
| `BRANDGUARD_AI_TIMEOUT_SECONDS`, `BRANDGUARD_AI_MAX_OUTPUT_TOKENS`, `BRANDGUARD_AI_MAX_FINDINGS`, `BRANDGUARD_AI_MAX_CONTEXT_CHARS` | Real AI controls | Defaults: 15 seconds, 1,400 tokens, 12 findings and 10,000 context characters; settings enforce upper/lower bounds. |
| `BRAND_DOCUMENT_MAX_UPLOAD_BYTES`, `BRAND_DOCUMENT_MAX_PDF_PAGES`, `BRAND_DOCUMENT_MAX_EXTRACTED_CHARS` | Document import | Defaults: 5 MiB, 10 pages, 80,000 extracted characters; settings cap maximums. |
| `PRIVATE_DOCUMENT_ROOT` | Local storage | Local private filesystem root; defaults to ignored `private_uploads/`. Not served as public media. |
| `PRIVATE_DOCUMENT_STORAGE_BACKEND`, `PRIVATE_DOCUMENT_STORAGE_OPTIONS` | Optional local/custom storage | Local backend and JSON options; normally leave defaults. `PRIVATE_DOCUMENT_STORAGE_OPTIONS` is read only outside production. |
| `PRIVATE_DOCUMENT_BUCKET`, `PRIVATE_DOCUMENT_REGION`, `PRIVATE_DOCUMENT_ENDPOINT_URL` | Production storage | Optional private S3-compatible object storage settings. Without bucket/region, production imports are disabled rather than saved on Render's ephemeral disk. Endpoint, if used, must be HTTPS. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | Storage credentials | Read by boto3/django-storages when configured; supply only via a secret manager/workload identity, never `.env.example` or Git. |
| `BRANDGUARD_DEMO_PASSWORD`, `BRANDGUARD_DEMO_<ROLE>_PASSWORD` | Local demo seed | Optional local seed passwords (`CREATOR`, `BRAND_MANAGER`, `REVIEWER`, `ADMIN`). Unset passwords are generated and printed once by `seed_demo`; existing passwords are not changed. |
| `BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP` | Explicit one-off production bootstrap | Must equal `YES` in addition to the command's confirmation flag. Do not leave enabled during normal service operation. See deployment docs. |

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for Render settings and private storage safeguards, and [docs/AI_INTEGRATION.md](docs/AI_INTEGRATION.md) for provider privacy, consent and failure behavior.

## Routes

- `/` — public product landing page.
- `/health/` — process health response (does not check the database).
- `/accounts/login/` — Django session login by username or email.
- `/accounts/logout/` — POST-only session logout.
- `/app/` — authenticated, role-aware workspace overview. Requires exactly one active membership in an active workspace.
- `/app/analytics/` — read-only policy analytics calculated from persisted submissions, deterministic reviews/findings, collaboration resolution events, and approval requests/decisions. Creators see only their own records; other workspace roles see current-workspace aggregates.
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
python manage.py makemigrations --check --dry-run
python manage.py test
python -m pip check
```

The current full suite contains **301 automated tests**. It uses Django's SQLite test database and covers authentication/workspace isolation, policy/source management and import, deterministic reviews and evidence, immutable revisions, comments/resolution history, approval decisions and stale approval, guarded TXT/PDF exports, AI isolation and simulated Demo AI, analytics, readiness, inbox search, and submission activity. It does not prove PostgreSQL row-lock/concurrency behavior. Run migrations against your local database with `python manage.py migrate`; this is part of setup, not the migration-drift check above. `python manage.py collectstatic --noinput` verifies/builds deployment static assets; generated `staticfiles/` output is ignored.

## Render deployment preparation

The project includes a pinned Render Python runtime, Gunicorn/WhiteNoise configuration, fail-closed production PostgreSQL settings, secure proxy/cookie/host configuration, a `build.sh` that installs dependencies, collects static files and applies migrations, and a public `/health/` route. Deployment state and the currently serving commit must be checked in the Render dashboard; a Git commit or this README does not prove deployment. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for setup, storage limitations and checks.

Brand-source document imports are intentionally disabled in production until a persistent private S3-compatible bucket, region and credentials are configured. The rest of the app—including existing source text, deterministic reviews, approvals and guarded TXT/PDF exports—does not depend on that bucket. Local imports continue using ignored `private_uploads/` storage.

`seed_demo` remains local-only (DEBUG + SQLite). The separate `bootstrap_demo` management command requires a PostgreSQL production environment, role-specific password environment variables, `BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP=YES`, and `--confirm-production-demo-bootstrap`. It does not run during builds, never prints or resets passwords, refuses conflicting non-demo data, and was not executed against production. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#explicit-production-demo-bootstrap).

## Architecture and limits

- `config/` — environment-backed settings, middleware, and named root routes.
- `core/` — public landing/health pages, reusable responsive application shell, local styles and accessible mobile-navigation script, plus the authenticated overview template.
- `accounts/` — a Django `ModelBackend` subclass, login and settings forms/views, and POST-only logout. Password hashes and session behavior use Django. The login identifier is an exact username or case-insensitive email; when an identifier ambiguously matches one account's username and another account's email, authentication is rejected. Django's `LoginView` validates `next` against the current host/scheme. Account changes are limited to first and last name plus Django-validated password changes.
- `workspaces/` — workspace and membership models, one-membership-per-user/workspace database constraint, server-side membership/role decorators, shared app page context, the role-aware `/app/` overview, read-only `/app/analytics/` aggregates, the admin-only workspace member view, and guarded demo provisioning commands.
- `brand_sources/` — workspace-scoped `BrandSource`, immutable `SourceVersion`, structured `BrandRule` matcher schemas, `ApprovedFact` and `BrandDocumentImport`. Services own bounded extraction, private file persistence, hash provenance, explicit text confirmation, draft cloning, atomic approval/supersession, active-version queries and policy snapshots. Imported text uses the exact same `SourceVersion` and evidence records as manual authoring. Rule/fact evidence must occur verbatim in the source version; offsets are checked. Matcher types use an allowlisted schema, never executable expressions.
- `content_reviews/` — text-only submissions and immutable content versions, snapshot-pinned review runs, exact approved source-version pins, per-rule check outcomes and findings validated against real source evidence. It also owns immutable version/finding comments and append-only resolution events. It evaluates no policy outside the stored snapshot. Its local scenario seeder submits synthetic copy through the same review service rather than inserting hardcoded findings.
- `approvals/` — one immutable-version-bound approval request and immutable human decision records, including exact review/policy snapshot, reviewer identity, changes comments and high-severity override rationale. `current_valid_approval()` is the reusable current-approval gate used by every export request.
- `content_reviews/exports.py` — workspace/role/owner-scoped export eligibility, evidence integrity checks, UTF-8 TXT content and paginated ReportLab report generation from saved records.
- `ai_analysis/` — separate advisory run/finding records, validated OpenAI Responses and Groq Chat Completions adapters for explicitly consented real analysis, and a repository-controlled Demo AI adapter that uses approved synthetic source excerpts and never contacts an external provider.
- `docs/` — [deployment](docs/DEPLOYMENT.md), [review engine](docs/REVIEW_ENGINE.md), [approval workflow](docs/APPROVAL_WORKFLOW.md), [exports](docs/EXPORTS.md), [AI boundaries](docs/AI_INTEGRATION.md), [Round 3 technical summary](docs/ROUND3_TECHNICAL_SUMMARY.md) and [roadmap](docs/ROUND3_ROADMAP.md).

The overview displays the signed-in identity, active workspace and membership role, real workspace and membership status/dates, role-specific guidance, and workspace-scoped counts with recent submissions and deterministic policy reviews. Creator counts and records are limited to that creator's own submissions; reviewer and management roles see current-workspace records according to their permissions. `/app/analytics/` is a separate read-only dashboard. Its counts, proportions, rule frequencies, resolution states, approval outcomes, decision turnaround, and review trend are calculated from persisted records; it does not show a compliance score, benchmarks, or fabricated data. Finding proportions use only findings with a stored classification; category proportions use deterministic finding records in the selected scope; the median turnaround includes decisions with a recorded non-negative request-to-decision duration. Historical approval decisions are not presented as currently valid approvals. Workspace admins can inspect actual membership records scoped to their current workspace; inactive membership records are shown as inactive. Other roles cannot access the member route, even by direct URL. The profile form only saves first and last name. Django's built-in `PasswordChangeForm` validates password changes, and the current session is updated with `update_session_auth_hash`. Profile and password changes use POST with CSRF protection. Role decisions are resolved from database membership, never client fields. A user with no active membership, an inactive workspace, or ambiguous multiple active memberships is denied workspace access.

Phases 3A and 3B implement text submissions, bounded deterministic checks, immutable creator revisions, human request-changes/approval decisions and current-approval validation. Phase 4A adds only private approved-content TXT and evidence-backed PDF report downloads. The product still does not include campaign-document uploads, independent or universal fact verification, semantic interpretation/synonyms, OCR or image/poster review, automatic suggestion application, public registration, tamper-proof audit logging or new roles. Brand-source publishing remains distinct from marketing-content approval. Imported original brand documents have no download UI/API; scanned-image text is not extracted. Effective dates are treated as start dates but expiry is not modeled. Account settings cannot change email, username or role. Workspace administration remains limited to the existing read-only member list plus source-policy management for admins. The existing Django user model is retained.

### Round 3 product work

Round 3 added collaborative version/finding comments and append-only per-finding resolution history; persisted-record policy analytics; duplicate-action and stale-form safeguards; role-aware Review Readiness guidance; reviewer inbox status filters/search; and a read-only submission Activity timeline derived from existing records. Finding resolution is collaboration state, not approval. Analytics use saved records and do not present fabricated compliance scores. See [docs/ROUND3_TECHNICAL_SUMMARY.md](docs/ROUND3_TECHNICAL_SUMMARY.md) for the architecture and [docs/ROUND3_ROADMAP.md](docs/ROUND3_ROADMAP.md) for completed, pending-validation and future work.

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

Submission **Activity** on a content version page is a read-only timeline derived from the existing submission, version, deterministic review, collaboration, approval and completed Demo AI records. It does not create a separate audit-log source of truth, and simulated AI events are explicitly labeled.

There is no automatic application of suggested corrections, notification integration, tamper-proof auditing or general fact verification. Optional AI analysis is advisory only and requires explicit consent; see `docs/AI_INTEGRATION.md`. Export is limited to the current approved version as UTF-8 TXT and an evidence-backed PDF report. See `docs/APPROVAL_WORKFLOW.md` and `docs/EXPORTS.md` for decision and export guards.

### Collaborative review discussion and finding resolution

Version-level discussion and finding-specific comments are available on saved review content. Comments are immutable, workspace-scoped records with an author/role snapshot and timestamp; feeds are shown oldest first. Findings have operational states `OPEN`, `ACKNOWLEDGED`, `RESOLVED`, and `WONT_FIX`, with every transition recorded in an append-only event history. These annotations do not modify the original finding, classification, evidence, content version, or approval decision. `RESOLVED` requires an explanatory note, while `WONT_FIX` requires a reason. Creators may acknowledge their own findings and resolve them after saving a newer version; reviewers, managers and admins may participate in workspace discussion and update permitted transitions. A resolved or wont-fix label does not approve content or waive the existing high-severity override requirement. See `docs/APPROVAL_WORKFLOW.md` for transitions and permission rules.

## Optional AI advisory: real provider and simulated demo

- Added the optional `ai_analysis` Django app with separate, immutable `AIAnalysisRun` and `AIAnalysisFinding` records. The new migration is additive; deterministic review, approvals, workflow state and approved exports are unchanged.
- A creator may explicitly request analysis for their own saved content version using the POST route `/app/reviews/<submission-id>/versions/<version>/ai/analyze/`. The form requires a CSRF-protected consent checkbox naming the selected external provider. Ordinary page views make no provider calls.
- The real provider adapters use strict JSON Schema output, no SDK retries, bounded timeout/output/context/findings/content length and per-user/version rate limiting. Select with `BRANDGUARD_AI_PROVIDER=openai|groq`; OpenAI uses the Responses API and defaults to `gpt-5.4-mini`; Groq uses Chat Completions at `https://api.groq.com/openai/v1` and defaults to `openai/gpt-oss-20b`. Groq sends the same BrandGuard schema through strict Structured Outputs with `max_completion_tokens`. OpenAI uses `OPENAI_API_KEY` and sends `store=False`; Groq uses `GROQ_API_KEY`. Real-provider mode defaults off. Automated provider tests use mocks. Separately, one authorized local synthetic Groq analysis completed successfully through BrandGuard and passed structured output, domain, and evidence validation. OpenAI remains supported, but its prior live request was rate-limited and OpenAI live success has not been established.
- Server validation checks response schema and classifications, exact workspace/policy/source identity, that evidence was sent to the provider and occurs in immutable approved source text, matched-content offsets, and numeric suggestion limits. Uncited claims may only be `NOT_VERIFIED` or `NEEDS_HUMAN_REVIEW`. Tone/messaging advisories remain subjective. Run identity includes provider and model so cached/reused results cannot cross providers or model versions.
- Each run pins the content hash and approved policy snapshot. Old content/policy analyses remain historical. They never modify deterministic findings, content workflow state, approval eligibility, reviewer decisions or export validity.
- Demo AI mode is separate and uses a repository-controlled adapter on retrieved approved LumaDesk excerpts. It goes through the same server-side schema/evidence validator, stores provider identity `demo` / model `simulated-brandguard-demo`, and makes no network request. The UI clearly calls it simulated and requires explicit confirmation. Both flags cannot be active together.
- `docs/AI_INTEGRATION.md` covers setup, privacy/retention, provider billing, failure behavior and rollback. The `openai` SDK is only used by optional real-provider mode; Demo AI requires no key.

Round 2 Improvement #1 is complete locally: one authorized Groq Chat Completions request using synthetic LumaDesk content completed through BrandGuard, persisted two validated findings, and left deterministic review, approvals, and exports unchanged. Earlier Groq Responses API attempts failed and were not retried. The successful run does not validate OpenAI live access and does not mean the unpushed code is deployed. Demo AI is the zero-cost judge path and is not a live model response. For the presentation set `BRANDGUARD_AI_ENABLED=false` and `BRANDGUARD_AI_DEMO_MODE=true`; to disable all advisory analysis set both to `false`.

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
