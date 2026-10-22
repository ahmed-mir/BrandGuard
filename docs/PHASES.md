# BrandGuard delivery phases

BrandGuard is developed in small, reviewable slices. Each phase must describe only behavior that is implemented and working.

## Phase 0 — project and product foundation (implemented)

- Django modular monolith, environment-based settings, SQLite default and PostgreSQL URL support.
- Public landing page describing the creator-to-reviewer concept, with a clearly illustrative synthetic LumaDesk preview.
- Shared visual tokens, responsive controls, and reusable application shell.
- Health response, static-file configuration, error templates, smoke tests and setup documentation.

## Phase 1A — authentication and workspace foundation (implemented)

- Django built-in session authentication using the built-in user model and password hashing; username and case-insensitive email login are supported.
- CSRF-protected login form and POST-only logout, with same-host safe post-login redirects.
- Persistent `Workspace` and `WorkspaceMembership` models, including creator, brand manager, reviewer and workspace admin roles and a database uniqueness constraint per user/workspace.
- `/app/` resolves a single active membership from the authenticated user on the server. Anonymous, inactive and ambiguous workspace access is denied; role decorators provide a server-side authorization foundation.
- Responsive login experience and authenticated workspace welcome page showing real identity, workspace and role. No dashboard activity or business data is fabricated.
- A repeatable `seed_demo` command provisions synthetic LumaDesk accounts only in DEBUG + local SQLite, without overwriting unrelated accounts or resetting existing passwords.

### PRD reconciliation

The task requested `docs/BrandGuard_Round2_PRD.pdf`; that path does not exist. The available document, `BrandGuard_Round2_Product_Requirements_Document.pdf`, was present in the repository root and was successfully extracted using a development-only `pypdf` install. `pypdf` was not added to application requirements.

The extracted requirements relevant to this phase call for workspace-scoped creator, brand manager, reviewer and workspace administrator roles, server-side membership authorization, and workspace-scoped product records. They describe a fuller dashboard with review, approval, draft and source-pack information. This phase deliberately follows the narrower Phase 1A task: the welcome page contains only database-backed identity, workspace and role, and the business actions/dashboard data remain unimplemented. The supplied brief's fictional LumaDesk seed aligns with the PRD's seeded workspace direction. No PRD requirement for SSO or public registration was inferred or added.

## Phase 1B — workspace UX and authenticated application experience (implemented)

- Reworked the reusable app shell into a compact desktop sidebar with current workspace and role context, shared page headers, account identity and POST logout.
- Added a small JavaScript mobile navigation drawer with Escape handling, focus return/trapping, a scrim, modal semantics, and a no-JavaScript navigation fallback.
- `/app/` is the role-aware workspace overview. It uses current membership/workspace data and real dates/status, gives role-specific guidance, and labels review activity as unavailable. It has no fake counts or fabricated activity.
- At the end of Phase 1B, future Brand Sources, Content Reviews and Approval Inbox entries were noninteractive and marked unavailable. Phase 2A makes Brand Sources a working route; Content Reviews and Approval Inbox remain unavailable until their later phases.
- Added `/app/settings/profile/`: users may view their username, email, workspace and role; edit only first/last name; and change their password using Django's built-in `PasswordChangeForm`, validators and session hash update. Both forms are POST + CSRF protected and show success/validation feedback.
- Added `/app/members/`: only workspace admins may access the read-only list of actual memberships in their currently resolved workspace. It includes role and active status and does not expose other workspaces.
- All routes use server-side membership/role decorators. No self-service registration, invitation, email/username/role editing, membership mutation, review, guideline or approval function was added.
- Tests cover role-specific overview/navigation, resolvable links, account/profile and password behavior, CSRF, session preservation, member visibility/denial/isolation, plus the existing Phase 0 and 1A flows.

### Phase 1B boundaries

Workspace administration remains read-only. The membership list does not invite, edit, disable or remove members; workspace metadata is displayed to admins but not edited. Account settings do not change email, username or workspace role. Phase 2A adds source-policy management while content review, AI analysis, content approvals, audit-event management and export remain deferred.

## Phase 2A — authored brand sources and policy (implemented)

- Added the `brand_sources` app with workspace-scoped `BrandSource`, immutable `SourceVersion`, structured `BrandRule`, and `ApprovedFact` records. Rules and facts retain exact excerpts from their source version; optional offsets are verified against the full source text.
- Brand managers and workspace admins can create sources/drafts, author typed deterministic rules and facts, inspect version history, and publish a validated draft. Publication is atomic, records approver/time, and supersedes the previous approved version without erasing it. Approved and superseded source text and policy entries are immutable through model saves and application forms.
- Every authenticated workspace role can view published guidance. Only managers/admins can see drafts or make policy changes. All detail, version, evidence and mutation queries are constrained to the server-resolved workspace.
- The library, version detail and editor reuse the responsive application shell. `/app/brand-sources/` is integrated as a working navigation route.
- `Workspace.policy_revision` increments on publication and `policy_snapshot_hash` stores a deterministic SHA-256 hash of the approved policy set. `active_policy_versions(workspace)` supplies the active approved versions; `policy_snapshot(workspace)` returns revision, hash and source/version IDs plus content hashes. A future review record should persist this revision/hash and the exact version IDs when the review is created, then compare those saved values to detect policy changes and query the pinned IDs for historical evidence. Review records and invalidation behavior are not implemented in this phase.
- `seed_demo` includes a synthetic LumaDesk pack with product naming rules, voice guidance, a required “Results vary.” disclaimer and FACT-02 with structured `UP_TO 20 %` comparison data and verbatim evidence. Seeding is idempotent, local-only (DEBUG + SQLite), and refuses collisions rather than overwriting manager-authored content.
- No document upload/extraction, AI analysis, automated general fact-checking, content submission/review, creator revisions, review decisions, audit-event subsystem or export is implemented. Effective dates are metadata; expiration enforcement is deferred.

### Phase 2A role access

| Role | Published sources | Drafts/history | Create/edit drafts | Publish | Workspace member management |
|---|---:|---:|---:|---:|---:|
| Creator | Yes | No | No | No | No |
| Reviewer | Yes | No | No | No | No |
| Brand manager | Yes | Yes | Yes | Yes | No |
| Workspace admin | Yes | Yes | Yes | Yes | Existing read-only member list |

## Phase 2B — brand document ingestion and human verification (implemented)

- Added `BrandDocumentImport`, a provenance record linked one-to-one to an actual `SourceVersion`, with workspace, uploader, timestamp, original filename, validated document type, SHA-256 of the uploaded bytes, extraction outcome, extracted character/page counts, and human confirmation metadata.
- Managers and workspace admins can POST a new document through `/app/brand-sources/import/`. Successful imports create a normal `BrandSource`, editable `DRAFT` `SourceVersion`, and private-file provenance record; then redirect to the existing source editor. No source is automatically approved, and no rules or facts are inferred.
- TXT supports UTF-8 and UTF-8 BOM. DOCX extracts paragraphs and tables in document order after ZIP structure, entry count, expansion size and compression checks. Text-based PDF extracts actual page text and preserves page boundaries. Browser MIME type is ignored; extension and structure/content are validated.
- Default bounds are 5 MiB per upload, 10 PDF pages, 80,000 extracted characters, 2,048 DOCX archive members, 10 MiB per decompressed DOCX member and 20 MiB total decompressed archive content. Password-protected PDFs, malformed inputs, unsupported encodings and files with no meaningful extracted text are rejected. Scanned PDFs are not OCR'd and the UI offers manual text entry instead.
- Original files use the `private_documents` Django storage alias and generated storage names under a local `private_uploads/` directory outside public media/static routes. There is no file-download endpoint. Deployments must configure a genuinely private storage backend and its access controls; this application does not make a public bucket private automatically.
- Imported text is an editable draft. A separate POST confirmation stores the SHA-256 of the reviewed source text, confirmation actor and timestamp. Saving a materially changed source text changes the hash and makes that confirmation stale. `approve_source_version` blocks imported drafts unless the current text hash is confirmed, then applies the existing evidence validation, immutable approval, atomic supersession and policy revision/hash logic.
- The policy service queries imported and manually authored sources through the same `SourceVersion`, `BrandRule` and `ApprovedFact` tables. `active_policy_versions(workspace)` and `policy_snapshot(workspace)` therefore require no parallel document-specific policy logic.
- The PRD root document was available and read with `pypdf`; it calls for text-based DOCX/PDF extraction, 5 MB and 10-page limits, and rejects encrypted/oversize documents. The scanned-document/OCR boundary follows the PRD. No extraction-only dependency was added for reading the PRD; `pypdf` and `python-docx` are runtime requirements because the product feature uses them.

### Phase 2B role access and workflow

| Role | Import source | View imported drafts | Edit/confirm draft | Publish | View approved source/evidence |
|---|---:|---:|---:|---:|---:|
| Creator | No | No | No | No | Yes |
| Reviewer | No | No | No | No | Yes |
| Brand manager | Yes | Yes | Yes | Yes | Yes |
| Workspace admin | Yes | Yes | Yes | Yes | Yes |

Import and confirmation are workspace-scoped and protected by the existing server-side manager/admin decorators. Approval permission is still checked server-side; hiding a control is not treated as authorization. Source edits and approvals re-fetch/lock the draft to avoid changing an already approved version.

## Phase 3A — text submission and evidence-backed review (implemented)

- Added the `content_reviews` Django app. A creator can save plain-text content with channel, optional audience and purpose. The first saved `ContentVersion` stores a SHA-256 hash and cannot be edited or deleted; this phase does not add revisions.
- Added workspace-scoped review history. Creators see and run reviews only for their own submissions. Reviewers, brand managers and workspace admins can inspect submissions in their current workspace. Creation and run actions require creator membership server-side; all detail queries enforce active workspace boundaries.
- Each review stores the `Workspace.policy_revision`, policy snapshot hash, exact approved source-version identities/content hashes, engine version and immutable run status. A short workspace-row lock captures the source snapshot. The evaluator then uses only those pinned, effective source-version IDs; later publication cannot rewrite old checks or evidence.
- The deterministic engine uses the existing Phase 2A matcher schema for `PROHIBITED_TERM`, `REQUIRED_TERM`, `REQUIRED_DISCLAIMER` and `APPROVED_FACT`. It stores per-rule `ReviewCheck` outcomes and `ReviewFinding` records only when a real finding is generated. Exact source evidence must still occur in the pinned source text; matched-content offsets are validated against the immutable submitted version.
- Prohibited phrases use escaped, word-bounded matching and configured case sensitivity. Required terms need a configured channel scope or applicability trigger before absence can become a finding. Required disclaimers run only with their stored trigger. Approved-fact comparisons require a narrow shared metric phrase, explicit matching unit, direction and structured comparison; unsupported claims are `NOT_VERIFIED`, not declared false.
- Runs are synchronous and bounded to 20,000 characters, 500 policy rules and 250 findings. Per-check failures preserve successful results and produce `PARTIAL` or `FAILED` status. No approved policy blocks review. No tone analysis or AI service is connected; the UI says this explicitly. A complete deterministic run is not a universal fact or compliance guarantee.
- Added `/app/reviews/`, `/new/`, submission/version detail, POST-only `/run/` and persisted `/runs/<review-id>/` routes. The responsive findings screen shows actual checks, severity labels, validated text offsets, exact pinned evidence and a warning when the current policy has changed.
- Added a local-only, idempotent `seed_review_scenarios` command. Its six synthetic submissions are passed through the actual engine; no finding records are preloaded. It requires `seed_demo`, DEBUG and SQLite, and skips existing seed-owned submissions/runs.
- Added tests for all four rule types, scope/trigger behavior, metric/unit boundaries, evidence integrity, policy pinning, immutability, no-policy/failed outcomes, permissions/isolation, idempotent seeding and an unmocked end-to-end creator flow.

### Phase 3A role access

| Role | List submissions | Create content | Run review | View findings |
|---|---|---|---|---|
| Creator | Own only | Yes | Own only | Own only |
| Reviewer | Current workspace | No | No | Current workspace |
| Brand manager | Current workspace | No | No | Current workspace |
| Workspace admin | Current workspace | No | No | Current workspace |

### Phase 3A limitations

Only pasted text is supported. There are no later content revisions, creator issue-resolution workflow, reviewer approval/rejection decisions, export, AI judgement, tone assessment, synonyms, semantic brand analysis or universal fact verification. Factual comparison recognizes only the documented structured numeric subset; unrecognized or non-comparable claims remain not verified or need human review. Effective dates are applied as start dates; source policy does not model expiry. This phase does not call external services.

## Phase 3B — creator revision and human review decisions (implemented)

- `ContentVersion` remains immutable. Creator revisions use an explicit base-version ID and content hash, are serialized against the submission where the database supports row locks, have a conditional unique idempotency key, and append a numbered version with a recomputed SHA-256. A stale base or changed content rejects the second session without replacing the first revision. Prior reviews/findings stay attached to their original version.
- Added a safe `difflib.SequenceMatcher` comparison route using stored text. Django templates escape both versions; the responsive comparison labels additions, removals and unchanged context without rendering submitted markup.
- Added the `approvals` app. `ApprovalRequest` pins workspace, submission, exact content version/hash, completed `ReviewRun`, policy revision/hash/full snapshot, requester and submission timestamp. One request is allowed for a content version. `ApprovalDecision` is an immutable one-to-one record for that request and stores reviewer, action, decision time, comment/override rationale and the same content/policy identities plus overridden high-finding IDs.
- Effective submission status is derived from the latest request for the current content version: no request is `DRAFT`, an undecided request is `IN_REVIEW`, a request with a changes decision is `CHANGES_REQUESTED`, and an approved request is `APPROVED` only while the approval remains current. A historical approval whose version or policy is no longer current is displayed as stale; its record is not edited or erased.
- A creator can submit only their current version, and only with a saved `COMPLETE` review containing completed checks that exactly matches today's policy revision/hash/version identities. Partial/failed/stale runs, other-version runs and duplicate requests are rejected. A policy change requires a fresh review and a new request.
- Reviewers and workspace admins access `/app/approvals/`. Workspace members can inspect a request if otherwise permitted; creators only see requests for their own submissions. Brand managers may inspect workspace review/request detail but cannot open the inbox or decide. Only reviewer/admin roles can request changes or approve. The content creator cannot review their own submission even if assigned reviewer/admin access.
- Requesting changes requires a useful comment and records reviewer and time. The immutable decision appears in the creator's version history; another attempt requires a newer content version.
- Approval rechecks current version/hash, complete exact review, current policy snapshot, workspace and reviewer permission inside a transaction. Unresolved high-severity findings block approval unless the reviewer provides a meaningful reason; the decision records the override and exact finding IDs while the findings remain open.
- `approvals.services.current_valid_approval(submission)` checks historical approval against the current content hash/version, complete linked review, current policy revision/hash/source identities, decision identity, and any recorded high-severity override finding IDs. Phase 4A uses this service as the export authorization source of truth.
- PostgreSQL row locks serialize content/policy decisions. SQLite does not implement `select_for_update`; version/idempotency/request/decision uniqueness constraints prevent replacement/duplicate records, and lock contention returns a safe retry message. SQLite is not represented as a multi-writer production concurrency guarantee.
- Added responsive revision, version comparison, creator decision-history, actual reviewer inbox and exact-version reviewer decision screens. All mutation forms use POST + CSRF and findings remain linked to original evidence.

### Phase 3B permissions

| Role | Revise own content | Submit own reviewed version | Inspect workspace requests | Inbox | Request changes / approve |
|---|---:|---:|---:|---:|---:|
| Creator | Yes | Yes | Own requests only | No | No |
| Reviewer | No | No | Yes | Yes | Yes, except own authored content |
| Brand manager | No | No | Yes | No | No |
| Workspace admin | No | No | Yes | Yes | Yes, except own authored content |

### Phase 3B limitations

- Suggested corrections remain copyable text; automatically applying a finding span is deferred because a safe span replacement must account for stale offsets and user confirmation.
- Approval decisions are application records, not tamper-proof or externally notarized audit events. Email/in-app notification delivery is not integrated.
- High-severity findings currently remain `OPEN` because finding resolution is not part of this phase. The reviewer may record a reasoned approval override, which does not dismiss a finding.
- SQLite is suitable for local demonstration but does not provide PostgreSQL-equivalent row-lock concurrency. PostgreSQL needs production configuration/verification before deployment.
- Marketing-content export was not included in Phase 3B; it is implemented in Phase 4A.

## Phase 4A — secure approved content export and review reports (implemented)

- Added `content_reviews.exports` as a reusable export eligibility and report-generation service. It resolves the active server-side workspace membership, limits creators to their own submissions, scopes other authorized workspace roles to their current workspace, and re-runs `current_valid_approval()` on every export request.
- `GET /app/reviews/<submission-id>/export/txt/` returns the exact current approved `ContentVersion.content_text` bytes as UTF-8 `text/plain`, with an ASCII slug-derived attachment name.
- `GET /app/reviews/<submission-id>/export/pdf/` builds a private in-memory paginated ReportLab report from the approval, decision, exact content version, completed review checks, source pins, findings and verified evidence. It includes IDs and hashes for the submission, version, review, decision and policy snapshot; override reports retain their original finding, evidence, reason and reviewer.
- Missing, pending, changes-requested, stale-content, stale-policy, invalid-snapshot, broken-evidence and unauthorized creator exports are denied. Stale or missing approval responses use HTTP 409 with a plain explanation; cross-workspace and another creator's submission are hidden with 404. Anonymous access follows the existing login redirect and workspace membership middleware.
- Attachments are generated in memory, use `Content-Disposition: attachment`, set `Cache-Control: private, no-store`, `Pragma: no-cache`, and `X-Content-Type-Options: nosniff`. Filenames contain only a safe slug and numeric version. User text is escaped as plain text in the PDF; characters unsupported by the embedded font produce a clear 422 response, while TXT remains UTF-8.
- Added the constrained `reportlab>=4.2,<5.0` runtime dependency. No database migration or export file storage was added.
- The UI exposes TXT and PDF actions only when the exact viewed version has a valid current approval. Stale approvals are labelled historical and point back to the review workflow; pending and changes-requested versions have no export action.
- Approval validity now also checks decision/review identity and that high-severity override IDs still exactly match the original open high findings. The report repeats this check and validates every exported finding against its pinned source version and evidence excerpt.
- No event-audit subsystem was added. Application records identify the originating records, but exports are not cryptographically signed or tamper-proof.

### Phase 4A export permissions

| Role | Export current approval | Scope |
|---|---:|---|
| Creator | Yes | Own submissions only |
| Reviewer | Yes | Current workspace |
| Brand manager | Yes | Current workspace |
| Workspace admin | Yes | Current workspace |

Every route is GET-only and repeats authentication, active membership, workspace/owner, current approval, exact content hash, complete review, policy snapshot and evidence checks. Hiding links in the interface is not the security boundary.

### Phase 4A limits and next work

- Export is limited to TXT and PDF; no DOCX export, publishing, sharing links, or stored export artifacts are implemented.
- PDF uses ReportLab's bundled Vera font for English and supported Latin characters. It fails clearly rather than silently dropping unsupported glyphs; use the UTF-8 TXT export for copy containing unsupported characters.
- SQLite ignores `SELECT FOR UPDATE`; local tests validate behavior, not concurrent multi-writer production guarantees. Configure and concurrency-test PostgreSQL before production use.
- There is no export event log, cryptographic signature, timestamp authority or tamper-proof approval certificate. The PDF is a generated report, not proof of authenticity.
- Operational hardening, production deployment, monitoring, backup/retention, and broader deployment security remain not started.

### Boundary

Phases 0 through 4A provide the product introduction, workspace/account foundation, authored and human-verified brand policy, text submission, deterministic evidence-backed checks, immutable content revisions, human approval, and current-approval-protected TXT/PDF exports. Phase 4A does not add campaign document uploads, AI, OCR, image/poster analysis, automatic suggestion application, tamper-proof audit events, notifications, external publishing or deployment operations. See `docs/EXPORTS.md`, `docs/APPROVAL_WORKFLOW.md` and `docs/REVIEW_ENGINE.md` for detailed behavior and limitations.

## Phase 4 — operational hardening (not started)

Production deployment operations, monitoring, recovery, retention policies and production-specific hardening remain future work.
