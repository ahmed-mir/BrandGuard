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
- Role-specific sidebar navigation shows future Brand Sources, Content Reviews and Approval Inbox as noninteractive, explicitly unavailable labels for relevant roles. All clickable destinations resolve to implemented routes.
- Added `/app/settings/profile/`: users may view their username, email, workspace and role; edit only first/last name; and change their password using Django's built-in `PasswordChangeForm`, validators and session hash update. Both forms are POST + CSRF protected and show success/validation feedback.
- Added `/app/members/`: only workspace admins may access the read-only list of actual memberships in their currently resolved workspace. It includes role and active status and does not expose other workspaces.
- All routes use server-side membership/role decorators. No self-service registration, invitation, email/username/role editing, membership mutation, review, guideline or approval function was added.
- Tests cover role-specific overview/navigation, resolvable links, account/profile and password behavior, CSRF, session preservation, member visibility/denial/isolation, plus the existing Phase 0 and 1A flows.

### Phase 1B boundaries

Workspace administration remains read-only. The membership list does not invite, edit, disable or remove members; workspace metadata is displayed to admins but not edited. Account settings do not change email, username or workspace role. Future product navigation is plain, noninteractive text. Brand source management, content review, AI analysis, approval workflow, audit events and export are not implemented.

## Phase 2 — approved guideline sources (not started)

Define how approved brand guidance is authored, versioned, sourced and made reviewable. Any findings must cite real source material.

## Phase 3 — content submission and versioning (not started)

Implement creator submissions and immutable content versions so a reviewer can identify precisely what changed.

## Phase 4 — evidence-backed review (not started)

Implement review execution against approved sources. Every finding must retain its supporting source evidence. Do not fabricate results when a review has not run.

## Phase 5 — revision and human approval (not started)

Implement revision tracking and role-authorized approval of an exact content version, with an auditable decision record.

## Phase 6 — export and operational hardening (not started)

Permit export only for the approved version, then add deployment operations, monitoring and production-specific hardening.

### Boundary

Phase 0, 1A and 1B provide public introduction, authentication/workspace identity, role-aware navigation, basic profile and password management, and read-only workspace membership inspection by admins. They do not include guideline management, content submission, review execution, AI integration, approval actions, audit events, activity data or export behavior. Current routes and limits are documented in the README.
