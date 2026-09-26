---
status: accepted
date: 2026-09-26
---

# Canonical error contract for the Mini Chat REST API

**ID**: `cpt-cf-mini-chat-adr-canonical-error-contract`

## Context and Problem Statement

The original Mini Chat contract (PRD §7.2, DESIGN §3.3 "Error Codes") defined its own JSON error envelope `{code, message}`. It also defined per-error HTTP statuses such as 413 `file_too_large`, 415 `unsupported_file_type` and 502 `provider_error`, and required the SSE `error` event to reuse that envelope.

The platform later moved every gear to the canonical error model in `toolkit-canonical-errors`. That model uses an RFC 9457 `Problem` with a fixed set of categories, each with a fixed HTTP status. Mini Chat was migrated in the same change (`dc9519b3c`, "canonical-error-aware extractors"; mapping in `mini-chat/src/api/rest/error.rs`). The gear's documents were never updated, so they described a wire format that clients no longer receive.

This ADR records the contract that is actually served.

## Decision Drivers

* One error shape across all platform gears, so clients and gateways parse errors the same way.
* HTTP status must follow from the error category, not from per-gear tables.
* Machine-readable reasons must survive the migration: clients still need to tell `NOT_LATEST_TURN` from `request_id_conflict`.
* The SSE stream is a separate channel: once the stream is open, errors cannot change the HTTP status.

## Considered Options

* Keep the gear-specific `{code, message}` envelope and statuses.
* Adopt the canonical `Problem` for REST, and keep `{code, message}` for the SSE `error` event.

## Decision Outcome

Chosen option: "Adopt the canonical `Problem` for REST, and keep `{code, message}` for the SSE `error` event".

**REST (pre-stream) errors** are `Problem` objects with the fields `type`, `title`, `status`, `detail`, `instance`, `trace_id` and `context`. There is **no** top-level `code` field. The machine-readable reason is in one of three places:

* `context.reason` (`aborted`, `permission_denied`);
* `context.field_violations[].reason` (`invalid_argument`, `out_of_range`);
* `context.violations[]` (`failed_precondition`: `{subject, description, type}`; `resource_exhausted`: `{subject, description}`).

| Condition | Category | HTTP | Reason / violation |
|---|---|---|---|
| Chat, message, turn, attachment or model not found (including another user's resource) | `not_found` | 404 | resource scoped by `type` |
| Unknown or disabled model on `POST /chats` | `invalid_argument` | 400 | `field_violations[model].reason = INVALID_MODEL` |
| Validation error (empty title, empty content, bad OData `$filter`/`$orderby`/cursor) | `invalid_argument` | 400 | `detail` |
| Request body does not match the schema (missing required field, wrong type, e.g. a non-UUID `attachment_ids` entry); malformed JSON is 400 | `invalid_argument` | 422 | platform JSON extractor (`toolkit::api::rest::extract::Json`) |
| Unsupported upload MIME type | `invalid_argument` | 400 | `UNSUPPORTED_CONTENT_TYPE` (was 415) |
| Image on a model without vision | `invalid_argument` | 400 | `VISION_NOT_SUPPORTED` (was 415) |
| Invalid, foreign or not-ready `attachment_ids` | `invalid_argument` | 400 | `field_violations[attachment]` |
| Upload larger than the limit | `out_of_range` | 400 | `FILE_TOO_LARGE` (was 413) |
| Too many images in one message | `out_of_range` | 400 | `TOO_MANY_IMAGES` |
| Message exceeds `max_input_tokens` | `out_of_range` | 400 | `INPUT_TOO_LONG` |
| Mandatory context does not fit the budget | `out_of_range` | 400 | `CONTEXT_BUDGET_EXCEEDED` |
| Kill switch (web search, images) | `failed_precondition` | 400 | `violations[{subject: web_search\|images, type: FEATURE_DISABLED}]` |
| Retry/edit/delete of a non-terminal turn | `failed_precondition` | 400 | `violations[{subject: turn_state, type: STATE}]` |
| Reaction on a non-assistant message | `failed_precondition` | 400 | `violations[{subject: reaction_target, type: STATE}]` |
| AuthZ denied, or the PDP failed (fail-closed) | `permission_denied` | 403 | `AUTHZ_DENIED` |
| Another turn is running in the chat (stream) | `aborted` | 409 | `turn_already_running` |
| `request_id` reused for a non-completed or deleted turn | `aborted` | 409 | `request_id_conflict` |
| Mutation of a turn that is not the latest (including an already deleted turn) | `aborted` | 409 | `NOT_LATEST_TURN` |
| Concurrent mutation lost the running-turn race | `aborted` | 409 | `GENERATION_IN_PROGRESS` |
| Deleting an attachment referenced by a message | `already_exists` | 409 | `resource_name = attachment_locked` |
| Quota exhausted | `resource_exhausted` | 429 | `violations[{subject: <quota_scope>}]` |
| Per-chat document count or storage limit | `resource_exhausted` | 429 | `document_limit` / `storage_limit` (was 400) |
| Provider or storage backend failure before streaming | `service_unavailable` | 503 + `Retry-After` | (was 502/504) |
| Upload concurrency limit | `service_unavailable` | 503 + `Retry-After` | |
| Internal / database error | `internal` | 500 | |

**SSE `error` event.** Once the stream is open, a terminal failure is sent as `event: error` with `data: {code, message}`. This envelope is independent of `Problem`. The codes are listed in DESIGN §3.3 "Streaming error codes".

### Consequences

* Good, because every platform gear returns errors in the same shape and statuses follow the category.
* Good, because reasons are still machine-readable and have stable names.
* Bad, because this is a breaking change for clients written against the old contract: there is no `code` field, and some statuses changed (413→400, 415→400, 502/504→503, document limit 400→429).
* Neutral, because JSON errors and the SSE `error` event now use different shapes.

### Confirmation

* `mini-chat/src/api/rest/error.rs` unit tests pin the category, status and reason of the mappings they cover (not every variant has a dedicated test).
* The E2E suite (`testing/e2e/suites/mini_chat`) asserts `Problem.type` and the reason fields through a shared `assert_problem` helper.
* The generated OpenAPI (`docs/api/api.json`) is the reference for the response schemas.

## Pros and Cons of the Options

### Keep the gear-specific envelope

* Good, because existing clients keep working.
* Bad, because Mini Chat would be the only gear with a private error format. That would need a per-gear transport override in `toolkit-canonical-errors`, which the platform does not provide for 413/415.

### Canonical `Problem` for REST, `{code, message}` for SSE

* Good, because it matches the platform and the implemented code.
* Bad, because it is a documented breaking change.

## More Information

* Supersedes the "Error Codes" table in DESIGN §3.3 and PRD §7.2 as they were before 2026-09.
* The old `docs/openapi.json` was hand-written, described the superseded contract and was removed. The generated `docs/api/api.json` at the repository root is the source of truth.

## Traceability

* **PRD**: [PRD.md](../PRD.md)
* **DESIGN**: [DESIGN.md](../DESIGN.md)

This decision directly addresses the following requirements or design elements:

* `cpt-cf-mini-chat-interface-public-api` — error responses of every REST operation.
* `cpt-cf-mini-chat-contract-sse-streaming` — the SSE `error` event envelope.
* `cpt-cf-mini-chat-fr-chat-streaming` — pre-stream errors are normal JSON errors.
* `cpt-cf-mini-chat-nfr-authz-alignment` — denied and failed authorization is 403, a hidden resource is 404.
