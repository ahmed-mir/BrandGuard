# Render deployment preparation

This document prepares the existing BrandGuard project for a **manual** Render deployment. Phase 4B-1 does not create GitHub or Render resources, push code, configure a domain, contact a production database, or claim that a public site is live.

## Runtime and process model

- Render's native Python runtime is pinned in `.python-version` to **Python 3.14.6**. The local project environment was Python 3.14.6; Render is configured independently from the local interpreter. Django 6.1 supports the 3.14 series, and all project requirements are constrained for this application. Render reads a fully qualified version from `.python-version`; see [Render's Python version configuration](https://render.com/docs/python-version) and the [Django 6.1 Python compatibility notes](https://docs.djangoproject.com/en/6.1/releases/6.1/#python-compatibility).
- `requirements.txt` retains the existing Django, psycopg 3, `python-docx`, `pypdf`, and ReportLab dependencies. Production additions are Gunicorn (WSGI server), WhiteNoise (static files), and `django-storages[s3]`/boto3 (optional private S3-compatible storage). There is no Redis, worker, Docker requirement, or second application server.
- `config.wsgi:application` is the production entry point. Render start command: `gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 90`. Two synchronous workers are a conservative small-service setting for BrandGuard's bounded synchronous text review. Render supplies `PORT`; the fallback is for local convenience.
- Build command: `bash ./build.sh`. It installs requirements, runs `collectstatic --noinput`, then `migrate --noinput`; `set -o errexit` stops the build on the first error. No demo data is seeded during builds. Because migrations run during the build, keep schema changes backward-compatible with the code currently serving requests; use staged expand/contract migrations for changes that cannot safely overlap.

## Manual GitHub preparation

Use the existing repository and preserve its current branch/history. Create a GitHub repository through the GitHub website only when the operator is ready; this project does not create it. From PowerShell at the repository root:

```powershell
git status
git remote -v
# Only if no origin is configured:
git remote add origin https://github.com/<OWNER>/<REPOSITORY>.git
git push -u origin HEAD
```

Replace the placeholders with the operator's repository. Review and commit the local changes before pushing; this phase did not commit or push them. Do not place `.env`, database URLs, storage credentials, passwords, local databases, or uploaded documents in Git.

## Create Render resources manually

1. In the Render Dashboard, choose **New → PostgreSQL**. Select a region and plan deliberately. For a Render web service in the same region, use the database's **Internal Database URL** for lower-latency private-network access. The application requires a PostgreSQL URL; production cannot fall back to SQLite.
2. Choose **New → Web Service**, connect the GitHub repository, select the Python runtime, and choose a service plan. This repository includes no Blueprint and creates no resources automatically.
3. Configure:
   - Build command: `bash ./build.sh`
   - Start command: `gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers 2 --timeout 90`
   - Health check path: `/health/`
4. Add the environment variables in the Render Dashboard. Set secrets using Render's secret/environment controls, never in a committed file. Use the exact host assigned to the service:

| Key | Required value |
| --- | --- |
| `DJANGO_ENV` | `production` (Render's built-in `RENDER=true` also activates production safeguards) |
| `DJANGO_SECRET_KEY` | A newly generated random value of at least 50 characters; do not reuse the development key |
| `DJANGO_DEBUG` | `false` |
| `DJANGO_ALLOWED_HOSTS` | Exact deployed host, e.g. `<service>.onrender.com`; comma-separate any verified custom host later |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Exact HTTPS origin, e.g. `https://<service>.onrender.com`; comma-separate any verified custom origin later |
| `DATABASE_URL` | Render PostgreSQL **internal** URL from the same region. Keep credentials in Render environment configuration. TLS is required by settings (`sslmode=require` by default); Render documents TLS with `require` for internal connections. |
| `DB_CONNECT_TIMEOUT` | Optional; defaults to `5` seconds, maximum `30` |
| `DB_CONN_MAX_AGE` | Optional; defaults to `60` seconds, maximum `600` |
| `DJANGO_HSTS_SECONDS` | Keep `0` until HTTPS and domain configuration are verified. After verification, an operator may choose a cautious positive value. Include-subdomains and preload default off. |

Do not set `DATABASE_URL` to a SQLite path. The production settings reject missing URLs, SQLite schemes, missing PostgreSQL hosts/database names, and TLS-disabled PostgreSQL URLs. Render terminates public TLS at its edge, sends `X-Forwarded-Proto` for HTTPS requests, and redirects public HTTP to HTTPS. `SECURE_PROXY_SSL_HEADER` is enabled only when Render sets its documented automatic `RENDER=true` variable; `DJANGO_ENV=production` alone does not trust forwarded headers. Django leaves its own SSL redirect off so platform health probes do not loop or fail. This is based on [Render's documented edge/proxy behavior](https://render.com/tutorials/web-service-vs-static-site/web-services); see Django's [proxy SSL header security requirements](https://docs.djangoproject.com/en/6.1/ref/settings/#secure-proxy-ssl-header). Do not reuse that setting behind a different proxy unless it strips client-supplied forwarded headers and sets the header itself.

5. Deploy. The build runs schema migrations against the configured database and fails the build if they fail. Review migration output; no business fixture command runs as part of deploy. Confirm `/health/` returns HTTP 200 with `{"status":"ok"}` before proceeding.

## Private brand-document storage

Render web filesystems are ephemeral unless a persistent disk is attached. Free web services do not support persistent disks. BrandGuard therefore **disables source-document imports in production by default** and uses a fail-closed storage backend instead of silently writing uploads to an ephemeral directory. Existing approved source text, reviews, approvals, and TXT/PDF marketing-content exports still work. The document-import page explains that uploads are unavailable and links to manual source entry.

To enable document imports, use a private S3-compatible bucket and configure:

| Key | Value |
| --- | --- |
| `PRIVATE_DOCUMENT_STORAGE_BACKEND` | `storages.backends.s3.S3Storage` |
| `PRIVATE_DOCUMENT_BUCKET` | Existing bucket name |
| `PRIVATE_DOCUMENT_REGION` | Bucket/provider region |
| `PRIVATE_DOCUMENT_ENDPOINT_URL` | Optional S3-compatible endpoint; if set, it must start with `https://` |
| `AWS_ACCESS_KEY_ID` | Optional provider credential supplied through Render's secret store, when workload identity is unavailable |
| `AWS_SECRET_ACCESS_KEY` | Corresponding secret, supplied only through Render's secret store |
| `AWS_SESSION_TOKEN` | Optional short-lived credential token |

Before enabling uploads, an operator must configure the bucket/provider to block public access, prevent public bucket/object policies, use private/default-private object ACL behavior, enable provider-side encryption and backups/retention appropriate for the data, and scope credentials to the required bucket operations. BrandGuard sets `default_acl=None`, keeps authenticated query-string signing enabled in the backend, assigns generated storage names, and does not expose source-document URLs or downloads. This follows django-storages' [S3 backend configuration](https://django-storages.readthedocs.io/en/stable/backends/amazon-S3.html). The app cannot verify a provider's bucket policy or credentials during settings import. Test a synthetic upload through the application and verify that unauthenticated/public requests cannot retrieve the original before enabling imports for real documents. If configuration is absent or unsupported, import remains visibly disabled. Local development continues using ignored `private_uploads/` storage; tests use Django's isolated temporary test database and clean their synthetic files.

No object-store credentials are required to run local tests. No production storage bucket was configured or tested in Phase 4B-1.

## Explicit production demo bootstrap

`seed_demo` and `seed_review_scenarios` remain restricted to `DEBUG=True` and SQLite. They are not deployment commands. `bootstrap_demo` is a separate, repeatable, append-safe operator command that provisions only the synthetic `lumadesk-demo` workspace, demo creator/brand-manager/reviewer memberships and missing synthetic approved source pack entries. It does not create an admin account, reset any existing password, overwrite a non-demo workspace/source, seed review findings, or run during deployment.

New account passwords must be distinct, strong, role-specific environment values. Bootstrap additionally requires both `BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP=YES` and the CLI flag `--confirm-production-demo-bootstrap`; it refuses any non-PostgreSQL target. Existing demo credentials are never changed. The command never prints passwords. If an existing demo user's password is unavailable, use an authorized password-management procedure deliberately; do not expect the seeder to reset it.

Render's free web services do not provide a shell or one-off job. An operator may run the command from a trusted workstation against the database's external URL, with SSL required and database access temporarily restricted to the operator's IP, or use an available authorized paid Render shell/one-off execution path. Never put a URL or password in shell history or a ticket. A PowerShell pattern that avoids echoing the URL/password values is:

```powershell
$env:DJANGO_ENV = 'production'
$env:DJANGO_DEBUG = 'false'
$env:DJANGO_SECRET_KEY = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
$env:DJANGO_ALLOWED_HOSTS = '<service>.onrender.com'
$env:DJANGO_CSRF_TRUSTED_ORIGINS = 'https://<service>.onrender.com'
$databaseUrlSecret = Read-Host 'Paste the external PostgreSQL URL with sslmode=require' -AsSecureString
$env:DATABASE_URL = [System.Net.NetworkCredential]::new('', $databaseUrlSecret).Password
$env:BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP = 'YES'
$creatorSecret = Read-Host 'New demo creator password (20+ characters)' -AsSecureString
$managerSecret = Read-Host 'New demo brand manager password (20+ characters)' -AsSecureString
$reviewerSecret = Read-Host 'New demo reviewer password (20+ characters)' -AsSecureString
$env:BRANDGUARD_DEMO_CREATOR_PASSWORD = [System.Net.NetworkCredential]::new('', $creatorSecret).Password
$env:BRANDGUARD_DEMO_BRAND_MANAGER_PASSWORD = [System.Net.NetworkCredential]::new('', $managerSecret).Password
$env:BRANDGUARD_DEMO_REVIEWER_PASSWORD = [System.Net.NetworkCredential]::new('', $reviewerSecret).Password
python manage.py migrate --check
python manage.py bootstrap_demo --confirm-production-demo-bootstrap
Remove-Item Env:DATABASE_URL, Env:DJANGO_SECRET_KEY, Env:BRANDGUARD_DEMO_CREATOR_PASSWORD, Env:BRANDGUARD_DEMO_BRAND_MANAGER_PASSWORD, Env:BRANDGUARD_DEMO_REVIEWER_PASSWORD, Env:BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP
Remove-Variable databaseUrlSecret, creatorSecret, managerSecret, reviewerSecret
```

Use a different freshly generated Django key for this operator process if desired; the command does not change the deployed service's Render key. Copy the deployed service's exact public host, and supply the correct PostgreSQL external URL through the masked prompt. The stored passwords are not recoverable from the command. Keep the login credentials in an approved password manager and remove the temporary external database IP allowlist after bootstrap. **This procedure is documented only; it was not run against a production database in this phase.**

## Local / CI validation commands

Run from the repo root. A stable local secret is required by settings; the commands below generate a temporary secret in the current PowerShell process without printing it. They preserve `db.sqlite3` and existing accounts.

```powershell
$env:DJANGO_ENV = 'development'
$env:DJANGO_SECRET_KEY = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
$env:DJANGO_DEBUG = 'true'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1,testserver'
python manage.py check
python manage.py makemigrations --check
python manage.py migrate
python manage.py test
python -m pip check
python manage.py collectstatic --noinput
git diff --check
```

For production-like configuration checks (do not use a real database URL):

```powershell
$env:DJANGO_ENV = 'production'
$env:DJANGO_DEBUG = 'false'
$env:DJANGO_SECRET_KEY = & .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
$env:DJANGO_ALLOWED_HOSTS = 'brandguard.example.invalid'
$env:DJANGO_CSRF_TRUSTED_ORIGINS = 'https://brandguard.example.invalid'
$env:DATABASE_URL = 'postgresql://check-user:check-password@127.0.0.1:5432/brandguard_check'
python manage.py check
python manage.py check --deploy
python manage.py collectstatic --noinput
```

`check --deploy` may warn that Django-level SSL redirection and HSTS are off; Render performs HTTP-to-HTTPS redirect at its edge, and HSTS is deliberately withheld until the operator verifies the final domain/TLS setup. A placeholder PostgreSQL URL is enough for settings checks and static collection, but it is not a database integration test. Do not run migration or business-data commands against that placeholder.

### Validation recorded 2026-10-22

- Inspected project `.venv`: Python **3.14.6**, Django **6.1.2**. The global `python` command outside the project environment reports Python 3.14.0, so use `.venv` for reproducible local runs.
- `python manage.py check`: passed, no issues.
- `python manage.py makemigrations --check`: passed, no model changes detected.
- `python manage.py migrate`: passed; no migrations were pending and the existing `db.sqlite3` was not reset.
- `python manage.py test`: **166 tests passed**. This includes the existing application tests plus production settings/storage/bootstrap tests; the test database was SQLite.
- `python -m pip check`: passed, no broken requirements.
- `bash -n ./build.sh` and `bash ./build.sh` through Git for Windows Bash: passed locally. Requirements were already satisfied, static collection completed, and migrations reported no pending work. This local run used development SQLite; it does not verify PostgreSQL deployment migrations.
- Production-style `python manage.py collectstatic --noinput` with a placeholder PostgreSQL URL: passed; 2 source static files were collected and 6 post-processed. A Django test-client smoke request under `DEBUG=False` returned landing 200, fingerprinted CSS 200 (`text/css`) and health 200 (`{"status":"ok"}`). No placeholder DB connection was needed for these requests.
- `python manage.py check --deploy` returned successfully with only `security.W004` (HSTS not configured) and `security.W008` (Django SSL redirect off). Both are deliberate: HSTS remains 0 until the operator verifies HTTPS/domain configuration, and Render's edge performs public HTTP-to-HTTPS redirects while the app retains health-probe behavior.
- `git diff --check`: passed with no whitespace errors (Git may print its normal LF-to-CRLF working-copy notices on Windows).
- A TCP listener responded at `127.0.0.1:5432`, but a psycopg probe was rejected with `fe_sendauth: no password supplied`; no safe test connection string/credentials or isolated PostgreSQL test database was configured. Therefore PostgreSQL migrations, constraints and approval concurrency were **not tested against PostgreSQL**. No production DB or uploaded document storage was used.

## PostgreSQL compatibility status

Current models use standard PostgreSQL-compatible foreign keys, UUID/JSON fields, check constraints, partial unique constraints and transactions. PostgreSQL supports the `select_for_update` and conditional-constraint semantics the approval/versioning services depend on; SQLite remains useful locally but does not provide equivalent row locks. A local TCP listener responded on port 5432, but an unauthenticated psycopg probe was rejected because no password was supplied, and no isolated PostgreSQL test URL was configured. Therefore **PostgreSQL migrations, concurrency and transactional approvals remain unverified in an actual PostgreSQL integration run**. Before relying on production approvals, create a disposable PostgreSQL database and run the full suite/migrations and competing-approval concurrency tests there. Do not represent SQLite test passes as PostgreSQL coverage.

## Live workflow smoke path (after a deployment and explicit bootstrap)

1. Open the Render HTTPS service URL. Verify `/` loads the landing page, `/accounts/login/` renders the login form, and `/health/` returns status 200.
2. Sign in as `lumadesk_creator`; create a text submission and run the review. Inspect a real finding and its evidence.
3. Revise the submission, rerun the review, and submit the current reviewed version for approval.
4. Sign out. Sign in as `lumadesk_reviewer`; open `/app/approvals/`, inspect the exact version and evidence, and record a real request-changes or approval decision.
5. Sign back in as the creator. For an approved current version, download both TXT and PDF. Confirm the TXT bytes match the approved text and the PDF includes the actual review/policy references.
6. Create a new content version or change approved source policy. Confirm the previous approval is shown as stale and both direct export URLs refuse downloads.
7. If private bucket storage is configured, upload a synthetic `.txt` guideline and verify text extraction, confirmation-before-publish, no public media URL, and denied anonymous access to the original object. Without that configuration, verify the import page clearly reports that uploads are unavailable and manually create a source instead.

## Common failures

- **Settings fail on startup/build:** check that secret, exact hosts/origins and PostgreSQL `DATABASE_URL` are present in the Render environment, including build-time environment availability. Never solve this by allowing SQLite in production.
- **PostgreSQL connection timeout or TLS failure:** ensure Render and database are in the same region, use the internal URL, verify host/port/name, and retain `sslmode=require`. Credentials with reserved URL characters must be percent-encoded. Do not paste credentials into logs.
- **Host/CSRF rejection:** the host must be in `DJANGO_ALLOWED_HOSTS`; browser origins must be exact `https://...` entries in `DJANGO_CSRF_TRUSTED_ORIGINS`. Do not use `*`.
- **Static files are missing:** confirm build output includes successful `collectstatic`, that `STATIC_ROOT` is not overridden, and WhiteNoise middleware is immediately after Django's security middleware. The hashed static storage intentionally detects missing referenced assets.
- **Document import unavailable:** configure supported S3 storage and private bucket settings; absent/unsupported settings deliberately fail closed. Verify bucket region, permissions, private access, endpoint TLS and provider credentials. Do not attach uploads to `MEDIA_URL`.
- **Build migration fails:** inspect migration trace in the build log without exposing credentials. Test against a disposable PostgreSQL instance and apply safe, backward-compatible migrations; do not delete/squash migration history to work around deployment state.
- **Demo bootstrap rejects a collision:** inspect the exact workspace/user/source record in the authorized database. The command refuses to rewrite real or conflicting records; resolve the conflict deliberately rather than forcing the seed.

## Plan limitations (no price claims)

Render's free web services use an ephemeral filesystem, spin down after 15 minutes idle, and do not offer persistent disks or dashboard shell/one-off execution. Free Render Postgres currently expires 30 days after creation and has no database backups; this is suitable for a short-lived preview, not dependable production data. Persistent disks require a paid web service. Confirm current plan capabilities and costs in the Render Dashboard before provisioning; this document does not state prices. See [Render free service limits](https://render.com/docs/free), [persistent disks](https://render.com/docs/disks), and [Render's Django deployment guide](https://render.com/docs/deploy-django).

## Phase 4B-1 status and blockers

The local application is configured for Render's Python runtime, Gunicorn WSGI, PostgreSQL, WhiteNoise, HTTPS-aware secure cookies/proxy handling, health checks, guarded S3-compatible private imports, and explicit demo bootstrap. No service, database, repository, domain, bucket, or credentials were created or configured. No live URL exists from this work. Actual PostgreSQL and object-storage integration, production demo bootstrap, deployed workflow, monitoring, backup/restore and external-device browser verification remain for Phase 4B-2/operator setup.
