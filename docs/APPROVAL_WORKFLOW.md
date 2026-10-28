# Phase 3B: content revision and human approval

This document describes the persisted workflow for revising marketing content and asking a human reviewer to decide on one exact reviewed version. Source-policy publication remains a separate operation. Phase 4A adds a distinct export gate: only a currently valid approval for the current content and policy can be downloaded.

## Records and identity

`content_reviews.ContentVersion` remains immutable. Its `content_hash` is SHA-256 of the full UTF-8 text. A revision records `revision_of` and a unique, per-submission `revision_idempotency_key`; it receives the next version number and becomes the submission's current version only if the submitted base version ID and hash are still current. A database uniqueness constraint guards both version number and idempotency key. Previous versions, review runs, findings and evidence remain attached to the version actually saved/reviewed.

`approvals.ApprovalRequest` records the current workspace and submission, exact `ContentVersion`, its hash, one `ReviewRun`, the submitting creator/time, and a copy of the review's policy revision, snapshot hash and full snapshot. The snapshot contains the exact approved `SourceVersion` IDs and their hashes. One request is allowed for a saved content version, preventing double submission and requiring a new revision after changes are requested.

`approvals.ApprovalDecision` is an immutable one-to-one decision for one request. It records the reviewer, action, timestamp, request-changes comment or approval override reason, content hash and policy snapshot identity. For an override it also records the exact high-severity finding IDs. It does not mutate or resolve those findings.

There is no separate `SourceSnapshot` table. The exact active source-version set is represented by the existing `ReviewRun.policy_snapshot` JSON plus its relational `ReviewPolicyVersion` rows; request and decision records copy that immutable identity and retain the exact `ReviewRun` foreign key. This is the implementation's PRD deviation from a possible standalone snapshot ID, not a loss of source-version traceability.

## State and allowed transitions

Submission state is resolved from the latest request tied to its current version; it is not accepted from client input and is not a manually editable `ContentSubmission.status` field.

| Effective current-version state | Persisted condition |
|---|---|
| `DRAFT` | No request exists for the current version, or the only historical approval is now stale |
| `IN_REVIEW` | A current-version request awaits a decision |
| `CHANGES_REQUESTED` | The request has a recorded changes-requested decision |
| `APPROVED` | The request has an approval and its exact content and policy identity still match current records |

Allowed path: `DRAFT -> IN_REVIEW -> CHANGES_REQUESTED -> DRAFT` after a new immutable version, fresh review and new request; or `DRAFT -> IN_REVIEW -> APPROVED`. A current approved version that is edited or whose policy changes becomes effectively `DRAFT` with a visible stale-approval explanation; its historical request/decision remains `APPROVED` as a historical action, never as a current authorization. A request cannot be decided twice. No content is approved directly without a fresh completed review.

Suggested wording on deterministic findings is displayed as copyable text only. Automatic span replacement is deferred to avoid applying stale offsets or presenting a suggested correction as approved language.

## Revision and comparison

The creator revision form loads the latest saved text. On POST, the service locks the submission where supported, checks creator ownership and active membership, compares base version ID and content hash with the current pointer, validates non-empty text and the 20,000-character bound, and appends a new numbered version with a newly calculated hash. The current-version pointer moves in the same transaction. A matching idempotency retry returns the same record only when base and text hash also match. Stale submissions or conflicting writes receive a retry message; earlier text is never overwritten.

Initial submission forms use a persisted workspace/creator-scoped request token: an identical retry returns the original version 1, while a changed payload under the same token is rejected. Brand-source create/import forms use the same pattern scoped to workspace and owner. Imported-text confirmation includes the hash displayed to the user; confirmation is rejected if the saved source text changed after the form was opened. Repeating confirmation for the same hash is a no-op and does not rewrite its confirmation timestamp.

The comparison route reads two versions from the same workspace-scoped submission. Python's standard `difflib.SequenceMatcher` produces an escaped line comparison. User content is rendered with Django template autoescaping, not trusted markup or `innerHTML`.

## Fresh review requirements

Before a creator can submit for approval, the service rechecks that:

1. The actor is an active `CREATOR` membership and owns the submission.
2. The selected content version is the submission's current version.
3. The selected review belongs to that workspace and exact content version.
4. The review status is `COMPLETE`, it contains saved checks, and no check is `FAILED` or `NOT_COMPLETED`.
5. The stored policy revision, hash and complete set of source-version identities match the current active approved policy.
6. No request already exists for that content version.

Partial, failed, empty-check, other-version and stale-policy runs cannot be submitted. If a source is published after review, the creator must run the engine again and create a fresh request. A request becoming stale after submission is preserved and visibly marked, but cannot receive a reviewer decision.

## Reviewer decisions and high-severity override

Only active `REVIEWER` and `ADMIN` membership can access the approval inbox and decision services. `BRAND_MANAGER` may inspect workspace request details as part of workspace review history, but cannot use the inbox or decide. Creators can inspect their own request/decision history only. A reviewer/admin is always denied when their user ID is the submitting creator, even if a later membership design were to give that person more than one authority.

**Request changes** requires a 10–3,000 character explanation. It stores the reviewer and time and changes the request to `CHANGES_REQUESTED`; the creator sees the comment and version it refers to. A new content version and fresh review are required before the creator can submit again.

**Approve version** runs in a transaction and rechecks workspace permission, self-approval, pending state, current version ID, SHA-256, exact complete review, current policy revision/hash/source IDs, and absence of a previous decision. The decision binds the request's content and policy snapshots. If the exact review has any high-severity findings, approval is blocked unless the reviewer supplies a meaningful reason (at least 10 characters). The decision records the override reason and the high-finding IDs. Findings are not hidden, dismissed or removed by the override. No override is needed when there are no high-severity findings.

Both actions are POST-only and rely on Django CSRF middleware. The request's one-decision constraint and transaction locks prevent a second decision from replacing the first. Database errors under SQLite contention are returned as a safe retry instruction when detected.

## Current approval validation

`approvals.services.current_valid_approval(submission)` returns `{valid, approval, decision, reason}`. It selects the latest historical approved request and checks that its content version is still current, the saved text recomputes to the approved SHA-256, the decision and review bind the same content and policy identity, the review is complete and attached to that exact version, the request/decision snapshots match the active workspace policy, and any high-severity override IDs still match the original findings. Operational resolution status does not change this identity check. Phase 4A export requests call this service every time; a displayed status alone is not sufficient.

Reasons distinguish no approval, content changes, content-hash mismatch, policy changes and invalid/missing review. A new version or policy snapshot leaves every prior request, decision, review and finding intact, while making the older approval fail the current validation.

## Role matrix

| Role | Revise own submissions | Submit own current reviewed version | View approval requests | Inbox | Decide |
|---|---:|---:|---:|---:|---:|
| Creator | Yes | Yes | Own only | No | No |
| Reviewer | No | No | Current workspace | Yes | Request changes / approve, except own authored content |
| Brand manager | No | No | Current workspace details | No | No |
| Workspace admin | No | No | Current workspace | Yes | Request changes / approve, except own authored content |

Every data query uses the active server-resolved workspace. Creator submission access additionally filters by owner. No endpoint trusts a client-supplied workspace, role, status, snapshot or approval flag.

## Routes

- `GET/POST /app/reviews/<submission-id>/revise/` — create an immutable creator revision.
- `GET /app/reviews/<submission-id>/compare/?from=<n>&to=<n>` — compare saved versions.
- `POST /app/reviews/<submission-id>/submit/` — create an approval request for the current version and selected fresh review.
- `GET /app/approvals/` — reviewer/admin inbox.
- `GET/POST /app/approvals/<request-id>/` — inspect the exact request and submit a request-changes or approve decision. `GET` never performs a decision.
- `POST /app/reviews/<submission-id>/versions/<version>/comments/` — append version-level discussion.
- `POST /app/reviews/<submission-id>/versions/<version>/findings/<finding-id>/comments/` — append finding-specific discussion.
- `POST /app/reviews/<submission-id>/versions/<version>/findings/<finding-id>/resolution/` — append a finding resolution transition.
- `GET /app/reviews/<submission-id>/export/txt/` and `/export/pdf/` — Phase 4A protected downloads for the current valid approved version only. See `docs/EXPORTS.md`.

## Concurrency, audit and limitations

On PostgreSQL, actions lock the workspace, submission and approval request rows in a consistent order. This serializes against the source-publication service (which locks the workspace), revision creation and competing reviewer decisions. Unique constraints remain the final protection against duplicate content versions, idempotency keys, requests and decisions.

SQLite does not implement effective `SELECT ... FOR UPDATE`; it is a local development database, not a concurrent multi-reviewer production guarantee. Constraints prevent silent overwrite/duplicate records and lock errors prompt a retry, but PostgreSQL must be configured and concurrency-tested before production use. This project does not claim tamper-proof audit storage: application-level immutable model methods and protected records are not a substitute for external append-only audit infrastructure.

## Collaborative discussion and finding resolution

`content_reviews.ReviewComment` stores append-only comments against an exact content version, optionally linked to a finding from that version. Comments include author, role snapshot, workspace, timestamp and an idempotency token. General discussion and each finding thread are displayed oldest first. Posting the same request token again returns the existing comment; comments cannot be edited or deleted.

`content_reviews.FindingResolutionEvent` is an immutable transition record with finding, original content version, workspace, actor/role, previous and new state, optional note, timestamp and request token. The convenience `ReviewFinding.resolution_status` displays the latest state. The allowed transitions are `OPEN -> ACKNOWLEDGED | RESOLVED | WONT_FIX`, `ACKNOWLEDGED -> OPEN | RESOLVED | WONT_FIX`, and `RESOLVED | WONT_FIX -> OPEN`. Repeating the current state is a safe no-op. `RESOLVED` requires a note; `WONT_FIX` requires a reason of at least 10 characters. A creator may acknowledge a finding on their own submission and mark it resolved only after saving a newer content version; creators cannot mark it `WONT_FIX`. Reviewers, brand managers and workspace admins may use the permitted transition graph in their workspace.

These statuses are operational collaboration annotations. They do not change the finding's original classification, matched text, offsets or evidence; do not dismiss a rule result; do not create approval; and do not count as approval or waiver. A later content version gets its own review findings and independent resolution history. Every high-severity finding in an approval's exact review still requires a documented reviewer override, whatever its collaboration status. Self-approval, stale-policy/content checks and export eligibility remain unchanged.

The workspace lock serializes resolution updates on PostgreSQL. SQLite does not provide equivalent row-lock concurrency guarantees. Requests include an idempotency token and expected prior status to reject stale forms; database uniqueness is the final duplicate-event guard. No PostgreSQL concurrency integration test is claimed by the SQLite suite.

Export remains limited to the current approved version in TXT/PDF form, uses the saved records, and does not create a signature or separate export event record. There are no notifications, automatic correction application or separate multi-stage reviewer chain. No AI or universal factual verification is involved. See `docs/REVIEW_ENGINE.md` and `docs/EXPORTS.md` for limitations.

## Local Windows PowerShell commands

From the repository root (with dependencies installed and a stable local `DJANGO_SECRET_KEY` configured):

```powershell
& .\.venv\Scripts\Activate.ps1
$env:DJANGO_SECRET_KEY = [Environment]::GetEnvironmentVariable('DJANGO_SECRET_KEY', 'User')
$env:DJANGO_DEBUG = 'true'
$env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
python manage.py migrate
python manage.py seed_demo
python manage.py seed_review_scenarios
python manage.py runserver
```

Open `/accounts/login/`. Sign in as `lumadesk_creator` to create, review, revise and submit content. Sign in as `lumadesk_reviewer` to inspect the approval inbox. Local demo passwords are read from the documented demo environment variables; `seed_demo` prints newly generated local credentials only when it creates those accounts. Never reuse local credentials in a deployed environment.

Run all checks:

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py migrate
python manage.py test
python -m pip check
git diff --check
```
