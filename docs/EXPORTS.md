# Phase 4A: approved content exports

Phase 4A provides two private downloads for a submission's **currently valid, human-approved current version**:

- `GET /app/reviews/<submission-id>/export/txt/` — the unchanged `ContentVersion.content_text` encoded as UTF-8 (`text/plain; charset=utf-8`).
- `GET /app/reviews/<submission-id>/export/pdf/` — an in-memory, paginated BrandGuard report generated with ReportLab.

The routes are GET-only and do not create or store public files. Both call `content_reviews.exports.approved_export_for()` on every request. Rendering links in a page are only a convenience; direct URL access runs the same checks.

## Eligibility and identity checks

The export service resolves the authenticated user's sole active workspace membership on the server, locks the active workspace and membership where supported, scopes the submission to that workspace, and permits creators to export only their own submissions. Reviewers, brand managers and workspace admins can export current-workspace submissions. A missing/cross-workspace submission or another creator's private submission returns 404; missing membership is denied by the existing workspace permission boundary.

`approvals.services.current_valid_approval(submission)` remains the source of truth. It must return `valid=True`, and the export service additionally confirms the exact approved request and immutable decision, current `ContentVersion`, actual SHA-256 of its UTF-8 text, exact linked `ReviewRun`, `COMPLETE` status, policy revision/hash/snapshot agreement, and no failed or not-completed checks. The current policy snapshot must still equal the saved snapshot. Each check and finding must belong to that applied pinned policy set. Every finding's rule, source version and excerpt is checked against the immutable approved source text; available content offsets are checked against the exported version.

Missing or stale approval responses return HTTP 409 with a clear explanation. A version or policy change blocks both formats, even if the old request remains historically `APPROVED`. No request accepts a version ID or workspace ID from the browser. The TXT route always resolves the current version from the submission.

`current_valid_approval()` also validates that decision content and policy identities match the request and review. If the decision contains a high-severity override, its recorded finding IDs must still exactly match the original review's open high-severity findings and the reason must remain meaningful. Findings stay open. The PDF discloses each original finding, its evidence, the rationale and the human decision maker. No override is represented as a finding dismissal.

## Permissions

| Workspace role | Export permission | Scope |
|---|---|---|
| Creator | Yes | Own submissions only |
| Reviewer | Yes | Any eligible submission in the current workspace |
| Brand manager | Yes | Any eligible submission in the current workspace |
| Workspace admin | Yes | Any eligible submission in the current workspace |

Authentication and active membership are checked for each HTTP request. The creator role additionally checks ownership. The four allowed roles come from the existing `WorkspaceMembership`; no client-supplied role or identifier is trusted. Export endpoints do not mutate workflow state and require no CSRF token because they are safe GET downloads.

## TXT format

The TXT response body is exactly the approved version text as UTF-8. It does not prepend a report header or alter whitespace, punctuation or claims. It uses a generated ASCII slug filename (`brandguard-<safe-slug>-v<number>.txt`) and `Content-Disposition: attachment`. The only input to the slug is normalized with Django's ASCII `slugify`; numeric version output comes from the database.

## PDF report contents

ReportLab generates the PDF in memory. The report identifies:

- Submission UUID, workspace name, channel, content-version number and UUID, full content hash, creation timestamp and generation timestamp.
- Approval request UUID, human decision UUID/action, reviewer identity and approval timestamp.
- ReviewRun UUID/status/engine version, policy revision and full snapshot hash.
- The applied approved source/version UUIDs, source names, version numbers and source content hashes.
- Actual saved deterministic check outcomes and summaries.
- Real findings by severity, open status, matched content, explanation, any configured suggested wording, and the exact verified rule/source evidence.
- Any recorded high-severity override with the original finding and evidence, reason and decision maker.
- Explicit limits: the report is not legal clearance, universal factual verification, accessibility certification, or tamper-proof proof of approval; no AI analysis was performed.

An empty findings list is stated as an actual absence of finding rows and not as universal compliance. Persisted `not_implemented_checks` or `incomplete_checks`, if present, are stated as limitations rather than passed checks. The underlying approval currently requires a complete review; unsupported or not-applicable checks remain shown with their actual outcome.

User- and policy-supplied text is escaped as plain text before ReportLab paragraph rendering. The embedded ReportLab Vera fonts support the documented English/Latin-language MVP. A character the font cannot encode returns a clear HTTP 422 response instead of silently dropping it; the exact UTF-8 TXT export remains available. Paragraph wrapping and Platypus pagination support multi-page content and evidence. No layout markup is accepted from content.

The report is a generated record, not a signed certificate, cryptographic authenticity mechanism, external timestamp or tamper-proof audit artifact. It does not prove that the approved content was published.

## Response and cache controls

Files are rendered in memory. Responses set attachment disposition, `Cache-Control: private, no-store, no-cache, max-age=0, must-revalidate`, `Pragma: no-cache` and `X-Content-Type-Options: nosniff`. No export file is written to `MEDIA_ROOT`, static paths, or public storage. PDF failures are logged with a submission ID and exception class only, not the document or evidence text, and return a generic safe error page. Unexpected rendering errors do not return a partial PDF.

There is no export-event audit model in Phase 4A. The PDF contains persisted source-record identifiers for authorized human traceability, but BrandGuard does not claim tamper-proof auditing.

## Dependency and local setup

Runtime dependency: `reportlab>=4.2,<5.0`. It is used only for PDF report generation. It is installed with the rest of the application:

```powershell
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No database models or migrations were added in Phase 4A. Run the app with the same environment configuration documented in `README.md`:

```powershell
$env:DJANGO_SECRET_KEY = [Environment]::GetEnvironmentVariable('DJANGO_SECRET_KEY', 'User')
$env:DJANGO_DEBUG = 'true'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
python manage.py migrate
python manage.py runserver
```

With current approval records available in the local database, open the submission's current version page to use the downloads. Run verification with:

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py test
python -m pip check
git diff --check
```

## Concurrency and deployment limits

On PostgreSQL, the export eligibility lookup locks the workspace, membership and submission while it reads the approval and review identity. This coordinates with policy publication and content revision services that lock the relevant records. The PDF is generated from the materialized records after eligibility is resolved. SQLite does not implement effective `SELECT FOR UPDATE`, so local SQLite is not a concurrent export/approval consistency guarantee. Configure PostgreSQL and run concurrency tests before production.

This phase is not production deployment readiness. TLS, PostgreSQL operations, rate limits, monitoring, retention, backup policy, export event auditing, report signing and private artifact storage are out of scope.
