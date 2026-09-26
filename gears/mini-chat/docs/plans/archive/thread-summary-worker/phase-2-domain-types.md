# Phase 2: Domain Types — Payload, Frontier, Enqueuer Trait Extension

## Goal

Define the domain-level types for thread summary orchestration: the durable outbox payload,
the summary frontier value object, and extend the `OutboxEnqueuer` trait to include
thread summary enqueue.

## Current State

- `ThreadSummaryModel` at `src/domain/repos/thread_summary_repo.rs:13-17` has
  `content`, `boundary_message_id`, `boundary_created_at`. Used for context assembly reads.
- `InfraOutboxEnqueuer::enqueue_thread_summary_task()` at `src/infra/outbox.rs:88-120`
  accepts raw `Vec<u8>` payload, partitions by `chat_id`. Marked `#[allow(dead_code)]`.
  **Not** on the `OutboxEnqueuer` trait — only on the concrete struct.
- `OutboxEnqueuer` trait at `src/domain/repos/outbox_enqueuer.rs:90-154` has methods for
  usage, attachment cleanup, chat cleanup, and audit — but NOT thread summary.
- No `ThreadSummaryTaskPayload` type exists.

## Tasks

### 2.1 Define `SummaryFrontier` value object

File: `src/domain/repos/thread_summary_repo.rs`

A strongly-typed composite frontier to avoid loose `(OffsetDateTime, Uuid)` tuples
throughout the codebase (M-STRONG-TYPES):

```rust
/// Inclusive summary frontier in the per-chat message order `(created_at ASC, id ASC)`.
///
/// Identifies the last message represented in the summary text.
/// Per DESIGN.md: "created_at alone is insufficient because multiple messages may share
/// the same timestamp. id is used only as a deterministic UUID tie-breaker."
#[domain_model]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SummaryFrontier {
    pub created_at: OffsetDateTime,
    pub message_id: Uuid,
}
```

Update `ThreadSummaryModel` to use `SummaryFrontier`:

```rust
#[domain_model]
#[derive(Debug, Clone)]
pub struct ThreadSummaryModel {
    pub content: String,
    pub frontier: SummaryFrontier,
    pub token_estimate: i32,
}
```

Update all call sites that use `boundary_message_id` / `boundary_created_at` to use
`model.frontier.message_id` / `model.frontier.created_at` instead:
- `stream_service/mod.rs` `gather_context()` (line ~802-809)
- Context assembly `thread_summary` parameter (unchanged — still passes `ts.content`)
- `recent_after_boundary()` call uses `ts.frontier.created_at`, `ts.frontier.message_id`

### 2.2 Define `ThreadSummaryTaskPayload`

File: `src/domain/repos/outbox_enqueuer.rs`

Per DESIGN.md (line 1574), the serialized outbox message MUST contain at minimum:

```rust
/// Durable outbox payload for thread summary generation.
///
/// Persisted at enqueue time in the finalization transaction. The handler
/// reads this payload to know exactly which message range to summarize.
/// `system_request_id` is generated once and reused across retries.
#[domain_model]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ThreadSummaryTaskPayload {
    pub tenant_id: Uuid,
    pub chat_id: Uuid,
    /// Stable system-task identity — generated at enqueue, reused across retries.
    pub system_request_id: Uuid,
    pub system_task_type: String,  // always "thread_summary_update"

    // ── Base frontier (current summary boundary at enqueue time) ──
    // None if no summary exists yet (first summary for this chat).
    #[serde(with = "time::serde::rfc3339::option")]
    pub base_frontier_created_at: Option<OffsetDateTime>,
    pub base_frontier_message_id: Option<Uuid>,

    // ── Frozen target frontier (last message to include in this summary) ──
    #[serde(with = "time::serde::rfc3339")]
    pub frozen_target_created_at: OffsetDateTime,
    pub frozen_target_message_id: Uuid,
}
```

### 2.3 Add `enqueue_thread_summary` to `OutboxEnqueuer` trait

File: `src/domain/repos/outbox_enqueuer.rs`

```rust
/// Enqueue a thread summary task within the caller's transaction.
///
/// Called in the finalization transaction when the trigger fires.
/// Partitioned by `chat_id` so all summary events for one chat are
/// processed sequentially within the same partition.
async fn enqueue_thread_summary(
    &self,
    runner: &(dyn DBRunner + Sync),
    payload: ThreadSummaryTaskPayload,
) -> Result<(), DomainError>;
```

### 2.4 Implement `enqueue_thread_summary` in infra

File: `src/infra/outbox.rs`

Update the `OutboxEnqueuer for InfraOutboxEnqueuer` impl block. The existing
`enqueue_thread_summary_task` method (lines 88-120) already has the correct partition
and queue logic. Refactor it:

```rust
async fn enqueue_thread_summary(
    &self,
    runner: &(dyn DBRunner + Sync),
    payload: ThreadSummaryTaskPayload,
) -> Result<(), DomainError> {
    let partition = Self::compute_partition(payload.chat_id, self.num_partitions);
    let serialized = serde_json::to_vec(&payload)
        .map_err(|e| DomainError::internal(format!("thread summary serialize: {e}")))?;

    self.outbox()
        .enqueue(
            runner,
            &self.thread_summary_queue_name,
            partition,
            serialized,
            "application/json",
        )
        .await
        .map_err(|e| DomainError::internal(format!("outbox enqueue: {e}")))?;

    info!(
        queue = %self.thread_summary_queue_name,
        partition,
        chat_id = %payload.chat_id,
        system_request_id = %payload.system_request_id,
        "thread summary task enqueued"
    );

    Ok(())
}
```

Remove the old `enqueue_thread_summary_task` method and its `#[allow(dead_code)]`.

### 2.5 Update `RecordingOutboxEnqueuer` in test helpers

File: `src/domain/service/test_helpers.rs`

Add `thread_summary_payloads: Mutex<Vec<ThreadSummaryTaskPayload>>` field and implement
the new trait method to record payloads for test assertions.

## Acceptance Criteria

- [ ] `SummaryFrontier` value object defined and used in `ThreadSummaryModel`
- [ ] `ThreadSummaryTaskPayload` serializes/deserializes correctly (round-trip test)
- [ ] `OutboxEnqueuer` trait extended with `enqueue_thread_summary`
- [ ] Infra implementation delegates to `outbox.enqueue()` with `chat_id`-based partition
- [ ] Old `enqueue_thread_summary_task` removed, `#[allow(dead_code)]` cleaned up
- [ ] `RecordingOutboxEnqueuer` updated in test helpers
- [ ] All callers of `ThreadSummaryModel.boundary_*` migrated to `frontier.*`
- [ ] Existing tests compile and pass
