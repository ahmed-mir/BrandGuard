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

## Phase 2B — source ingestion (not started)

Add safe document uploads and text extraction with provenance and human verification. Extracted content must not become approved policy automatically.

## Phase 3 — content submission and versioning (not started)

Implement creator submissions and immutable content versions so a reviewer can identify precisely what changed.

## Phase 4 — evidence-backed review (not started)

Implement review execution against approved sources. Every finding must retain its supporting source evidence. Do not fabricate results when a review has not run.

## Phase 5 — revision and human approval (not started)

Implement revision tracking and role-authorized approval of an exact content version, with an auditable decision record.

## Phase 6 — export and operational hardening (not started)

Permit export only for the approved version, then add deployment operations, monitoring and production-specific hardening.

### Boundary

Phase 0, 1A and 1B provide the product introduction and workspace/account foundation; Phase 2A adds structured, evidence-backed source authoring and publication. These phases do not include document ingestion/extraction, content submission, review execution, AI integration, review findings, content approval decisions, audit-event subsystem, activity data or export. Current routes and limits are documented in the README.
