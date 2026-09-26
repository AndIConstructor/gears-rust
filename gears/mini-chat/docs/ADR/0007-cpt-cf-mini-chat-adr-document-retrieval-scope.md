---
status: accepted
date: 2026-09-26
---

# P1 scope of document processing and retrieval

**ID**: `cpt-cf-mini-chat-adr-document-retrieval-scope`

## Context and Problem Statement

PRD §5.2 and DESIGN specify several document-related capabilities. Some were implemented differently and some were not implemented. This ADR records the P1 state so the documents, the code and the E2E suite agree.

## Decision Drivers

* The documents must match what the upload and retrieval paths actually do.
* No large new features in the P1 alignment.
* Every gap must name the requirement it affects and when to revisit it.

## Considered Options

* Implement the missing capabilities.
* Record the implemented behaviour and mark the gaps "Not implemented" / "Future".

## Decision Outcome

Chosen option: "Record the implemented behaviour and mark the gaps".

| Capability | Requirement | Status | Current behaviour |
|---|---|---|---|
| Document summary on upload | `cpt-cf-mini-chat-fr-doc-summary` | Not implemented | `doc_summary` and `summary_updated_at` are always `null`. No background task exists and the ContextPlan has no document-summary tier. |
| Synchronous upload | `cpt-cf-mini-chat-fr-file-upload`, `cpt-cf-mini-chat-fr-image-upload` | Accepted | `POST /attachments` uploads to the provider, indexes the document and builds the thumbnail within the request. It returns 201 with `status: ready`. On failure it returns an HTTP error, and the row stays visible via GET with `status: failed` and `error_code`. The `uploaded` status is an internal intermediate state and can be observed. Polling still works but is not needed. |
| Max indexed chunks per chat | `cpt-cf-mini-chat-fr-per-chat-doc-limits` | Not implemented | Only the document count and total size per chat are enforced (429 `document_limit` / `storage_limit`). |
| Per-turn `file_search` call limit | `cpt-cf-mini-chat-fr-file-search` | Accepted (different) | Bounded by the model's `max_tool_calls`, which covers all built-in tools together (default 2). |
| Per-user daily `file_search` limit | PRD §4.1 | Not implemented | `quota_usage.file_search_calls` is not counted. |
| Immediate exclusion of a deleted document from `file_search` | `cpt-cf-mini-chat-fr-attachment-deletion` | Not implemented | Deletion removes the provider file asynchronously. `file_search` is called without attribute filters, so chunks may be returned until the provider file is gone. Citations never reference a deleted attachment. Attachments referenced by a sent message cannot be deleted (409 `attachment_locked`). |
| Anthropic chats: document search (`search_files`, `load_files`) | `features/anthropic-provider-support.md` §1.2 | Not implemented | Documents are indexed in the RAG provider, but the Anthropic adapter drops the `file_search` tool, so Claude cannot search them. Knowledge search (`search_knowledge`), when enabled, is the only retrieval path. |
| Historical messages list deleted attachments | PRD §9 | Accepted (different) | `attachments[]` on messages lists only non-deleted attachments. |

### Consequences

* Good, because every documented behaviour is now testable against the running system.
* Bad, because document summaries, the chunk cap and deletion-time retrieval exclusion remain open product gaps.

### Confirmation

* E2E: a document upload reaches `ready` (`testing/e2e/suites/mini_chat/test_attachments.py::TestUploadAndGet::test_upload_and_get_attachment`). `doc_summary` is asserted null/absent only for an XLSX (code interpreter) upload (`test_code_interpreter.py::TestXlsxUploadAccepted::test_xlsx_reaches_ready`); no test asserts it for a `file_search` document.
* E2E: deleting a referenced attachment gives 409 `attachment_locked` (`test_attachments.py::TestDeleteReferencedAttachment::test_delete_referenced_attachment_409`).

## More Information

* Re-evaluate the document summary when the context budget for document-heavy chats becomes a support issue.
* Re-evaluate retrieval exclusion when `file_search_filters` (P4-6) is wired.

## Traceability

* **PRD**: [PRD.md](../PRD.md) §5.2, §9
* **DESIGN**: [DESIGN.md](../DESIGN.md) §3.3 (attachments), §3.6 (file upload sequence), §4 (P1 Scope Boundaries)

This decision directly addresses the following requirements or design elements:

* `cpt-cf-mini-chat-fr-doc-summary`
* `cpt-cf-mini-chat-fr-file-upload`
* `cpt-cf-mini-chat-fr-image-upload`
* `cpt-cf-mini-chat-fr-file-search`
* `cpt-cf-mini-chat-fr-per-chat-doc-limits`
* `cpt-cf-mini-chat-fr-attachment-deletion`
* `cpt-cf-mini-chat-seq-file-upload`
