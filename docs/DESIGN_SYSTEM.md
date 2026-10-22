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
- Workspace identity shows only database-backed membership/workspace details and role-specific guidance. Review activity is explicitly unavailable because there is no review model yet. Do not add dashboard metrics, notifications or status summaries without database-backed behavior.
- Future navigation entries may appear only as noninteractive labels with an explicit unavailable state, and only for the roles they concern. Use links only for named routes that already exist.
- The members screen uses a semantic list of real current-workspace membership records; role and active status are text as well as color. Keep the desktop row grid compact and stack identity/access details on mobile.
- Workspace admins see current workspace configuration metadata and a read-only member route. Avoid implying that settings, invitations, member changes, email changes or role edits are available.
- Transition duration is short and reduced motion is respected. Loading animation has a static reduced-motion fallback.

## Preview policy

The landing-page preview uses synthetic LumaDesk content and is labeled as illustrative. Its sample findings, guideline citation and status are not data, do not reflect a completed engine run and must not be used as a model for claiming product functionality.
