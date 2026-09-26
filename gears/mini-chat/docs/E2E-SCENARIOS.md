# E2E Test Scenario Map

Maps DESIGN.md requirements to the E2E tests in `testing/e2e/suites/mini_chat/`.

**Convention:** Scenario IDs use `{area}-{number}` format (e.g., `10-01`). Some tests
name the scenario in a docstring or comment (e.g., `# 10-01, 10-02: Upload and get attachment`).

**Coverage column:**

- `TestClass::test_function` — the E2E test that covers the scenario. A class name alone
  means every test in the class contributes.
- `GAP — <reason>` — no automated test covers the scenario.
- `N/A — not implemented (ADR-00xx)` — the capability is not part of P1; see the ADR.
- `unit tests: <file>` — covered only by Rust unit tests; paths are relative to the repository root.

**Modes:** the suite runs `--mode offline` (mock provider, default) or `--mode online`
(real providers). Tests marked `online_only` are skipped offline; they are marked
"(online only)" below. Many offline tests also call `pytest.skip` in online mode because
they drive or inspect the mock provider. `provider_chat` is parameterized over OpenAI
(`gpt-5.2`, standard tier) and Azure (`azure-gpt-4.1`, premium tier).

**`test_live_smoke.py` is online only** (module-level `online_only` marker). It runs against
an already-running backend with real LLM calls and duplicates paths the offline suite covers
with the mock provider. It is referenced below only where it is the sole coverage.

**Error contract (ADR-0004):** pre-stream errors are canonical Problem JSON (`type` names the
category, e.g. `invalid_argument`, `not_found`, `aborted`, `resource_exhausted`). Post-stream
errors are an SSE `error` event with `{code, message}`. A body that fails schema
deserialization is 422; malformed JSON and semantic validation failures are 400; size limits
are 400 `out_of_range` (not 413); unsupported types are 400 (not 415). Requests without a
valid token are 401. Resources of another user or tenant are 404.

---

## 01 — Principles & Constraints

| ID    | Scenario                          | Test File             | Covered by                                                              |
|-------|-----------------------------------|-----------------------|-------------------------------------------------------------------------|
| 01-01 | Tenant-Scoped Isolation           | test_isolation.py     | TestIsolation (parameter `other_tenant`)                                |
| 01-02 | Owner-Only Content Access         | test_isolation.py     | TestIsolation (parameter `same_tenant`)                                 |
| 01-03 | Streaming-First Delivery          | —                     | (architectural, not directly testable)                                  |
| 01-04 | Linear Conversation Model         | —                     | (enforced by schema)                                                    |
| 01-05 | OpenAI-Compatible Provider        | —                     | (architectural)                                                         |
| 01-06 | Model Image Capability Constraint | —                     | (enforced by catalog)                                                   |
| 01-07 | No Credential Storage             | —                     | (architectural)                                                         |
| 01-08 | Context Window Budget             | —                     | (enforced internally)                                                   |
| 01-09 | License Gate                      | —                     | GAP — the rig always grants the base license feature; the gate is the interim base-license check (ADR-0008) |
| 01-10 | No Buffering Constraint           | test_principles.py    | TestPrinciples::test_no_buffering                                       |
| 01-11 | Model Locked Per Chat             | test_principles.py    | TestPrinciples::test_model_locked_per_chat                              |
| 01-12 | Quota Before Outbound             | test_quota_policy.py  | TestQuotaExhaustion::test_all_tiers_exhausted_429 (provider not called) |
| 01-13 | Kill Switch: disable_premium_tier | —                     | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 01-14 | Kill Switch: force_standard_tier  | —                     | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 01-15 | Kill Switch: disable_file_search  | —                     | unit tests: gears/mini-chat/mini-chat/src/domain/service/stream_service/mod.rs, gears/mini-chat/mini-chat/src/domain/retrieval.rs |
| 01-16 | Kill Switch: disable_web_search   | —                     | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 01-17 | Kill Switch: disable_images       | —                     | unit tests: gears/mini-chat/mini-chat/src/domain/service/attachment_service_test.rs |

Kill switches are fixed plugin configuration in the E2E rig (all off), so they cannot be
toggled per test.

## 02 — Chat CRUD

| ID    | Scenario                                         | Test File             | Covered by                                                   |
|-------|--------------------------------------------------|-----------------------|--------------------------------------------------------------|
| 02-01 | Create Chat with Default Model → 201             | test_chat_crud.py     | TestCreateChat::test_create_chat_default_model               |
| 02-02 | Create Chat with Custom Model → 201              | test_chat_crud.py     | TestCreateChat::test_create_chat_with_model                  |
| 02-03 | Create Chat with Title → 201                     | test_chat_crud.py     | TestCreateChat::test_create_chat_with_title                  |
| 02-04 | Create Chat with Unknown Model → 400 `invalid_argument` (`INVALID_MODEL`) | test_chat_crud.py | TestCreateChat::test_create_chat_invalid_model |
| 02-05 | Get Chat → 200                                   | test_chat_crud.py     | TestGetChat::test_get_chat                                   |
| 02-06 | Get Chat Not Found → 404 `not_found`             | test_chat_crud.py     | TestGetChat::test_get_chat_not_found                         |
| 02-07 | List Chats with Cursor Pagination                | test_chat_crud.py     | TestListChats::test_list_chats, TestListChats::test_list_chats_pagination |
| 02-08 | Update Chat Title → 200, `updated_at` bumped     | test_chat_crud.py     | TestUpdateChat::test_update_title                            |
| 02-09 | Update Chat Not Found → 404                      | test_chat_crud.py     | TestUpdateChat::test_update_not_found                        |
| 02-10 | Delete Chat → 204, then GET/DELETE → 404, not listed | test_chat_crud.py | TestDeleteChat::test_delete_chat                             |
| 02-11 | Delete Chat Not Found → 404 `not_found`          | test_chat_crud.py     | TestDeleteChat::test_delete_not_found                        |
| 02-12 | Update Title — Whitespace-Only → 400 `invalid_argument` | test_chat_crud.py | TestUpdateChat::test_update_whitespace_title_rejected     |
| 02-13 | Update Title — 255 chars → 200, 256 → 400        | test_chat_crud.py     | TestUpdateChat::test_update_title_length_boundary            |
| 02-14 | Create Chat with Disabled Model → 400 `invalid_argument` (`INVALID_MODEL`) | test_chat_crud.py | TestCreateChat::test_create_chat_disabled_model |
| 02-15 | Create Title — 255 chars → 201, 256 → 400        | test_chat_crud.py     | TestCreateChat::test_create_chat_title_length_boundary       |
| 02-16 | List Chats — Unknown `$filter` Field → 400 `invalid_argument` | test_chat_crud.py | TestListChats::test_list_chats_unknown_filter_field_400 |
| 02-17 | List Chats — Malformed Cursor → 400 (`INVALID_CURSOR`) | test_chat_crud.py | TestListChats::test_list_chats_malformed_cursor_400          |
| 02-18 | List Ordered by Activity (send moves chat to top) | test_chat_crud.py    | TestListChats::test_send_moves_older_chat_to_top             |
| 02-19 | Update Without Title (schema-invalid) → 422 `invalid_argument` | test_chat_crud.py | TestUpdateChat::test_update_without_title_is_422     |
| 02-20 | Update with Malformed JSON → 400 `invalid_argument` | test_chat_crud.py  | TestUpdateChat::test_update_malformed_json_is_400            |
| 02-21 | Full Conversation Lifecycle (3 turns, history, `message_count`, turn status, delete) | test_full_scenario.py | TestFullConversationScenario::test_full_conversation |

## 03 — Messages API

| ID    | Scenario                                   | Test File         | Covered by                                         |
|-------|--------------------------------------------|-------------------|----------------------------------------------------|
| 03-01 | List Messages — Cursor Pagination          | test_messages.py  | TestMessages::test_cursor_pagination               |
| 03-02 | OData $select (Field Projection)           | —                 | N/A — `$select` is not supported (PRD, DESIGN)  |
| 03-03 | OData $orderby                             | test_messages.py  | TestMessages::test_odata_orderby                   |
| 03-04 | OData $filter                              | test_messages.py  | TestMessages::test_odata_filter_role               |
| 03-05 | Message request_id Always Non-Null         | test_messages.py  | TestMessages::test_request_id_non_null             |
| 03-06 | Attachments Array Always Present           | test_messages.py  | TestMessages::test_attachments_array_present       |
| 03-07 | my_reaction Field on Assistant Messages    | test_messages.py  | TestMessages::test_my_reaction_field               |
| 03-08 | User + Assistant Messages Share request_id | test_messages.py  | TestMessages::test_request_id_shared_per_turn      |
| 03-09 | Unknown `$filter` Field → 400 `invalid_argument` | test_messages.py | TestMessages::test_unknown_filter_field_400    |
| 03-10 | Messages of Nonexistent Chat → 404 `not_found` | test_messages.py | TestMessages::test_messages_of_nonexistent_chat_404 |
| 03-11 | Chat `message_count` Increments per Turn   | test_multi_turn.py | TestMultiTurn::test_message_count_increments      |
| 03-12 | Messages Ordered Chronologically           | test_multi_turn.py | TestMultiTurn::test_messages_ordered_chronologically |

## 04 — Streaming: Send Message

| ID    | Scenario                                   | Test File              | Covered by                                                    |
|-------|--------------------------------------------|------------------------|---------------------------------------------------------------|
| 04-01 | Send Message → 200 `text/event-stream`     | test_streaming.py      | TestStreamBasic::test_stream_returns_200_sse                  |
| 04-02 | Server Generates request_id if Omitted     | test_stream_started.py | TestStreamStartedOnSend::test_stream_started_has_request_id   |
| 04-03 | Client request_id Echoed in stream_started | test_stream_started.py | TestStreamStartedOnSend::test_stream_started_request_id_matches_client_id |
| 04-04 | Attachment ID Not a UUID → 422 `invalid_argument` | test_streaming.py | TestStreamPreflightErrors::test_malformed_attachment_id_rejected |
| 04-05 | Unknown Attachment ID → 400 `invalid_argument` (`invalid_attachment`), provider not called | test_streaming.py | TestStreamPreflightErrors::test_nonexistent_attachment_id_rejected |
| 04-06 | Empty Content → 400 `invalid_argument` (`EMPTY_CONTENT`) | test_streaming.py | TestStreamPreflightErrors::test_empty_content_rejected  |
| 04-07 | Missing Content (schema-invalid) → 422 `invalid_argument` | test_streaming.py | TestStreamPreflightErrors::test_missing_content_rejected |
| 04-08 | Chat Not Found → 404 `not_found` (JSON)    | test_streaming.py      | TestStreamPreflightErrors::test_chat_not_found                |
| 04-09 | Messages Persisted After Stream            | test_streaming.py      | TestMessages::test_messages_persisted_after_stream, TestMessages::test_user_message_content_matches |
| 04-10 | Assistant Message Has Token Counts         | test_streaming.py      | TestMessages::test_assistant_message_has_tokens               |
| 04-11 | Too Many Images per Message → 400 `out_of_range` | —                | GAP — no test sends more than `max_images_per_message` images |

## 05 — SSE Event Contract

| ID    | Scenario                                    | Test File              | Covered by                                                   |
|-------|---------------------------------------------|------------------------|--------------------------------------------------------------|
| 05-01 | stream_started: First Event with Fields     | test_stream_started.py | TestStreamStartedOnSend::test_stream_started_is_first_event, TestStreamStartedOnSend::test_stream_started_has_message_id |
| 05-02 | stream_started: is_new_turn=true on Send    | test_stream_started.py | TestStreamStartedOnSend::test_stream_started_is_new_turn_true_on_send |
| 05-03 | stream_started: is_new_turn=false on Replay | test_stream_started.py | TestStreamStartedOnReplay::test_replay_emits_stream_started_with_is_new_turn_false |
| 05-04 | Delta Events: type=text, content=string     | test_streaming.py      | TestStreamBasic::test_stream_has_delta_events, TestStreamBasic::test_stream_assembled_text_nonempty |
| 05-05 | Tool Events: phase/name/details             | test_web_search.py     | TestWebSearchBasic::test_web_search_returns_tool_events; test_code_interpreter.py TestCodeInterpreterToolEvents |
| 05-06 | Citations Event: items Array                | test_web_search.py     | TestWebSearchCitations                                       |
| 05-07 | Citations: No Provider IDs Exposed          | test_attachments.py    | TestUploadSearchCitationFlow::test_upload_search_citation_flow (online only) |
| 05-08 | Done Event: Core Fields                     | test_streaming.py      | TestStreamDoneEvent::test_done_event_contract                |
| 05-09 | Done Event: Usage Tokens (no internal token fields) | test_streaming.py | TestStreamDoneEvent::test_done_event_contract               |
| 05-10 | Done Event: quota_warnings Array            | test_quota_status.py   | TestQuotaWarningsInDoneEvent::test_done_event_has_quota_warnings |
| 05-11 | Done Event: Downgrade Fields                | test_quota_policy.py   | TestDowngrade                                                |
| 05-12 | Done Event: message_id NOT in done          | test_streaming.py      | TestStreamDoneEvent::test_done_event_contract                |
| 05-13 | Error Event: Terminal with Code             | test_error_mapping.py  | TestErrorMapping::test_post_stream_sse_error_event           |
| 05-14 | Error: Provider Details Sanitized           | test_error_mapping.py  | TestErrorMapping::test_error_message_no_provider_ids         |
| 05-15 | Ping Events only before the first content (ADR-0010) | test_streaming.py | TestStreamPing::test_ping_only_before_content              |
| 05-16 | Event Ordering Grammar                      | test_streaming.py      | TestStreamEventOrdering::test_no_events_after_terminal; test_stream_started.py TestStreamStartedOrdering::test_stream_started_before_deltas_before_done |
| 05-17 | Server Closes After Terminal                | —                      | (network-level, hard to e2e test)                            |
| 05-18 | Replay `done` Byte-Identical to Original    | —                      | N/A — not implemented (ADR-0010): replay rebuilds `done`, omits `downgrade_reason` and citations |

## 06 — Idempotency & Replay

| ID    | Scenario                                   | Test File              | Covered by                                                        |
|-------|--------------------------------------------|------------------------|-------------------------------------------------------------------|
| 06-01 | Replay Completed Turn → 200                | test_turns.py          | TestIdempotency::test_replay_completed_turn                       |
| 06-02 | Replay: is_new_turn=false, Same message_id | test_turns.py          | TestIdempotency::test_replay_completed_turn; test_stream_started.py TestStreamStartedOnReplay |
| 06-03 | Replay: No LLM Call, No Quota, No Outbox   | test_idempotency.py    | TestIdempotency::test_replay_does_not_modify_quota_or_call_provider; test_settlement.py TestSettlement::test_one_usage_outbox_event_per_turn |
| 06-04 | Multiple Replays Side-Effect-Free          | test_idempotency.py    | TestIdempotency::test_replay_does_not_modify_quota_or_call_provider (3 replays) |
| 06-05 | Running Turn + Same request_id → 409 `aborted` (`request_id_conflict`) | test_idempotency.py | TestIdempotency::test_running_turn_same_request_id_409 |
| 06-06 | Failed Turn + Same request_id → 409 `aborted` (`request_id_conflict`) | test_idempotency.py | TestIdempotency::test_failed_turn_same_request_id_409 |
| 06-07 | Cancelled Turn + Same request_id → 409 `aborted` (`request_id_conflict`) | test_idempotency.py | TestIdempotency::test_cancelled_turn_same_request_id_409 |
| 06-08 | Replay Priority Over Parallel Turn Check   | test_idempotency.py    | TestIdempotency::test_replay_priority_over_parallel_check         |
| 06-09 | Replay Does Not Modify Quota               | test_settlement.py     | TestSettlement::test_replay_does_not_charge_again                 |
| 06-10 | request_id of a Turn Replaced by Retry → 409 `aborted` (`request_id_conflict`) | test_idempotency.py | TestIdempotency::test_request_id_replaced_by_retry_409 |

## 07 — Parallel Turn Enforcement

| ID    | Scenario                                    | Test File             | Covered by                                                  |
|-------|---------------------------------------------|-----------------------|-------------------------------------------------------------|
| 07-01 | Partial Unique Index                        | —                     | (DB-level, not e2e testable)                                |
| 07-02 | Second Stream → 409 `aborted` (`turn_already_running`) | test_parallel_turn.py | TestParallelTurn::test_second_stream_409_turn_already_running |
| 07-03 | New Stream Succeeds After Previous Terminal | test_parallel_turn.py | TestParallelTurn::test_new_stream_succeeds_after_terminal   |

## 08 — Turn Mutations

| ID    | Scenario                                    | Test File              | Covered by                                                    |
|-------|---------------------------------------------|------------------------|---------------------------------------------------------------|
| 08-01 | Retry Latest Terminal Turn                  | test_turn_mutations.py | TestTurnRetry::test_retry_replaces_the_answer                 |
| 08-02 | Retry Running Turn → 400 `failed_precondition` (`turn_state`/`STATE`) | test_turn_mutations.py | TestTurnRetry::test_retry_running_turn_400 |
| 08-03 | Retry Non-Latest Turn → 409 `aborted` (`NOT_LATEST_TURN`) | test_turn_mutations.py | TestTurnRetry::test_retry_non_latest_turn_409   |
| 08-04 | Retry Generates New request_id              | test_stream_started.py | TestStreamStartedOnMutation::test_retry_emits_stream_started_with_new_request_id |
| 08-05 | Edit: Replace Content + Regenerate          | test_turn_mutations.py | TestTurnEdit::test_edit_replaces_user_message_and_answer      |
| 08-06 | Edit SSE Contract Identical to Stream       | test_stream_started.py | TestStreamStartedOnMutation::test_edit_emits_stream_started_with_new_request_id |
| 08-07 | Delete Last Turn → 204                      | test_turn_mutations.py | TestTurnDelete::test_delete_last_turn_204                     |
| 08-08 | Delete Running Turn → 400 `failed_precondition` (`turn_state`/`STATE`) | test_turn_mutations.py | TestTurnDelete::test_delete_running_turn_400 |
| 08-09 | Delete Non-Latest Turn → 409 `aborted` (`NOT_LATEST_TURN`) | test_turn_mutations.py | TestTurnDelete::test_delete_non_latest_turn_409 |
| 08-10 | Soft-Deleted Turn Not in Messages           | test_turn_mutations.py | TestTurnDelete::test_soft_deleted_turn_excluded_from_messages |
| 08-11 | Concurrent Retries: one 200, the other 409 `aborted` | test_turn_mutations.py | TestConcurrentRetries::test_concurrent_retries_one_wins |
| 08-12 | Retry Failed or Cancelled Turn              | test_turn_mutations.py | TestTurnRetry::test_retry_failed_turn, TestTurnRetry::test_retry_cancelled_turn; test_stream_started.py TestCancelledMessagePersistence::test_retry_cancelled_turn_produces_new_message |
| 08-13 | Old Turn Marked with replaced_by_request_id | test_turn_mutations.py | TestReplacedByRequestId::test_replaced_by_request_id_set      |
| 08-14 | Edit with Empty Content → 400 `invalid_argument` (`EMPTY_CONTENT`) | test_turn_mutations.py | TestTurnEdit::test_edit_empty_content_400 |
| 08-15 | Edit Non-Latest Turn → 409 `aborted` (`NOT_LATEST_TURN`) | test_turn_mutations.py | TestTurnEdit::test_edit_non_latest_turn_409     |
| 08-16 | GET Deleted Turn → 404 `not_found`          | test_turn_mutations.py | TestTurnDelete::test_get_deleted_turn_404                     |
| 08-17 | Second Delete of a Turn → 409 `aborted` (`NOT_LATEST_TURN`) | test_turn_mutations.py | TestTurnDelete::test_second_delete_turn_409_not_latest |
| 08-18 | Deleted Turn Not Sent to Provider           | test_turn_mutations.py | TestTurnDelete::test_deleted_turn_not_sent_to_provider        |
| 08-19 | Retry of Old Turn While Its Retry Streams → 409 `aborted` (`NOT_LATEST_TURN`) | test_turn_mutations.py | TestConcurrentRetries::test_retry_while_retry_running_409_not_latest |

## 09 — Turn Lifecycle

| ID    | Scenario                                    | Test File              | Covered by                                                   |
|-------|---------------------------------------------|------------------------|--------------------------------------------------------------|
| 09-01 | Turn Row Created Before SSE Opens (`running` while streaming) | test_turn_mutations.py | TestTurnRetry::test_retry_running_turn_400 |
| 09-02 | Preflight Rejection → JSON Error, No Turn Row | test_quota_policy.py | TestQuotaExhaustion::test_all_tiers_exhausted_429            |
| 09-03 | Atomic User Message + Turn                  | —                      | GAP — a failure between the two inserts cannot be injected over HTTP |
| 09-04 | Immutable Quota Fields Persisted            | test_full_scenario.py  | TestTurnDetailsInDb::test_max_output_tokens_applied; test_settlement.py TestSettlement::test_reservation_snapshot_persisted |
| 09-05 | Completed → assistant_message_id Set        | test_turns.py          | TestTurnStatus::test_turn_completed_after_stream             |
| 09-06 | Cancelled With Content → Partial Message    | test_stream_started.py | TestCancelledMessagePersistence::test_cancelled_turn_has_assistant_message_id |
| 09-07 | Cancelled Without Content → message_id NULL | test_turn_lifecycle.py | TestTurnLifecycle::test_cancelled_without_content_null_message_id |
| 09-08 | Failed Turn → message_id NULL               | test_turn_lifecycle.py | TestTurnLifecycle::test_failed_turn_null_message_id          |
| 09-09 | Cancelled Message in GET /messages          | test_stream_started.py | TestCancelledMessagePersistence::test_cancelled_message_content_equals_received_deltas |
| 09-10 | CAS Prevents Double Finalization            | test_turn_lifecycle.py | TestTurnLifecycle::test_done_turn_state_stable_after_completion (effect only); CAS: unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs |
| 09-11 | Turn State Machine: running → terminal      | test_turn_lifecycle.py | TestTurnLifecycle                                            |
| 09-12 | GET Unknown Turn → 404 `not_found`          | test_turns.py          | TestTurnStatus::test_turn_not_found                          |
| 09-13 | Client Disconnect Under Backpressure → `cancelled` | —               | unit tests: gears/mini-chat/mini-chat/src/domain/service/stream_service/mod.rs (cancel-token path; the failed-send path has no dedicated test) |

## 10 — Attachments

Upload is synchronous in P1: `POST /attachments` returns 201 with `status: ready`; polling
still works but is not required (ADR-0007).

| ID    | Scenario                                     | Test File                | Covered by                                                  |
|-------|----------------------------------------------|--------------------------|-------------------------------------------------------------|
| 10-01 | Upload Attachment → 201                      | test_attachments.py      | TestUploadAndGet::test_upload_and_get_attachment            |
| 10-02 | GET Attachment — Status `ready`              | test_attachments.py      | TestUploadAndGet::test_upload_and_get_attachment            |
| 10-03 | DELETE Attachment → 204, GET → 404           | test_attachments.py      | TestDeleteAndVerifyGone::test_delete_and_verify_gone        |
| 10-04 | DELETE Referenced Attachment → 409 `already_exists` (`attachment_locked`) | test_attachments.py | TestDeleteReferencedAttachment::test_delete_referenced_attachment_409 |
| 10-05 | Unsupported MIME → 400 `invalid_argument` (`UNSUPPORTED_CONTENT_TYPE`) | test_attachments.py | TestUploadInvalidType::test_upload_invalid_type_rejected |
| 10-06 | Oversize Image → 400 `out_of_range` (`FILE_TOO_LARGE`) | test_attachments.py | TestUploadSizeEnforcement::test_oversize_image_rejected |
| 10-07 | Oversize Document → 400 `out_of_range` (`FILE_TOO_LARGE`) | test_attachments.py | TestUploadSizeEnforcement::test_oversize_document_rejected_by_gateway |
| 10-08 | Document Within Limit → 201 + Ready          | test_attachments.py      | TestUploadSizeEnforcement::test_document_within_limit_succeeds |
| 10-09 | size_bytes Matches Actual                    | test_attachments.py      | TestUploadSizeBytesAccuracy::test_size_bytes_matches_actual |
| 10-10 | MIME Inference from Extension                | test_code_interpreter.py | TestXlsxOctetStreamInference::test_xlsx_octet_stream_accepted |
| 10-11 | Kind Routing: XLSX→code_interpreter, TXT→file_search | test_code_interpreter.py | TestXlsxPurposeRouting                          |
| 10-12 | Image Upload: kind=image                     | test_attachments.py      | TestImageUploadAndSend::test_image_upload_and_send          |
| 10-13 | Multi-Provider Upload                        | test_attachments.py      | TestDualProviderUpload::test_dual_provider_upload; TestDualProviderRAGStream::test_dual_provider_rag_stream (online only) |
| 10-14 | doc_summary: Async for Docs, Null for Images | —                        | N/A — not implemented (ADR-0007): `doc_summary` is always `null` |
| 10-15 | img_thumbnail Present for Ready Images       | test_attachments.py      | TestImageUploadAndSend::test_image_upload_and_send (non-null only; format not checked) |
| 10-16 | error_code on Failed Status                  | —                        | GAP — no test forces a provider upload failure              |
| 10-17 | Mid-Stream Size Limit → 400 `out_of_range`   | —                        | GAP — the client always sends Content-Length, so the early check rejects first |
| 10-18 | Content-Length Early Check → 400 `out_of_range` | test_attachments.py   | TestUploadSizeEnforcement::test_oversize_image_rejected, TestUploadSizeEnforcement::test_oversize_document_rejected_by_gateway |
| 10-19 | Chunked-Encoding Streaming Counter           | —                        | GAP — no test uploads with chunked transfer encoding        |
| 10-20 | Images Not Added to Vector Store             | test_attachments.py      | TestImageUploadAndSend::test_image_upload_and_send          |
| 10-21 | provider_file_id Never Exposed               | test_attachments.py      | TestUploadAndGet::test_provider_storage_fields_not_exposed  |
| 10-22 | Stream with Document → file_search           | test_attachments.py      | TestSendMessageWithAttachments::test_send_message_with_attachments; TestUploadSearchCitationFlow::test_upload_search_citation_flow (online only) |
| 10-23 | Mixed XLSX + TXT → Both Tools                | test_code_interpreter.py | TestMixedAttachments::test_mixed_xlsx_and_txt_both_tools_in_request |
| 10-24 | Image + Document Combined                    | test_attachments.py      | TestDocumentAndImageTogether::test_document_and_image_combined (online only) |
| 10-25 | GET Nonexistent Attachment → 404 `not_found` | test_attachments.py      | TestUploadAndGet::test_get_nonexistent_attachment_404       |
| 10-26 | Documents per Chat Exceeded → 429 `resource_exhausted` (`document_limit`) | — | GAP — not exercised end to end; mapping covered by unit tests in gears/mini-chat/mini-chat/src/api/rest/error.rs |
| 10-27 | Storage per Chat Exceeded → 429 `resource_exhausted` (`storage_limit`) | — | GAP — not exercised end to end; mapping covered by unit tests in gears/mini-chat/mini-chat/src/api/rest/error.rs |
| 10-28 | Max Indexed Chunks per Chat                  | —                        | N/A — not implemented (ADR-0007)                            |
| 10-29 | Deleted Document Immediately Excluded from file_search | —              | N/A — not implemented (ADR-0007)                            |
| 10-30 | XLSX Upload Accepted and Ready               | test_code_interpreter.py | TestXlsxUploadAccepted                                      |
| 10-31 | Code Interpreter Tool Events in Stream       | test_code_interpreter.py | TestCodeInterpreterToolEvents, TestCodeInterpreterEventOrdering::test_tool_events_before_done |
| 10-32 | code_interpreter Tool with container.file_ids in Provider Request | test_code_interpreter.py | TestCodeInterpreterProviderRequest::test_code_interpreter_tool_in_request |
| 10-33 | Code Interpreter Real Answer                 | test_code_interpreter.py | TestCodeInterpreterOnline::test_xlsx_code_interpreter_produces_answer (online only) |
| 10-34 | Image Recognition by the Model               | test_attachments.py      | TestImageRecognition::test_image_recognition_cat (online only) |
| 10-35 | Per-Provider Send with Attachment; Medium File Pipeline | test_attachments.py | TestProviderSendMessageWithAttachment::test_send_message_with_attachment (online only), TestUploadStreamingPipeline::test_medium_file_upload_and_stream (online only) |

## 11 — Models API

| ID    | Scenario                    | Test File      | Covered by                                        |
|-------|-----------------------------|----------------|---------------------------------------------------|
| 11-01 | List Models                 | test_models.py | TestListModels::test_list_models                  |
| 11-02 | Catalog Models Present      | test_models.py | TestListModels::test_catalog_models_present       |
| 11-03 | Model Has Required Fields   | test_models.py | TestListModels::test_model_has_required_fields    |
| 11-04 | Get Existing Model → 200    | test_models.py | TestGetModel::test_internal_fields_not_exposed, TestGetModel::test_extended_response_fields |
| 11-05 | Get Nonexistent Model → 404 `not_found` | test_models.py | TestGetModel::test_get_nonexistent_model  |
| 11-06 | Internal Fields Not Exposed | test_models.py | TestGetModel::test_internal_fields_not_exposed    |
| 11-07 | Disabled Model Not Listed   | test_models.py | TestDisabledModel::test_disabled_model_not_listed |
| 11-08 | Extended Response Fields    | test_models.py | TestGetModel::test_extended_response_fields       |
| 11-09 | Get Disabled Model → 404    | test_models.py | TestDisabledModel::test_get_disabled_model_404    |

## 12 — Reactions API

| ID    | Scenario                       | Test File         | Covered by                                             |
|-------|--------------------------------|-------------------|--------------------------------------------------------|
| 12-01 | Set Reaction (like) → 200      | test_reactions.py | TestReactions::test_set_reaction_like                  |
| 12-02 | Reaction Upsert Idempotent     | test_reactions.py | TestReactions::test_put_same_reaction_twice_is_idempotent |
| 12-03 | Reaction on User Message → 400 `failed_precondition` (`reaction_target`/`STATE`) | test_reactions.py | TestReactions::test_reaction_on_user_message_400 |
| 12-04 | Remove Reaction → 204          | test_reactions.py | TestReactions::test_remove_reaction_204                |
| 12-05 | Remove Reaction Idempotent → 204 | test_reactions.py | TestReactions::test_remove_reaction_idempotent       |
| 12-06 | Switch Reaction like → dislike | test_reactions.py | TestReactions::test_switch_reaction_like_to_dislike    |
| 12-07 | Reaction on Nonexistent Message → 404 `not_found` (PUT and DELETE) | test_reactions.py | TestReactions::test_reaction_on_nonexistent_message_404 |

## 13 — Quota Status API

| ID    | Scenario                                  | Test File            | Covered by                                                   |
|-------|-------------------------------------------|----------------------|--------------------------------------------------------------|
| 13-01 | Quota Status Endpoint Structure           | test_quota_status.py | TestQuotaStatusEndpoint::test_returns_200_with_tiers_and_threshold |
| 13-02 | Each Tier Has Periods                     | test_quota_status.py | TestQuotaStatusEndpoint::test_each_tier_has_periods          |
| 13-03 | remaining_percentage in [0, 100]          | test_quota_status.py | TestQuotaStatusEndpoint::test_remaining_percentage_is_valid  |
| 13-04 | next_reset Is Future                      | test_quota_status.py | TestQuotaStatusEndpoint::test_next_reset_is_future           |
| 13-05 | Credits Increase After Send               | test_quota_status.py | TestQuotaUsageTracking::test_used_credits_increase_after_send |
| 13-06 | remaining_credits_micro Decreases After Send | test_quota_status.py | TestQuotaUsageTracking::test_remaining_credits_decrease_after_send |
| 13-07 | SSE quota_warnings Consistent with REST   | test_quota_status.py | TestQuotaWarningsInDoneEvent::test_quota_warnings_consistent_with_endpoint; test_quota_policy.py TestQuotaStatusFlags::test_done_quota_warnings_match_status |
| 13-08 | Warning Fires at Threshold Boundary       | test_quota_policy.py | TestQuotaStatusFlags::test_total_daily_flags                 |
| 13-09 | Exhausted Flag at Zero                    | test_quota_policy.py | TestQuotaStatusFlags::test_total_daily_flags                 |
| 13-10 | Usage Accounted per User                  | test_isolation.py    | TestQuotaIsolation::test_other_user_usage_is_not_charged     |

## 14 — Quota Enforcement

| ID    | Scenario                                  | Test File                 | Covered by                                                   |
|-------|-------------------------------------------|---------------------------|--------------------------------------------------------------|
| 14-01 | Preflight Reserve Persisted               | test_settlement.py        | TestSettlement::test_reservation_snapshot_persisted; test_full_scenario.py TestTurnDetailsInDb::test_max_output_tokens_applied |
| 14-02 | Tier Downgrade: Premium Exhausted → Standard | test_quota_policy.py   | TestDowngrade::test_premium_exhausted_downgrades_to_standard |
| 14-03 | Bucket Model: total + tier:premium        | test_quota_enforcement.py | TestQuotaEnforcement::test_bucket_model_premium_counts_total, TestQuotaEnforcement::test_bucket_model_standard_counts_total |
| 14-04 | Daily + Monthly Periods Both Checked      | —                         | GAP — tests exhaust all periods at once; none exhausts only daily or only monthly |
| 14-05 | All Tiers Exhausted → 429 `resource_exhausted` (`tokens`) | test_quota_policy.py | TestQuotaExhaustion::test_all_tiers_exhausted_429 |
| 14-06 | Reserve Before Provider Call              | test_quota_policy.py      | TestQuotaExhaustion::test_all_tiers_exhausted_429 (rejected reserve: provider not called) |
| 14-07 | Credits Formula: Integer Arithmetic       | test_quota_enforcement.py | TestQuotaEnforcement::test_bucket_model_premium_counts_total, TestQuotaEnforcement::test_bucket_model_standard_counts_total; test_settlement.py TestSettlement::test_completed_turn_charged_once |
| 14-08 | max_output_tokens Hard Cap                | test_full_scenario.py     | TestTurnDetailsInDb::test_max_output_tokens_applied          |
| 14-09 | policy_version_applied Persisted          | test_quota_enforcement.py | TestQuotaEnforcement::test_policy_version_persisted_per_turn |
| 14-10 | Settlement Uses Persisted Policy          | —                         | GAP — needs a second policy version; the static plugin has a fixed version (ADR-0008) |
| 14-11 | No Stuck Reserves After Completion        | test_full_scenario.py     | TestQuotaAccumulation::test_no_stuck_reserves_after_completion |
| 14-12 | Web Search Surcharge in Reserve           | —                         | GAP — the reserve of a web-search turn is not asserted       |
| 14-13 | warning_threshold_pct Configurable        | —                         | GAP (config-level; only the default 80 is exercised by test_quota_policy.py TestQuotaStatusFlags) |
| 14-14 | Invalid Threshold → Gear Fails to Start   | —                         | GAP (startup test)                                           |
| 14-15 | Retry While Exhausted → 429, Old Turn Kept | test_quota_policy.py     | TestQuotaExhaustion::test_retry_while_exhausted_keeps_old_turn |
| 14-16 | Disabled Chat Model → Downgrade (`model_disabled`) | test_quota_policy.py | TestDowngrade::test_disabled_chat_model_downgrades     |
| 14-17 | Web Search Daily Quota → 429 `resource_exhausted` (`web_search`) only for web-search requests | test_quota_policy.py | TestWebSearchDailyQuota |
| 14-18 | Web Search Turn Credits and Tokens        | test_web_search_usage.py  | TestWebSearchUsageAccounting                                 |
| 14-19 | Code Interpreter Turn Credits and `code_interpreter_calls` | test_code_interpreter_usage.py | TestCodeInterpreterUsageAccounting |
| 14-20 | Code Interpreter Daily Quota → 429        | —                         | GAP — no test seeds `code_interpreter_calls` to the daily quota |
| 14-21 | Per-User Daily Image-Input Quota          | —                         | N/A — not implemented (ADR-0008)                             |
| 14-22 | Per-User Daily file_search Limit          | —                         | N/A — not implemented (ADR-0007)                             |

## 15 — Settlement & Finalization

| ID    | Scenario                                 | Test File          | Covered by                                                      |
|-------|------------------------------------------|--------------------|-----------------------------------------------------------------|
| 15-01 | CAS Guard: First Terminal Wins           | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs |
| 15-02 | CAS Loser Exits Without Side Effects     | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs |
| 15-03 | Completed: Actual Settlement             | test_settlement.py | TestSettlement::test_completed_turn_charged_once                |
| 15-04 | Overshoot ≤ Tolerance → Commit Actual    | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 15-05 | Overshoot > Tolerance → Cap at Reserve   | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 15-06 | Cancelled With Usage → Reserve Released  | test_settlement.py | TestSettlement::test_cancelled_with_usage                       |
| 15-07 | Cancelled Without Usage → Reserve Released | test_settlement.py | TestSettlement::test_cancelled_without_usage                  |
| 15-08 | Provider Failure → Reserve Released      | test_settlement.py | TestSettlement::test_provider_http_error_releases_reserve       |
| 15-09 | Orphan Timeout → Estimated Settlement    | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs, gears/mini-chat/mini-chat/src/infra/db/repo/turn_repo.rs |
| 15-10 | Atomic: CAS + Quota + Outbox             | —                  | unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs |
| 15-11 | Outbox Dedupe Key Format                 | test_settlement.py | TestSettlement::test_one_usage_outbox_event_per_turn            |
| 15-12 | Duplicate Outbox Insert Ignored          | —                  | GAP — a duplicate insert cannot be triggered over HTTP          |
| 15-13 | One Outbox Message Per Terminal Turn     | test_settlement.py | TestSettlement::test_one_usage_outbox_event_per_turn            |
| 15-14 | Completed Turn Releases Reserve          | test_settlement.py | TestSettlement::test_completed_turn_releases_reserve            |
| 15-15 | Settlement with Real Provider Token Counts | test_settlement.py | TestSettlementPerProvider::test_completed_settlement_per_provider (online only) |
| 15-16 | Finalization Failure → SSE `error` (`finalization_failed` / `message_persistence_failed`), not `done` | — | unit tests: gears/mini-chat/mini-chat/src/domain/service/stream_service/mod.rs |

## 16 — Context Assembly

| ID    | Scenario                                 | Test File                | Covered by                                                   |
|-------|------------------------------------------|--------------------------|--------------------------------------------------------------|
| 16-01 | System Prompt Delivered                  | test_context_assembly.py | TestSystemPrompt::test_system_prompt_sent_as_instructions; TestSystemPrompt::test_ping_pong_proves_system_prompt (online only) |
| 16-02 | System Prompt Across Models              | test_context_assembly.py | TestSystemPrompt::test_system_prompt_sent_as_instructions (both providers) |
| 16-03 | Recent Messages: Up to K                 | test_context_assembly.py | TestContextInputTokenGrowth, TestContextMessageTokens::test_message_input_tokens_increase |
| 16-04 | Deleted Turns Excluded                   | test_turn_mutations.py   | TestTurnDelete::test_deleted_turn_not_sent_to_provider       |
| 16-05 | Thread Summary Replaces Older Messages   | test_live_smoke.py       | TestLiveThreadSummaryTrigger::test_summary_trigger_fires_and_applied_on_next_turn (online only) |
| 16-06 | Only Messages After Summary Boundary     | —                        | GAP — not asserted end to end; unit tests in gears/mini-chat/mini-chat/src/domain/service/context_assembly.rs |
| 16-07 | Model Recall from Earlier Turns          | test_context_assembly.py | TestContextRecall (online only); test_multi_turn.py TestMultiTurn::test_two_turns_in_sequence (online only) |
| 16-08 | web_search Tool with search_context_size | test_provider_request.py | TestWebSearchToolType::test_web_search_tool_type_is_web_search, TestWebSearchToolType::test_web_search_has_search_context_size |
| 16-09 | file_search Tool with max_num_results    | test_provider_request.py | TestFileSearchMaxNumResults::test_file_search_has_max_num_results |
| 16-10 | web_search Disabled → No Tool            | test_provider_request.py | TestWebSearchToolType::test_no_web_search_tool_without_flag; test_web_search.py TestWebSearchDisabledByDefault |
| 16-11 | max_tool_calls in Provider Request       | test_provider_request.py | TestMaxToolCalls                                             |
| 16-12 | Cancelled Partial Message in Context     | —                        | GAP — no test inspects the provider request after a cancelled turn |
| 16-13 | Empty Cancelled Turn → No Message        | —                        | GAP — no test inspects the provider request after an empty cancelled turn |
| 16-14 | Tool Guard Instructions Appended         | —                        | GAP — the captured `instructions` are compared only with the catalog prompt |
| 16-15 | Missing System Prompt → None             | —                        | GAP — every catalog model in the rig has a system prompt     |

## 17 — Error Mapping & Sanitization

| ID    | Scenario                              | Test File             | Covered by                                                  |
|-------|---------------------------------------|-----------------------|-------------------------------------------------------------|
| 17-01 | Pre-Stream Errors → Problem JSON      | test_streaming.py     | TestStreamPreflightErrors                                   |
| 17-02 | Post-Stream → SSE event: error        | test_error_mapping.py | TestErrorMapping::test_post_stream_sse_error_event          |
| 17-03 | Provider Timeout → provider_timeout   | —                     | GAP — the gear's request timeout (30 s) does not fit the test budget; a provider HTTP 504 is `provider_error` (test_error_mapping.py TestErrorMapping::test_provider_504_is_provider_error) |
| 17-04 | Provider Unavailable → provider_error | test_error_mapping.py | TestErrorMapping::test_provider_unavailable_error_code      |
| 17-05 | Provider 429 → rate_limited           | test_error_mapping.py | TestErrorMapping::test_rate_limited_error_code              |
| 17-06 | Error Sanitization: No Provider IDs   | test_error_mapping.py | TestErrorMapping::test_error_message_no_provider_ids        |
| 17-07 | 404 Masking: Foreign Resource → 404   | test_isolation.py     | TestIsolation::test_foreign_resource_is_404                 |
| 17-08 | Schema-Invalid Body → 422 `invalid_argument` | test_streaming.py | TestStreamPreflightErrors::test_missing_content_rejected; test_chat_crud.py TestUpdateChat::test_update_without_title_is_422 |
| 17-09 | Malformed JSON → 400 `invalid_argument` | test_chat_crud.py   | TestUpdateChat::test_update_malformed_json_is_400           |

## 18 — Web Search

| ID    | Scenario                             | Test File                | Covered by                                                   |
|-------|--------------------------------------|--------------------------|--------------------------------------------------------------|
| 18-01 | Web Search Tool Events               | test_web_search.py       | TestWebSearchBasic                                           |
| 18-02 | Web Search Citations                 | test_web_search.py       | TestWebSearchCitations                                       |
| 18-03 | Citations Before Done                | test_web_search.py       | TestWebSearchEventOrdering                                   |
| 18-04 | No Tools Without web_search Flag     | test_web_search.py       | TestWebSearchDisabledByDefault                               |
| 18-05 | Works on Standard Model              | test_web_search.py       | TestWebSearchBasic (the `openai` parameter runs on `gpt-5.2`, standard tier) |
| 18-06 | Turn Done After Web Search           | test_web_search.py       | TestWebSearchTurnStatus::test_turn_done_after_web_search     |
| 18-07 | Messages Persisted After Web Search  | test_web_search.py       | TestWebSearchTurnStatus::test_messages_persisted_after_web_search |
| 18-08 | Credits Tracked for Web Search Turns | test_web_search_usage.py | TestWebSearchUsageAccounting::test_web_search_usage_correct  |
| 18-09 | disable_web_search Kill Switch       | —                        | unit tests: gears/mini-chat/mini-chat/src/domain/service/quota_service.rs |
| 18-10 | Web Search Call Limits               | test_quota_policy.py     | TestWebSearchDailyQuota (daily quota); test_provider_request.py TestMaxToolCalls (per-turn cap) |
| 18-11 | Meaningful Answer                    | test_web_search.py       | TestWebSearchOnline (online only)                            |

## 19 — Cleanup & Recovery

| ID    | Scenario                               | Test File       | Covered by                                                        |
|-------|----------------------------------------|-----------------|-------------------------------------------------------------------|
| 19-01 | Chat Deletion → Background Cleanup     | test_cleanup.py | TestCleanup::test_deleted_chat_hides_chat_and_attachment, TestCleanupWorkerDB::test_chat_deletion_enqueues_chat_cleanup_event |
| 19-02 | Cleanup Marks Attachments Pending      | test_cleanup.py | TestCleanupWorkerDB::test_chat_deletion_marks_attachments_for_cleanup |
| 19-03 | Provider 404 on Delete → Success       | —               | GAP — the mock provider does not return 404 on file delete in any test |
| 19-04 | Vector Store Deleted After Attachments | —               | GAP — provider delete calls are not asserted                      |
| 19-05 | Attachment Cleanup State Machine       | test_cleanup.py | TestCleanupWorkerDB::test_chat_deletion_with_multiple_attachments |
| 19-06 | Orphan Watchdog Detects Stuck Turns    | —               | unit tests: gears/mini-chat/mini-chat/src/infra/db/repo/turn_repo.rs, gears/mini-chat/mini-chat/src/infra/workers/orphan_watchdog.rs (minimum timeout 90 s) |
| 19-07 | Orphan → Estimated Settlement + Outbox | —               | unit tests: gears/mini-chat/mini-chat/src/domain/service/finalization_service.rs |
| 19-08 | Crash Recovery: Turn Status API        | —               | GAP — needs a server restart mid-stream                           |
| 19-09 | Thread Summary Trigger                 | test_live_smoke.py | TestLiveThreadSummaryTrigger::test_summary_trigger_fires_and_applied_on_next_turn (online only) |
| 19-10 | Thread Summary Worker                  | test_live_smoke.py | TestLiveThreadSummaryTrigger::test_summary_trigger_fires_and_applied_on_next_turn (online only) |
| 19-11 | Attachment Deletion Enqueues Cleanup Event | test_cleanup.py | TestCleanupWorkerDB::test_attachment_deletion_enqueues_cleanup_event |
| 19-12 | Second Chat Delete → 404, Single Cleanup Event | test_cleanup.py | TestCleanupWorkerDB::test_second_delete_chat_404_single_cleanup_event |
| 19-13 | Empty Chat Deletion Still Enqueues Cleanup | test_cleanup.py | TestCleanupWorkerDB::test_chat_without_attachments_still_enqueues |
| 19-14 | Hard Purge After Grace Period          | —               | N/A — not implemented (ADR-0009)                                  |
| 19-15 | Audit Event for Chat Deletion          | —               | N/A — not implemented (ADR-0009)                                  |
| 19-16 | Chat Deletion Cancels Running Turn     | —               | N/A — not implemented (ADR-0009)                                  |

## 20 — Authorization

| ID    | Scenario                                   | Test File         | Covered by                                                 |
|-------|--------------------------------------------|-------------------|------------------------------------------------------------|
| 20-01 | PEP Before Every Operation                 | test_isolation.py | TestAuthentication, TestIsolation (every public operation) |
| 20-02 | PDP Unreachable → 403 Fail-Closed          | —                 | GAP — the rig cannot make the PDP unreachable; mapping covered by unit tests in gears/mini-chat/mini-chat/src/domain/error.rs |
| 20-03 | Foreign Resource → 404, Provider Not Called | test_isolation.py | TestIsolation::test_foreign_resource_is_404               |
| 20-04 | Constraints Compiled to SQL WHERE          | test_isolation.py | TestIsolation::test_foreign_chat_not_listed                |
| 20-05 | Missing Token → 401                        | test_isolation.py | TestAuthentication::test_missing_token_is_401              |
| 20-06 | Unknown Token → 401                        | test_isolation.py | TestAuthentication::test_unknown_token_is_401              |
| 20-07 | Foreign Mutations Leave Owner Data Unchanged | test_isolation.py | TestIsolation::test_owner_resources_unchanged            |
