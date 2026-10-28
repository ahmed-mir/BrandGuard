# Phase 3A review engine

BrandGuard Phase 3A accepts plain-text marketing content and performs bounded deterministic checks against approved brand policy already stored by `brand_sources`. It does not call an AI or external provider. A result reflects only the checks described below; it is not a general factual-accuracy or brand-compliance guarantee.

## Stored records

- `ContentSubmission` identifies the workspace, creator, title, channel, optional audience and campaign purpose. It points at the current saved content version and is retained as review history.
- `ContentVersion` stores the complete submitted text, a SHA-256 content hash, creator, version number and timestamp. Saving a version is immutable. Phase 3B revisions append another version; the evaluated text and findings are never overwritten.
- `ReviewRun` stores the requested actor, status and timestamps, engine version, policy revision/hash, a JSON copy of the Phase 2A snapshot, a unique request token, safe failure information and incomplete/not-implemented check metadata. Terminal runs cannot be changed or deleted through model methods.
- `ReviewPolicyVersion` stores each approved `SourceVersion` identity and its content hash from the snapshot. Future-effective source versions are kept in the snapshot with `applied=False` and a reason; rules from them are not evaluated.
- `ReviewCheck` stores one outcome per saved rule. Outcomes include no violation, finding, supported, contradicted, not verified, not applicable, needs human review, not completed and failed.
- `ReviewFinding` exists only for a real result against submitted content. It retains the `BrandRule`, exact `SourceVersion`, check/run, finding type and origin, optional classification, exact matched text and content offsets (or explicit location unavailable), explanation, evidence excerpt and only a configured correction where one exists. Deterministic identity/evidence fields stay immutable. The current operational `resolution_status` is a separate convenience state whose append-only events are described below; it does not change or dismiss the finding.

Foreign keys use protective deletion behavior. Uniqueness constraints prevent duplicate content-version numbers, duplicate runs for a workspace/request token, duplicate rule checks per run and duplicate source pins per run. Workspace and execution indexes support review-list and result queries.

## Execution and snapshot behavior

`content_reviews.services.create_submission()` validates the creator's active workspace membership, trims the content, enforces the channel and 20,000-character limit, and atomically saves the submission plus its immutable first version. The application form adds user-facing validation.

`execute_review()` starts a run through `start_review()`. In a short database transaction, it locks the active workspace row, rechecks that the actor has an active creator membership and that the submission is theirs, and captures the existing `brand_sources.services.policy_snapshot(workspace)` plus `active_policy_versions(workspace)`. It stores revision/hash and relational pins before evaluating rules. This serializes snapshot capture with the existing source-approval service, which also locks the workspace during atomic publish/supersession. Rule evaluation then queries only the stored applied source-version IDs. Approved `SourceVersion` text/rules/facts are immutable; if the active set changes afterward, past checks remain tied to the saved version IDs and content hashes. The result page compares the pinned revision/hash to today's snapshot only to display that policy has since changed.

Effective dates are interpreted as start dates. A future-effective approved source version remains visible as a policy pin but its rules are excluded. A future-effective `ApprovedFact` gets a not-applicable check and cannot be used to classify a claim before its effective date. The schema does not model source/fact expiry, so expiry is not evaluated. If no approved source exists, or all approved sources are future-effective, the run is `FAILED` with a clear blocking reason and no policy checks. A policy snapshot with no structured rules can complete with zero checks; the UI explicitly says that no compliance conclusion was made.

The run uses a UUID idempotency token provided by its start form. Repeating the same request with the same token and same actor/content returns the existing run. A separate user action/token creates a distinct historical run. Processing is synchronous and bounded to 500 rules, 250 findings, and 20,000 input characters. Completed checks/findings are saved individually; if a check or persistence step fails, the valid prior results remain and the run becomes `PARTIAL` when any check completed, otherwise `FAILED`. Remaining work is recorded. A partial or failed result must not be presented as a pass.

Content submission forms also carry a persisted request token scoped to the workspace and creator. Reposting the same form returns the original immutable version 1; reusing that token for different content is rejected. Successful mutations use POST/redirect/GET, so refreshing the result page does not replay the mutation. A deliberate new review form has a new token and creates a distinct historical run; a retry of the same form reuses its run.

## Implemented rule evaluation

All evaluation uses the validated matcher schema already saved by Phase 2A. The engine never interprets source prose as code, accepts user-defined expressions, or contains LumaDesk-specific matching logic.

### `PROHIBITED_TERM`

Uses escaped literal phrase matching with Unicode word-character boundaries, configured case sensitivity, and optional saved channel scope. Every match yields the exact submitted substring and start/end offsets. A preferred term is suggested only if the approved matcher stores one. Word boundaries prevent the phrase from matching inside a larger word.

### `REQUIRED_TERM`

Checks the saved term only within the configured channel scope. Presence yields a no-violation check. Absence becomes a finding only where a concrete channel scope or a configured content trigger makes applicability explicit. A broad `ALL` rule without a trigger does not treat absence as a violation; it is marked not applicable. Missing-term findings have no exact location in the submitted content, so the record says location unavailable and suggests only the configured required term.

### `REQUIRED_DISCLAIMER`

Checks the saved disclaimer phrase only in its allowed scope. A missing-disclaimer finding is produced only when its configured trigger phrase is present and the required phrase is absent. If a legacy rule has no trigger, an absent disclaimer is not declared a violation; the check is marked as needing human review. If an explicit trigger is absent, the check is not applicable. This does not infer claims or disclaimer applicability semantically.

### `APPROVED_FACT`

Requires a real linked `ApprovedFact` on the exact pinned source version. The current evaluator supports only narrow structured numeric comparisons: the submitted sentence must contain an exact shared metric phrase derived from at least two contiguous subject/statement tokens; the fact must have a numeric comparison operator/value and unit; the submitted claim must use an explicit matching unit and matching direction wording. An exact approved statement can be marked supported. For `UP_TO`, an above-bound comparable number can be marked contradicted. The same maximum can be supported only when its “up to” qualifier is present. Lower point claims under an upper bound are not established by that evidence and remain `NOT_VERIFIED`. Supported `AT_LEAST`, `EXACTLY` and `RANGE` comparisons use the stored numeric boundary where metric, unit and direction are explicit.

The numeric bound comparison currently supports `UP_TO` only. Other saved operators are not used to classify a non-exact claim. Mismatched units, absent quantities, unsupported comparators or ambiguous direction are not treated as false; they remain not verified or need human review. Sentences with explicit negation near a comparable metric are routed to human review rather than treated as support. The engine does not equate synonyms such as productivity with task completion speed, infer products from context, check general truth, validate arbitrary statistics, or establish causation. No finding is generated when no claim matching a fact's metric is found.

## Evidence validation

Before evaluating a rule, the service confirms that its source version belongs to the run's applied pin set, its stored content hash matches the pinned version, its exact evidence excerpt appears in that immutable source text, and its matcher passes the existing allowlist validator. An approved-fact rule must link to a fact from that same source version, whose own exact excerpt matches both source text and rule evidence.

Each persisted finding is also validated by the model: check, rule, run and source version must agree; the source must be an applied version in the pinned snapshot; evidence must equal the rule's excerpt and occur in the pinned source text; factual evidence must equal the linked fact excerpt; and available content offsets must return precisely the stored matched substring from the immutable `ContentVersion`. A failed validation creates no finding. The corresponding check is recorded as failed where possible and the run becomes partial/failed. No generated, guessed or fallback citations are used.

## Result presentation and security

Submission and review endpoints resolve workspace and role from the active server-side `WorkspaceMembership`. Creators can create/run and see only their own submissions. Reviewers, brand managers and admins can inspect the current workspace. Only creators can access create/run endpoints; role-based hiding is not the protection. Cross-workspace or another creator's submission IDs are scoped out. All mutations use POST and the standard Django CSRF middleware. User copy and source evidence are rendered with normal template escaping. The UI clearly distinguishes findings, non-findings, not-verified checks, human review, not-applicable checks, partial runs and failed runs; it displays no compliance score or fake AI state.

## Phase 3B relationship

Phase 3B adds creator revisions and approval decisions without changing the deterministic evaluator. A revision creates another immutable `ContentVersion`; all `ReviewRun`, check, finding and evidence rows stay linked to the version actually evaluated. The newest version must receive a new complete run before an approval request can be submitted. The request pins the review run and that run's exact policy snapshot. Reviewers see findings and evidence through the approval detail page; approval does not alter the finding records.

## Discussion and resolution history

Version-level `ReviewComment` records and finding-specific comments are append-only, workspace-scoped and bound to the exact `ContentVersion`. Feeds display oldest first. `FindingResolutionEvent` records each allowed state transition with actor, role-at-action, timestamp and optional note. The latest state is mirrored in `ReviewFinding.resolution_status`; the event stream is the audit history. New content versions never inherit old finding state/history. Operational status never changes the saved deterministic classification or evidence and never creates an approval. High-severity findings continue to require the existing documented override even when marked acknowledged, resolved or won't fix. See `docs/APPROVAL_WORKFLOW.md` for transitions and role rules.

See `docs/APPROVAL_WORKFLOW.md` for request/decision models, state transitions, review freshness, policy invalidation, high-severity overrides, role boundaries and database concurrency assumptions.

## Current limitations

- Plain text only; no campaign file upload, image, OCR, scanned text, PDF/DOCX parsing, formatting analysis or asset review.
- Synchronous execution; no queue, cancellation, retry dashboard or streaming progress. Input and result limits bound the work.
- Deterministic checks remain independent from optional AI advisory analysis. AI does not change these runs, findings, submission state, approval eligibility or export validity. See `docs/AI_INTEGRATION.md` for Phase 5A behavior.
- No semantic deterministic rules, paraphrase matching, synonym resolution, general factual verification, campaign content uploads, automatic suggested-text replacement, external notifications or tamper-proof auditing.
- Required-term and disclaimer applicability is only as good as the explicit saved scope/trigger. If policy configuration is too broad or ambiguous, the engine will not manufacture a violation.
- Approved facts use a deterministic, narrow comparison subset and can classify only the supported quantity/metric combinations. `SUPPORTED` means the claim text matches that stored structured statement; it does not prove that the underlying statement is externally true.
- Future-effective sources are excluded; expired-source semantics do not exist in the source schema.

## Future adapter boundary

There is no AI adapter or AI-originated finding in Phase 3A. A future judgement provider should be integrated as a separate explicit check interface, consume only the immutable content version and saved pinned policy evidence, return a typed result with verified source/version references and location metadata, and be recorded with an origin that clearly distinguishes it from deterministic checks. It must not overwrite deterministic results or pretend an unverified citation is valid. Provider failures should produce incomplete/failed status and preserve completed deterministic checks. Such an integration must be separately scoped, reviewed and tested before any provider is called.

## Local commands

From the repository root in PowerShell, activate the existing environment and set the documented local environment variables:

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

Open `/app/reviews/` after signing in as `lumadesk_creator`. Run the checks with:

```powershell
python manage.py check
python manage.py makemigrations --check
python manage.py test
python -m pip check
git diff --check
```
