# BrandGuard Round 3 technical summary

This document summarizes the implementation in the repository. It describes code and local validation, not a claim that the latest local commit is deployed.

## Application architecture

BrandGuard is a server-rendered Django application. `config/` owns environment-backed settings, root URLs, middleware, WSGI/ASGI entry points and safe request logging. `accounts/` supplies session login by username/email and account settings. `workspaces/` resolves the active workspace membership and role on the server and provides the overview and read-only analytics. `brand_sources/` stores workspace-owned sources, immutable versions, structured rules/facts, private import records, and publication/policy-snapshot services. `content_reviews/` stores submissions, immutable text versions, deterministic review runs/checks/findings, comments, resolution events, review readiness and the derived activity timeline. `approvals/` stores approval requests and immutable reviewer decisions. `ai_analysis/` stores separate advisory runs/findings and contains both the optional provider adapter and the explicitly simulated Demo AI adapter.

The core record flow is:

```text
Brand Sources and structured rules/facts
  -> approved source versions and workspace policy snapshot
  -> immutable submitted ContentVersion
  -> deterministic ReviewRun pinned to content and policy
  -> evidence-backed ReviewFindings
  -> version/finding comments and append-only resolution events
  -> approval request for the exact current reviewed version
  -> human reviewer decision bound to content hash and policy snapshot
  -> TXT/PDF export revalidates current approval on every request
```

## Persistence and provenance

Content versions are append-only and have their own content hash. A deterministic review stores its content identity, policy revision/hash, exact approved source-version pins, completed checks and findings. Finding evidence links to actual structured rules/facts and source versions; excerpts are checked against saved approved source text. Historical reviews and decisions remain available when content or policy changes.

An approval is not a mutable submission flag. Its request and human decision identify one content version/hash, review run, policy revision/snapshot and reviewer. A later content version or active policy change makes the old decision historical/stale while preserving it. TXT/PDF export routes call the current-approval service again and block stale or missing approval; UI state is not treated as authorization.

## Authorization

Authentication uses Django sessions. Each workspace request resolves an active membership server-side; view querysets are scoped to that workspace. Creator views additionally scope content to ownership. Reviewers/admins may use the approval inbox and decision actions, with self-approval prohibited; brand managers can manage approved source policy and inspect permitted workspace records but do not receive approval authority by default. Creator, reviewer, manager and admin permissions are enforced on routes/services, not hidden links. Mutations use POST and CSRF protection.

## Deterministic review

The deterministic engine evaluates only saved structured policy: prohibited terms, required terms, triggered disclaimers and a narrow structured ApprovedFact comparison. It persists a check outcome and any finding with validated source evidence. Unsupported or incomparable claims are left unverified or sent for human review rather than presented as proven false. Deterministic findings are the authoritative rules-first check in this product; they are not universal legal or factual verification.

## Collaboration and resolution (Round 3 commitment #2)

Version-level and finding-specific `ReviewComment` records preserve author, role-at-posting, timestamp, workspace and content-version context. Comments are append-only. `FindingResolutionEvent` records each effective state transition with actor, previous/new state, note and time; `ReviewFinding.resolution_status` is a convenience projection of the latest state. States are `OPEN`, `ACKNOWLEDGED`, `RESOLVED` and `WONT_FIX`. Resolution is an operational collaboration annotation: it never deletes/reclassifies a finding, creates approval, waives a high-severity issue or changes export eligibility. New content versions receive separate review findings/history.

## Analytics and workflow guidance (Round 3 commitment #3)

`/app/analytics/` computes scoped counts and breakdowns from persisted submissions, deterministic runs/findings, approval requests/decisions and resolution history. Creator scope is limited to the creator's own records; reviewer and management roles see workspace aggregates allowed by their memberships. Time windows filter persisted timestamps. There are no fabricated benchmarks, compliance scores or cached analytics records. Review Readiness is a read-only view model derived from current policy, reviews, findings, approval state and export eligibility. The submission Activity timeline reconstructs recent lifecycle events from existing timestamped records; it is not a separate audit-log source of truth.

## AI boundary and Round 2 commitment #1

Real AI is optional, user-initiated, disabled by default and requires explicit external-processing consent. The OpenAI Responses adapter requests strict structured output; BrandGuard validates returned fields, content spans and cited approved source/version/evidence before persisting separate advisory findings. AI runs do not change deterministic review, workflow state, approval decisions or export validity. Provider failures are recorded/displayed separately and do not substitute for deterministic results.

Demo AI uses repository-controlled output, reports provider/model as `demo` / `simulated-brandguard-demo`, passes citations through the source/evidence validation path, and makes no external provider request. It is clearly labelled simulated. A controlled real-provider request previously reached the provider but was rate-limited because the account had no usable credits. Therefore implementation exists, but a successful real-provider evaluation remains externally blocked/unverified; simulated output does not satisfy that validation commitment.

## Hardening and duplicate actions

Creation/revision/import/comment/resolution/review/AI/approval flows use existing request tokens, unique constraints, transactions and stale-state checks where appropriate. State changes use POST/CSRF and redirect to stable GET pages. These mechanisms protect ordinary retries and duplicate form submissions; they are not a claim of universal exactly-once behavior across all infrastructure failures. SQLite is the local/test database and does not provide PostgreSQL's effective row locking. The test suite does not prove production PostgreSQL concurrency semantics.

## Tests and deployment

The repository uses Django's test runner with SQLite. At the Phase 7F local clean-clone checkpoint, 301 tests pass. That clean-room check installed `requirements.txt` into a new Python 3.14.6 virtual environment, applied migrations from an empty SQLite database, seeded the synthetic demo workspace and six real review scenarios, ran `check`/`pip check`, and started the local server for health/login GET checks. This does not establish PostgreSQL concurrency or live Render behavior.

Production configuration targets Render, PostgreSQL, Gunicorn and WhiteNoise. `build.sh` installs requirements, collects static files and applies migrations; it does not seed accounts. Source-document uploads are disabled in production unless persistent private S3-compatible storage is configured. See [DEPLOYMENT.md](DEPLOYMENT.md), [REVIEW_ENGINE.md](REVIEW_ENGINE.md), [APPROVAL_WORKFLOW.md](APPROVAL_WORKFLOW.md), [EXPORTS.md](EXPORTS.md) and [AI_INTEGRATION.md](AI_INTEGRATION.md) for operational detail.

## Known boundaries

- The local SQLite suite is not a PostgreSQL concurrency integration test.
- Successful real-provider AI output has not been validated; API credits/model access are required for a separately authorized request.
- Demo AI is simulated and must not be represented as live model output.
- Scanned-document OCR, video/audio analysis and universal internet fact checking are not implemented.
- Production original-document imports require persistent private object storage; otherwise the UI disables them and manual source authoring remains available.
- Submission Activity is reconstructed from application records and is not tamper-proof audit logging. Export downloads are not persisted as activity events.
- Browser automation is not configured; production workflow/visual verification requires a separate manual or browser-test pass.
