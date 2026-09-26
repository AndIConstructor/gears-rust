# Phase 4: Trigger — Evaluation in Finalization Path + Outbox Enqueue

## Goal

Evaluate the thread summary trigger during turn finalization and, if conditions are met,
enqueue a durable thread-summary outbox message atomically within the CAS finalization
transaction.

## Current State

- `FinalizationService::try_finalize()` at `src/domain/service/finalization_service.rs`
  runs steps 1-6 (CAS, billing, quota, message persist, usage event, audit event) inside
  a single `db.transaction()`.
- `FinalizationInput` carries `assembled_context_tokens: u64` (from context assembly —
  the real conversation size including system prompt, thread summary, history, and current message),
  `messages_truncated: bool` (true when context assembly dropped older messages), and
  `context_window: u32` / `max_output_tokens_applied: i32` for budget calculation.
- `OutboxEnqueuer` has `enqueue_thread_summary` (Phase 2).
- `ThreadSummaryRepository::get_latest` returns real data (Phase 3).
- `MessageRepository::find_latest_message` exists (Phase 3).

## Design Constraints

From DESIGN.md:
- Two-condition trigger (OR):
  1. **Proactive**: `assembled_context_tokens >= compression_threshold_pct% of effective_budget`
     AND no existing summary (`ThreadSummaryRepository::get_latest` returns `None`).
  2. **Urgent**: `messages_truncated == true` — context assembly dropped older messages,
     meaning the existing summary is stale and the conversation is losing context.
- When a summary already exists and context is not truncated, trigger MUST NOT fire.
- Default compression threshold: **60%** of effective input token budget.
- Durable scheduling MUST occur only in the same transaction that makes the causing turn durable.
- Thread summary generation MUST NOT block or modify the user-visible response path.
- The trigger SHOULD fire only for `Completed` turns.
- The request path SHOULD avoid enqueueing a duplicate when the frontier is unchanged.

## Tasks

### 4.1 Add compression threshold to config

File: `src/config/background.rs`

Add to `ThreadSummaryWorkerConfig`:

```rust
/// Compression threshold: summary triggered when assembled context tokens
/// reach this percentage of the effective input token budget. Default: 60.
pub compression_threshold_pct: u32,
```

Default: `60`. Range: `1-99`.

### 4.2 Carry context assembly data in `FinalizationInput`

File: `src/domain/model/finalization.rs`

Fields on `FinalizationInput` needed for trigger evaluation:

```rust
/// Context window size of the effective model (tokens).
pub context_window: u32,
/// Estimated input tokens from context assembly (all messages + system prompt).
pub assembled_context_tokens: u64,
/// True when context assembly dropped older messages due to budget.
pub messages_truncated: bool,
```

These flow from `AssembledContext` → `FinalizationCtx` → `FinalizationInput`.

### 4.3 Add trigger evaluation function

File: `src/domain/service/finalization_service.rs`

Pure function — no I/O:

```rust
/// Two conditions (OR):
/// 1. Context assembly truncated messages (urgent — conversation losing context).
/// 2. Context fills >= threshold% of budget AND no summary exists yet (proactive).
fn should_trigger_summary(
    assembled_context_tokens: u64,
    max_output_tokens_applied: i32,
    context_window: u32,
    compression_threshold_pct: u32,
    messages_truncated: bool,
    has_existing_summary: bool,
) -> bool {
    if messages_truncated {
        return true; // Urgent: messages already being dropped
    }
    if has_existing_summary {
        return false; // Existing summary still effective
    }
    // Proactive: context filling up, first summary
    let estimated_input = i64::try_from(assembled_context_tokens).unwrap_or(i64::MAX);
    let effective_budget = i64::from(context_window) - i64::from(max_output_tokens_applied);
    if effective_budget <= 0 || estimated_input <= 0 {
        return false;
    }
    let threshold = effective_budget * i64::from(compression_threshold_pct) / 100;
    estimated_input >= threshold
}
```

### 4.4 Wire trigger into finalization transaction

File: `src/domain/service/finalization_service.rs`

Inside `try_finalize()`, after step 6 (audit event), add step 7:

```rust
// 7. Evaluate thread summary trigger (completed turns only)
// Fetch existing summary first — needed for both trigger decision and frontier.
let current_summary = if input.terminal_state == TurnState::Completed
    && summary_config.enabled
{
    ThreadSummaryRepository::get_latest(&ts_repo, tx, &scope, input.chat_id).await?
} else {
    None
};

let summary_triggered = input.terminal_state == TurnState::Completed
    && summary_config.enabled
    && should_trigger_summary(
        input.assembled_context_tokens,
        input.max_output_tokens_applied,
        input.context_window,
        summary_config.compression_threshold_pct,
        input.messages_truncated,
        current_summary.is_some(),
    );

if summary_triggered {
    let base_frontier = current_summary.as_ref().map(|s| &s.frontier);
    let frozen_target = message_repo
        .find_latest_message(tx, &scope, input.chat_id)
        .await?;

    if let Some(target) = frozen_target {
        let should_enqueue = match base_frontier {
            Some(bf) => bf != &target,
            None => true,
        };
        if should_enqueue {
            let payload = ThreadSummaryTaskPayload { /* ... */ };
            outbox_enqueuer.enqueue_thread_summary(tx, payload).await?;
            metrics.record_thread_summary_trigger("scheduled");
        } else {
            metrics.record_thread_summary_trigger("not_needed");
        }
    }
}
```

### 4.5–4.6 Unchanged

Dependencies and wiring remain as originally planned.

## Acceptance Criteria

- [ ] `should_trigger_summary` returns `true` when `assembled_context_tokens >= 60% of budget` and no existing summary
- [ ] `should_trigger_summary` returns `false` when below threshold
- [ ] `should_trigger_summary` returns `false` when summary exists and context not truncated (proactive suppressed)
- [ ] `should_trigger_summary` returns `true` when `messages_truncated` even with existing summary (urgent)
- [ ] `should_trigger_summary` returns `false` when effective budget is non-positive
- [ ] Trigger only fires for `Completed` turns
- [ ] `OutboxEnqueuer::enqueue_thread_summary` called atomically within the finalization transaction
- [ ] `system_request_id` is `Uuid::new_v4()` generated once at enqueue
- [ ] Dedupe: no enqueue when base frontier equals target (frontier hasn't moved)
- [ ] Trigger does NOT block the user-visible response path (all within existing tx)
- [ ] Metrics: `record_thread_summary_trigger("scheduled"|"not_needed")` emitted
- [ ] Existing finalization tests pass with summary trigger disabled or below threshold
- [ ] New unit tests for `should_trigger_summary`: proactive, suppressed, urgent, non-positive budget, zero tokens
