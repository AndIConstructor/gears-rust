# Phase 7: Tests — Unit + Integration

## Goal

Comprehensive test coverage for the thread summary trigger, handler, and repository layer.

## Test Categories

### 7.1 Unit tests — `should_trigger_summary`

File: `src/domain/service/finalization_service.rs` (test module)

| Test | Input | Expected |
|------|-------|----------|
| `proactive_fires_above_threshold_no_summary` | `assembled_context_tokens=2000, ctx=4096, max_out=1024, threshold=60, truncated=false, has_summary=false` → budget=3072, 60%=1843 | `true` |
| `proactive_skipped_below_threshold` | `assembled_context_tokens=1000` (same budget) | `false` |
| `proactive_suppressed_when_summary_exists` | `assembled_context_tokens=2000, has_summary=true, truncated=false` | `false` |
| `urgent_fires_when_truncated_even_with_summary` | `assembled_context_tokens=500, truncated=true, has_summary=true` | `true` |
| `non_positive_budget_returns_false` | `max_output >= context_window` | `false` |
| `zero_tokens_returns_false` | `assembled_context_tokens=0` | `false` |

### 7.2 Unit tests — `build_summary_prompt`

File: `src/infra/workers/thread_summary_worker.rs` (test module)

| Test | Input | Expected |
|------|-------|----------|
| `prompt_without_existing_summary` | `existing=None, messages=[user:"hi", assistant:"hello"]` | Contains "Messages to Summarize", both messages |
| `prompt_with_existing_summary` | `existing=Some("prev summary"), messages=[...]` | Contains "Existing Summary", "New Messages to Incorporate" |
| `prompt_empty_messages` | `existing=None, messages=[]` | No message section (edge case) |

### 7.3 Unit tests — `build_system_usage_event`

File: `src/infra/workers/thread_summary_worker.rs` (test module)

| Test | Input | Expected |
|------|-------|----------|
| `usage_event_fields` | payload with known IDs | `requester_type="system"`, `user_id=None`, `dedupe_key` format correct |
| `dedupe_key_format` | `tenant=abc, system_request_id=def` | `"abc/thread_summary_update/def"` (hex-normalized) |

### 7.4 Unit tests — `ThreadSummaryTaskPayload` serialization

File: `src/domain/repos/outbox_enqueuer.rs` (test module)

| Test | Input | Expected |
|------|-------|----------|
| `round_trip_with_base_frontier` | Payload with Some base frontier | Deserializes back to equal |
| `round_trip_without_base_frontier` | Payload with None base frontier | Deserializes back to equal |
| `deserialization_from_invalid_json` | `b"{invalid}"` | `Err` |

### 7.5 Integration tests — Repository layer

File: `src/infra/db/repo/thread_summary_repo.rs` (test module) or `repo_test.rs`

**`get_latest`:**
| Test | Setup | Expected |
|------|-------|----------|
| `get_latest_returns_none_when_empty` | No rows | `Ok(None)` |
| `get_latest_returns_summary` | Insert one summary row | `Ok(Some(...))` with correct fields |

**`upsert_with_cas`:**
| Test | Setup | Expected |
|------|-------|----------|
| `first_summary_inserts` | No existing row, `base=None` | Returns 1, row exists |
| `first_summary_conflict` | Row already exists, `base=None` | Returns 0 (ON CONFLICT DO NOTHING) |
| `advance_frontier_succeeds` | Existing row with frontier A, `base=A` | Returns 1, frontier=B |
| `advance_frontier_cas_conflict` | Existing row with frontier B, `base=A` | Returns 0 |

**`mark_messages_compressed`:**
| Test | Setup | Expected |
|------|-------|----------|
| `marks_range_compressed` | 5 messages, mark range [2..4] | Messages 2-4 have `is_compressed=true` |
| `skips_deleted_messages` | Message 3 is soft-deleted | Message 3 not marked |
| `skips_already_compressed` | Message 2 already compressed | Not double-marked |
| `first_summary_marks_all_up_to_target` | `base=None`, target=msg3 | Messages 1-3 compressed |

**`fetch_messages_in_range`:**
| Test | Setup | Expected |
|------|-------|----------|
| `returns_messages_in_order` | 5 messages | Correct `(created_at, id)` order |
| `excludes_deleted` | Message 3 deleted | Not in result |
| `excludes_compressed` | Message 2 compressed | Not in result |
| `excludes_after_target` | Target = msg3 | Messages 4-5 excluded |
| `first_summary_includes_from_beginning` | `base=None`, target=msg3 | Messages 1-3 returned |

**`find_latest_message`:**
| Test | Setup | Expected |
|------|-------|----------|
| `returns_latest` | 3 messages | Returns frontier of msg3 |
| `excludes_deleted` | msg3 deleted | Returns frontier of msg2 |
| `returns_none_when_empty` | No messages | `Ok(None)` |

### 7.6 Integration tests — Handler (outbox handler path)

File: `src/infra/workers/thread_summary_worker.rs` (test module)

These tests use mock `LlmProvider` and real DB (SQLite or test Postgres):

| Test | Scenario | Expected |
|------|----------|----------|
| `handler_rejects_invalid_payload` | Garbage bytes in payload | `HandlerResult::Reject` |
| `handler_succeeds_first_summary` | No existing summary, 3 messages, LLM returns "summary" | `Success`, summary row created, messages compressed |
| `handler_succeeds_incremental_summary` | Existing summary, 2 new messages | `Success`, summary updated, only new messages compressed |
| `handler_cas_conflict_pre_check` | Frontier already advanced before LLM call | `Success` (skip), no LLM call |
| `handler_cas_conflict_on_commit` | Frontier advanced between LLM call and commit | `Success` (skip), no summary written |
| `handler_retries_on_llm_failure` | LLM returns error | `Retry`, previous summary unchanged |
| `handler_skips_empty_range` | All messages in range are deleted | `Success` |
| `handler_enqueues_system_usage_event` | Successful summary | Usage event in outbox with `requester_type=system` |

### 7.7 Integration tests — Trigger in finalization

File: `src/domain/service/finalization_service.rs` (test module)

| Test | Scenario | Expected |
|------|----------|----------|
| `completed_turn_above_threshold_enqueues_summary` | `reserve_tokens` above 80% budget | `ThreadSummaryTaskPayload` in recorded outbox |
| `completed_turn_below_threshold_no_enqueue` | `reserve_tokens` below threshold | No summary payload in outbox |
| `failed_turn_does_not_trigger` | `terminal_state=Failed` | No summary payload |
| `cancelled_turn_does_not_trigger` | `terminal_state=Cancelled` | No summary payload |
| `dedupe_no_enqueue_when_frontier_unchanged` | Frontier matches target | No summary payload |
| `summary_disabled_no_enqueue` | `summary_config.enabled=false` | No summary payload |

### 7.8 End-to-end tests — Full pipeline

File: `tests/thread_summary_e2e.rs` (new) or added to existing integration test file.

These tests exercise the complete trigger → outbox → handler → commit pipeline with
a real DB (SQLite in-memory with all migrations), mock `LlmProvider`, and the outbox
running in test/synchronous mode. They validate the cross-cutting invariants that
unit tests for individual phases cannot cover.

| Test | Setup | Asserts |
|------|-------|---------|
| `e2e_first_summary_generation` | Create chat with 10+ messages. Complete a turn where `reserve_tokens` exceeds 80% of budget. Let outbox deliver to handler. Mock LLM returns "summary text". | 1. `thread_summaries` row created with correct `summarized_up_to_*` frontier. 2. All messages up to frozen target have `is_compressed = true`. 3. Usage event in outbox with `requester_type=system`, `system_task_type="thread_summary_update"`. 4. Subsequent `get_latest()` returns the new summary. |
| `e2e_incremental_summary` | Create chat, generate first summary (frontier at msg 5). Add 5 more messages. Complete another turn above threshold. Let outbox deliver. Mock LLM returns updated summary. | 1. `thread_summaries` row updated (not duplicated — UNIQUE on `chat_id`). 2. Only messages 6-10 newly marked `is_compressed = true`. 3. Previously compressed messages (1-5) unchanged. 4. Frontier advanced to msg 10. |
| `e2e_below_threshold_no_summary` | Create chat with few short messages. Complete a turn where `reserve_tokens` is below 80%. | 1. No outbox message enqueued for `thread_summary` queue. 2. No `thread_summaries` row created. 3. All messages remain `is_compressed = false`. |
| `e2e_llm_failure_preserves_previous_summary` | Create chat, generate first summary. Add more messages, trigger fires. Mock LLM returns error. | 1. Previous summary text and frontier unchanged. 2. No messages newly marked as compressed. 3. Handler returns `Retry`. 4. `mini_chat_summary_fallback_total` incremented. |
| `e2e_concurrent_handlers_cas_safety` | Enqueue two thread-summary outbox messages for the same chat with the same `(base_frontier, frozen_target)`. Process both handlers concurrently (e.g., via `tokio::join!`). Both mock LLM calls succeed. | 1. Exactly one handler wins the CAS commit. 2. Exactly one `thread_summaries` row with correct frontier. 3. Messages compressed exactly once. 4. `mini_chat_thread_summary_cas_conflicts_total` incremented by 1 (the loser). 5. No duplicate usage events (losers don't emit usage). |
| `e2e_chat_deleted_during_summary` | Trigger fires, outbox message enqueued. Soft-delete chat before handler runs. Handler executes. | 1. Handler either returns `Success` (messages not found — empty range) or gracefully skips. 2. No `thread_summaries` row created (CASCADE may have removed it). 3. No crash or panic. |
| `e2e_summary_used_in_next_turn_context` | Generate first summary for a chat. On the next turn, verify context assembly. | 1. `gather_context()` returns `thread_summary = Some(...)` with the committed summary text. 2. `recent_after_boundary()` is called (not `recent_for_context`) — messages after frontier are loaded as recent. 3. Compressed messages are NOT loaded as recent context (they are represented by the summary). |
| `e2e_idempotent_redelivery` | Generate summary successfully (frontier advanced). Redeliver the same outbox message (simulating outbox replay). | 1. Pre-check detects frontier already advanced. 2. Handler returns `Success` without making an LLM call. 3. No duplicate summary writes. 4. `mini_chat_thread_summary_cas_conflicts_total` incremented. |
| `e2e_system_task_isolation` | Complete the full summary pipeline. Inspect all DB tables. | 1. No `chat_turns` row created for the summary task. 2. No `quota_usage` row debited for any user. 3. Usage event has `user_id = null` and `requester_type = "system"`. |

**Test infrastructure required:**

- **Mock `LlmProvider`**: configurable per-call responses (success with text/usage, or error).
  Can use `Arc<Mutex<VecDeque<Result<ResponseResult, LlmProviderError>>>>` for sequenced
  responses across multiple handler invocations.
- **Real DB**: SQLite in-memory with all migrations applied (same as test_helpers pattern).
- **Outbox test mode**: either use `toolkit_db::outbox` in synchronous/test delivery mode,
  or call `handler.handle()` directly with constructed `OutboxMessage` from the outbox table
  after the trigger transaction commits.
- **Deterministic timestamps**: use controlled `created_at` values for messages to ensure
  the strict `(created_at, id)` ordering is predictable and testable.
- **Assertion helpers**: `assert_messages_compressed(chat_id, expected_ids)` to verify
  `is_compressed` status across the messages table.

### 7.9 Unit tests — `SummaryFrontier` equality

File: `src/domain/repos/thread_summary_repo.rs` (test module)

| Test | Input | Expected |
|------|-------|----------|
| `same_frontier_eq` | Same `(created_at, message_id)` | `==` true |
| `different_message_id_ne` | Same `created_at`, different `message_id` | `!=` true |
| `different_created_at_ne` | Different `created_at`, same `message_id` | `!=` true |

## Acceptance Criteria

- [ ] All unit tests pass
- [ ] All integration tests pass on both SQLite and Postgres
- [ ] All e2e tests pass: full trigger → outbox → handler → commit pipeline
- [ ] No test uses `#[ignore]` without documented reason
- [ ] Test coverage: trigger logic, handler happy/error paths, CAS conflicts, repo operations
- [ ] E2e coverage: first summary, incremental, CAS concurrency, LLM failure, idempotent replay, context integration, system task isolation
- [ ] No flaky timing-dependent tests (use deterministic inputs and controlled timestamps)
- [ ] `e2e_concurrent_handlers_cas_safety` validates at-most-once summary commit
- [ ] `e2e_summary_used_in_next_turn_context` validates the summary is actually used downstream
