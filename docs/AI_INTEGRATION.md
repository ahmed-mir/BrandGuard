# Phase 5A — source-grounded AI advisory analysis

AI analysis is an optional, user-initiated aid. It is stored separately from deterministic `ReviewRun` and `ReviewFinding` records. It never changes a deterministic result, content workflow state, approval decision, current-approval validation, or TXT/PDF export eligibility.

## Provider and model

The adapter uses the official OpenAI Python SDK and the Responses API with a strict JSON Schema response format. The default model is `gpt-5.4-mini`; this identifier is configurable and is currently listed by OpenAI as supporting the Responses endpoint and Structured Outputs. The SDK API is `OpenAI(...).responses.create(...)`. The implementation sends `store=False`, caps output tokens, sets a short timeout, and disables SDK retries. See the [Responses API text guide](https://developers.openai.com/api/docs/guides/text?api-mode=responses), [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs), [model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini), and [official Python SDK](https://github.com/openai/openai-python).

The model identifier is a provider configuration value, not an entitlement. The OpenAI API account must have API billing/usage enabled and access to the chosen model; a ChatGPT subscription does not itself provide API credits. Usage is charged by the provider according to its current pricing and account limits.

## Configuration

AI is off by default. A configured key alone does not turn it on. The process needs all of:

- `BRANDGUARD_AI_ENABLED=true`
- `OPENAI_API_KEY` in the server-side secret store
- `BRANDGUARD_AI_MODEL` (defaults to `gpt-5.4-mini`)

Optional bounded controls:

| Variable | Default | Enforced range |
|---|---:|---:|
| `BRANDGUARD_AI_TIMEOUT_SECONDS` | 15 | 3–20 seconds |
| `BRANDGUARD_AI_MAX_OUTPUT_TOKENS` | 1,400 | 100–2,000 |
| `BRANDGUARD_AI_MAX_FINDINGS` | 12 | 1–12 |
| `BRANDGUARD_AI_MAX_CONTEXT_CHARS` | 10,000 | 1,000–12,000 |

Marketing content is already limited to 20,000 characters by the content-review form and model. The synchronous timeout is 15 seconds by default, below the app's Gunicorn request timeout. The SDK retry count is zero. Configure these variables in Render's secret/environment settings only when intentionally enabling the feature. Never put the API key in source control, this document, the browser or a logged command.

For local PowerShell, after adding the key to a trusted local secret manager/process environment, load it into this shell without printing it, then opt in for that process:

```powershell
$env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'User')
$env:BRANDGUARD_AI_ENABLED = 'true'
$env:BRANDGUARD_AI_MODEL = 'gpt-5.4-mini'
python manage.py runserver
```

If the key is absent or `BRANDGUARD_AI_ENABLED` is false, the page shows an honest unavailable state and deterministic review continues normally.

## User consent and transmitted information

The creator must submit a CSRF-protected POST and check an explicit disclosure on the exact content-version page. Normal page viewing never calls the provider. The disclosure states that selected marketing copy and relevant approved excerpts leave BrandGuard for the requested analysis.

For one request, the provider receives:

- The exact saved marketing text for the selected immutable `ContentVersion` (max 20,000 characters).
- Channel, optional audience and campaign purpose.
- A bounded lexical selection of exact passages (max 10,000 characters by default) from currently approved, currently effective source versions in the resolved workspace.
- The provided source-version IDs, source titles and version numbers used to make citations possible.

It does not receive raw uploaded files, draft/superseded/future-effective/cross-workspace sources, database rows, credentials, account identity, tools or a web-search capability. The user must have authority to send that content and source material to the provider. The synthetic LumaDesk pack is recommended for demos.

The provider request uses Responses `store=False`. OpenAI's current data-control documentation says Responses API application-state retention is 30 days by default or with `store=true`, while abuse-monitoring logs may retain customer content for up to 30 days unless an eligible organization has approved Modified Abuse Monitoring or Zero Data Retention. Therefore `store=False` is a privacy measure, not a promise of zero provider retention. Operators must review their own provider account controls and terms before sending confidential data. See [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data).

## Snapshot and bounded context

The service resolves membership from the authenticated server-side workspace. It captures `policy_snapshot(workspace)` and the exact effective approved source version set while briefly locking the workspace row used by policy publication. It releases database locks before making the network request. It uses bounded lexical retrieval over source text and gives explicit voice-rule passages priority. There is no vector store and no external search.

The persisted `AIAnalysisRun` records the workspace, exact immutable content version/hash, policy revision/hash/full snapshot, the bounded context manifest (exact approved passages sent), requesting user, provider/model, consent timestamp, status, error category and token usage when returned. It does not persist the full prompt, complete provider response, duplicate copy of the submitted text, or private uploaded files. Each finding links to its source version and source-content hash when it is evidence-backed. Results for a content version or policy snapshot that is no longer current remain available as historical advisory results and are visibly marked stale.

## Output schema and validation

The provider receives a strict JSON Schema with a `findings` array. Each finding has only `category`, `classification`, `explanation`, `matched_text`, `source_version_id`, `evidence_excerpt` and `suggested_wording`; extra properties are rejected. Supported categories are `TONE`, `MESSAGING` and `CLAIM`. Classifications are `SUPPORTED`, `POTENTIAL_CONFLICT`, `NOT_VERIFIED` and `NEEDS_HUMAN_REVIEW`.

The server validates the schema again, allowed categories/classifications, field lengths, finding count, workspace, version identity, snapshot membership, approved source hash, and that the exact quoted evidence occurs both in the immutable source text and in a passage actually sent to the provider. Tone and messaging findings must be marked as potential conflict or human review and cite an approved excerpt. A claim may be uncited only when it is classified `NOT_VERIFIED` or `NEEDS_HUMAN_REVIEW`; the interface clearly says that absence of evidence does not establish falsity. Invalid source IDs/evidence fail the run and no finding is stored. Suggested wording is rejected if it adds a number not present in its cited excerpt.

Prompt injection is treated as untrusted text. System instructions are separate from JSON-encoded content/source passages and explicitly prohibit following instructions within that data. The model has no tools or external actions. Strict JSON is not treated as a prompt-injection defense by itself; server validation remains authoritative.

## Deterministic versus AI results

Deterministic `ReviewRun` checks are objective executions of persisted typed rules and remain the basis for the existing content review workflow. AI analysis is separately identified, subjective and advisory. An AI finding cannot become a deterministic rule finding, human approval, high-severity override, or export authorization. AI analysis is not included in the approved-content PDF report in this phase.

## Failure behavior and cost controls

The stored run state is `RUNNING`, then `COMPLETE` or `FAILED`. No provider request results in a success-shaped response. Failures use non-sensitive categories for disabled/missing configuration, no approved context, application rate limit, timeout, authentication, quota, provider rate limit/unavailability, refusal, invalid response/evidence, stale references or persistence failure. The UI preserves deterministic results and says AI had no effect on them. Error logs contain record IDs and exception class only; no content, source excerpt, API key, response body or request headers.

At most three analysis attempts per requesting user/content version in a rolling hour are permitted. A completed analysis with the same content hash, exact policy snapshot, provider and model is reused instead of billed again. An equivalent recent in-flight request is also reused, including when separate tabs have different request tokens; its request is bounded by the configured provider timeout. Old in-flight records beyond that timeout window do not block a retry after an abnormal worker exit. New content or policy identities allow a new separately recorded analysis. No automatic reruns occur. Provider account spend limits should also be configured by the operator.

## Data model and migration

The new `ai_analysis` app stores `AIAnalysisRun` and `AIAnalysisFinding` separately from deterministic review records. Findings are immutable and their content offsets are derived and checked against the exact saved version. Evidence fields can only refer to an approved source version in the saved policy snapshot/context manifest. No change is made to approval or export models/services.

Apply local migrations with `python manage.py migrate`. To roll back only this optional feature, first set `BRANDGUARD_AI_ENABLED=false` (or omit the key) and deploy that setting. The deterministic review, approvals and exports continue. The migration may remain in place; do not delete AI analysis history as part of an emergency disable. Rolling back app code after data has been written requires a normal Django migration review and backup; do not reverse migrations blindly.

## Known limits

- Tone and contextual messaging are subjective and can be wrong; a cited passage shows what was supplied, not that the judgement is objectively true.
- No internet fact checking, broad factual validation, calibrated confidence, semantic rule execution, image/OCR, poster, audio or video analysis is implemented.
- Lexical retrieval can miss relevant guidance; the analysis is limited to the excerpts saved in its context manifest.
- AI analysis has no effect on the approval workflow. Human approval remains the only approval authority.
- No live provider request has been run during this implementation; automated tests use mocked provider responses. Provider availability, account retention settings and actual billing are operator-side concerns.
- SQLite tests do not establish PostgreSQL multi-writer concurrency. The brief's current optional feature is synchronous and short; it uses the PostgreSQL-compatible workspace row-lock snapshot pattern, but a live PostgreSQL AI-request test remains to be performed before enabling it broadly.
