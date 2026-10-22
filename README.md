# BrandGuard

BrandGuard is a source-backed review and approval system for marketing content. Phases 0, 1A and 1B provide the public product introduction, Django session authentication, workspace-scoped identity, a role-aware application shell, editable basic profile details, secure password changes, and an administrator-only read-only member list. The LumaDesk examples on the public page remain illustrative; no brand review, approval, or export is performed.

## Requirements

- Python 3.12 or newer.
- Django 6.1.x (constrained in `requirements.txt`).
- SQLite by default. PostgreSQL is supported through `DATABASE_URL` and the included psycopg driver.

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

The test suite covers Phase 0 landing/health routes, Phase 1A authentication and membership security, and Phase 1B role-specific navigation, account/profile/password behavior, CSRF, session preservation, admin-only members access, workspace isolation, and read-only boundaries. `python manage.py collectstatic --noinput` can be used to verify/build deployment static assets; generated `staticfiles/` output is ignored.

## Architecture and limits

- `config/` — environment-backed settings, middleware, and named root routes.
- `core/` — public landing/health pages, reusable responsive application shell, local styles and accessible mobile-navigation script, plus the authenticated overview template.
- `accounts/` — a Django `ModelBackend` subclass, login and settings forms/views, and POST-only logout. Password hashes and session behavior use Django. The login identifier is an exact username or case-insensitive email; when an identifier ambiguously matches one account's username and another account's email, authentication is rejected. Django's `LoginView` validates `next` against the current host/scheme. Account changes are limited to first and last name plus Django-validated password changes.
- `workspaces/` — workspace and membership models, one-membership-per-user/workspace database constraint, server-side membership/role decorators, shared app page context, the role-aware `/app/` overview, the admin-only workspace member view, and a local-only `seed_demo` management command.
- `docs/` — phase plan and design system.

The overview displays the signed-in identity, active workspace and membership role, the real workspace and membership status/dates, and role-specific guidance. It does not query or display review counts, submissions, analytics, notifications, compliance scores, or fabricated dashboard records. Content review activity is explicitly marked unavailable because review records are not implemented. Role-specific future navigation items are noninteractive and marked unavailable. Workspace admins can inspect actual membership records scoped to their current workspace; inactive membership records are shown as inactive. Other roles cannot access the member route, even by direct URL. The profile form only saves first and last name. Django's built-in `PasswordChangeForm` validates password changes, and the current session is updated with `update_session_auth_hash`. Profile and password changes use POST with CSRF protection. Role decisions are resolved from database membership, never client fields. A user with no active membership, an inactive workspace, or ambiguous multiple active memberships is denied workspace access.

Brand guideline management, content submissions, evidence-backed review, revisions, approval decisions, audit history, AI integration, exports, public registration, and workspace/member modification remain deferred. Account settings cannot change email, username or role. Workspace administration is read-only in Phase 1B. No custom user model or PRD-specific role behavior beyond the documented workspace roles was introduced.

## PRD note

The requested path `docs/BrandGuard_Round2_PRD.pdf` is absent. The available source was `BrandGuard_Round2_Product_Requirements_Document.pdf` in the repository root; it was read with a development-only `pypdf` install, which is not a runtime dependency. Phase 1A implemented the account, role, workspace and security foundation; Phase 1B adds workspace UX and basic account settings while leaving workspace membership read-only and keeping later business workflow features deferred. See `docs/PHASES.md` for reconciliation notes.
