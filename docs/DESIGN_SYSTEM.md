# BrandGuard visual system

The system aims for a quiet editorial character: clear structure, measured color, typography with contrast and a careful grid. It uses semantic HTML, local CSS and no external font or image dependencies.

## Tokens

| Token | Value | Use |
|---|---|---|
| Canvas | #F7F8F6 | Main page background |
| Paper | #FFFFFF | Form and interface surfaces |
| Ink | #192B2C | Primary type and dark approval section |
| Ink soft | #435756 | Supporting type |
| Muted | #657673 | Secondary copy and hints |
| Brand teal | #176F66 | Primary actions, links, focus and evidence cues |
| Teal dark | #12564F | Hover state for primary action |
| Teal pale | #E7F1EE | Selected navigation and soft highlights |
| Border | #E3E9E5 | Dividers and default outlines |
| Amber | #93611B / #FBF3E3 | Needs attention and pending states |
| Green | #276A4D / #E8F2EB | Resolved and success states |
| Red | #A33F3F / #F9EAEA | Error states |
| Slate | #647672 / #EDF0EE | Neutral and informational states |

CSS custom properties are defined in core/static/core/css/site.css under :root and should be reused instead of one-off colors and spacing.

## Type

- Main text: system sans stack (Inter, Aptos, Segoe UI, then platform UI fallbacks).
- Editorial emphasis: Georgia serif, used sparingly for italic phrase contrast.
- Technical labels: system monospace stack with restrained tracking.
- External fonts are not required, so the page renders without network access.

## Layout and spacing

- Content max width: 1160px; responsive side gutters contract from 32px to 15px.
- Spacing scale: 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80 and 96px.
- Small radii only: 5px, 9px and 14px. Borders define surfaces; shadows are limited to the illustrative product frame.
- Breakpoints: 900px, 640px and 390px. Layouts are designed to fit from 360px upward.

## Components and conventions

- Use semantic landmarks, heading hierarchy, lists for ordered workflows and real anchors for navigation.
- Provide visible :focus-visible treatment and a skip link on page templates.
- Buttons are reserved for actions; anchors navigate to real routes or page sections. Omit controls whose action does not exist.
- Shared utility patterns include .button, .field, .status, .finding, .state, and .app-shell.
- Status colors communicate semantic meaning and use text labels as well as color.
- The application shell is shared by overview, account settings and workspace members. Its desktop sidebar stays compact; at widths up to 800px it becomes an accessible slide-in navigation drawer. A no-JavaScript fallback keeps navigation visible. New routes supply a named active section rather than inventing links.
- The mobile navigation toggle is a real button with `aria-expanded` and `aria-controls`. When open, focus moves into the drawer, is trapped while modal, returns to the trigger on Escape/close, and the main area is inert. The scrim and close control are keyboard-operable. Honor `prefers-reduced-motion` by removing drawer movement.
- Authentication and account settings use the same canvas, ink and teal tokens. Keep form labels persistent, errors adjacent, connected with `aria-describedby` and announced. Success feedback uses a restrained semantic status region. Password fields must never render bound password values.
- Workspace identity shows only database-backed membership/workspace details and role-specific guidance. Since Phase 3A, Content reviews is a real route backed by persisted submissions and review runs. Keep the overview free of invented counts, analytics, notifications or compliance summaries; any future summaries must query real records and describe their scope accurately.
- Future navigation entries may appear only as noninteractive labels with an explicit unavailable state, and only for the roles they concern. Use links only for named routes that already exist.
- The members screen uses a semantic list of real current-workspace membership records; role and active status are text as well as color. Keep the desktop row grid compact and stack identity/access details on mobile.
- Workspace admins see current workspace configuration metadata and a read-only member route. Avoid implying that settings, invitations, member changes, email changes or role edits are available.
- Transition duration is short and reduced motion is respected. Loading animation has a static reduced-motion fallback.

## Content review workspace (Phase 3A)

- The review list is an actual submission list. Creators see their own records; workspace reviewers, brand managers and admins see records in their current workspace. A new-submission action appears only for creators and points to a real route.
- The submission editor is plain text and intentionally does not look like a rich-text or file-upload editor. It explains that saving creates an immutable version and that campaign-document upload is not supported.
- Version detail shows the actual saved content, channel, hash, creator and review runs. Each run starts through a CSRF-protected POST with a per-form idempotency token. Repeated submission with the same token must not create duplicate review records.
- The results page prioritizes findings and source evidence. Use visible text for severity and check status; do not rely on color alone. Findings display saved rule identity, category, severity, exact matched text and validated character offsets. Missing-phrase findings explicitly state that a content location is unavailable.
- Evidence expands using native accessible `<details>`/`<summary>` and identifies the exact source title/version, rule identifier and excerpt. Link to the actual pinned source version. Render both content and evidence with Django template escaping; do not inject content as HTML.
- A policy-change notice describes the old pinned snapshot. The status must distinguish complete, partial and failed runs. The interface may say that no violations were found only for completed checks, and must disclose human-review flags and the narrow deterministic scope. It must never show a compliance score or imply universal factual verification.
- Tone analysis is explicitly labeled as not performed. No invented AI assessment, progress animation, score or claim is permitted.
- Phase 3A uses `.review-*` and `.review-state*` components in `site.css`, continuing the shared canvas, ink, teal, subtle borders and spacing tokens. Severity and outcome states use existing semantic feedback colors with text labels. At mobile widths, content and findings stack in one column and long identifiers/evidence wrap without page-level horizontal overflow.

## Preview policy

The landing-page preview uses synthetic LumaDesk content and is labeled as illustrative. Its sample findings, guideline citation and status are not data, do not reflect a completed engine run and must not be used as a model for claiming product functionality.

## Brand source library (Phase 2A)

- The source library uses a compact, horizontally scrollable table at desktop widths; on narrow screens, keep source identity, version/status and the primary open action understandable without page-level horizontal overflow. Filters are GET forms and only show controls managers can use.
- Source detail separates authoritative text, structured rules, facts, exact evidence and version/approval history. Keep quoted evidence visually distinct with a left rule and a text label; never communicate provenance through color alone.
- Draft editors use one semantic form per operation with persistent labels, adjacent field errors and a visible source-version context. Rule matcher fields are typed controls, not editable JSON or executable expressions. Fact evidence must remain linked to the same source version.
- Approved and superseded versions are presented as read-only. Approval is an explicit POST action with CSRF; it includes the exact draft version number in its confirmation text. A successful publication identifies the approver and time, while errors preserve the draft.
- The sidebar Brand Sources link resolves for every workspace role; server-side policy decides which status/version records each role can see. Do not infer access control from navigation visibility.
- Show real source/version state and useful empty guidance. Do not add review counts, compliance charts, false “verified” indicators, or AI status. Effective date metadata must not be labeled as expiration enforcement.
- Use `.brand-*`, `.source-*`, `.published-*`, `.evidence-quote` and `.source-status` component families already added to `site.css`; extend existing tokens and focus states rather than introducing page-specific palette values.
- When a policy version is published, the workspace revision and deterministic hash refer to the active approved set. Future review UI should display a pinned historical snapshot only from a review record, not recompute a past review against today's policy.

## Document import and confirmation (Phase 2B)

- Keep the import view a focused metadata form: title, source type, optional description, and a plainly labelled file input. State the actual limits and supported formats adjacent to the input; do not use invented processing bars or scan animations for synchronous extraction.
- Use the existing source editor to review extracted text. Keep source text editable while draft; show provenance (original filename, type, uploader, timestamp, extraction result, hash) as read-only metadata. Display imported text as normal escaped form/content output; never mark user-provided source text safe HTML.
- Confirmation is a separate, explicit, CSRF-protected POST after the manager has inspected and saved the current text. Use text plus a semantic state label to show whether confirmation is current or stale; a stale confirmation requires an action before publishing. Avoid presenting extraction as verification of facts.
- The original document is private provenance, not a public download link. Do not expose storage URLs or path values in templates. Communicate that scanned-PDF OCR is unavailable and provide the existing manual-authoring route instead.
- Continue using the established `.source-*`, `.document-*`, `.form-field` and `.button` classes and semantic tokens. Keep the provenance definition list compact, allow hashes to wrap, and stack form/provenance layouts below tablet width without page-level overflow.

## Revision and approval workspace (Phase 3B)

- Keep the approval inbox a real table at desktop widths. At mobile widths, stack each request into labelled rows; do not make the whole page horizontally scroll. Pending and stale requests use explicit text labels as well as semantic colors.
- Version comparison is a two-column, line-based `difflib` view with removed, added and unchanged regions. Submitted text is always Django-escaped. Stack each old/new line pair at phone widths and allow long text to wrap.
- Version history identifies author, timestamp, content version, latest real review state and version-bound approval state. A historical approval is never styled or labelled as current when content or policy changed.
- Approval detail gives the exact content and evidence equal priority. The heading shows version, creator, channel, request date, review identity and policy revision. Findings remain visibly tied to source/version and are not hidden behind collapsed accordions.
- Place decision forms after the content and findings so the reviewer can inspect the scope first. Make the approval action explicit; request-changes requires adjacent comment feedback. High-severity override copy must state that the finding remains open. Do not use an ambiguous single “Done” action.
- The creator revision editor preloads the actual current text and explains that save creates a new immutable version and requires a fresh review. Suggested wording remains copyable; no automatic span replacement is exposed.
- Use `.approval-*`, `.version-diff*`, `.version-history*` and existing `.review-*` component families. Reuse semantic tokens, borders and focus treatment. No new palette, modal framework, animation or visual redesign of the public landing page is needed.
