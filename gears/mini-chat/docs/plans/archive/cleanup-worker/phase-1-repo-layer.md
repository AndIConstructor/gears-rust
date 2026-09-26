# Phase 1: Repository Layer for Cleanup

## Goal

Add query and update methods to `AttachmentRepository` and `VectorStoreRepository` that the cleanup handlers need. No handler logic yet — pure data access.

## Current State

- `AttachmentRepository` trait (`domain/repos/attachment_repo.rs`) has no cleanup-related methods.
- Entity (`infra/db/entity/attachment.rs`) already has columns: `cleanup_status`, `cleanup_attempts`, `last_cleanup_error`, `cleanup_updated_at`.
- Migration already has `idx_attachments_cleanup` partial index on `cleanup_status WHERE cleanup_status IS NOT NULL AND deleted_at IS NULL`.
- `VectorStoreRepository` has `find_by_chat()` and `delete()` — sufficient for Phase 4, but `delete()` uses AccessScope which is problematic for background workers (no user session).

## Tasks

### 1.1 Add `AttachmentRepository` trait methods

File: `src/domain/repos/attachment_repo.rs`

```rust
/// Load all attachments for a soft-deleted chat that still need provider cleanup.
/// `chat_id` is a globally unique UUID — no tenant scoping needed.
async fn find_pending_cleanup_by_chat<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
) -> Result<Vec<AttachmentModel>, DomainError>;

/// Mark a single attachment's cleanup as done.
/// Sets cleanup_status = 'done', cleanup_updated_at = now().
/// Returns rows affected (0 if already terminal — idempotent).
async fn mark_cleanup_done<C: DBRunner>(
    &self,
    runner: &C,
    attachment_id: Uuid,
) -> Result<u64, DomainError>;

/// Record a retryable cleanup failure.
/// Increments cleanup_attempts, sets last_cleanup_error, cleanup_updated_at = now().
/// If cleanup_attempts >= max_attempts, transitions to 'failed' instead.
/// Returns the new cleanup_status ('pending' or 'failed').
async fn record_cleanup_attempt<C: DBRunner>(
    &self,
    runner: &C,
    attachment_id: Uuid,
    error: &str,
    max_attempts: u32,
) -> Result<String, DomainError>;

/// Bulk-set cleanup_status = 'pending' for all non-deleted attachments of a chat
/// that don't already have a cleanup_status set.
/// Used in the chat-deletion transaction (Phase 2).
/// Returns count of rows updated.
async fn mark_attachments_pending_for_chat<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
) -> Result<u64, DomainError>;
```

Design notes:
- These methods do NOT take `AccessScope` — background workers have no user session. This is safe because the outbox message was originally enqueued within a scoped transaction.
- No `tenant_id` in signatures — `chat_id` is a globally unique UUID, sufficient for all queries. Workers operate without authorization context.
- `record_cleanup_attempt` encapsulates the state machine: if `attempts + 1 >= max_attempts`, it sets `cleanup_status = 'failed'`; otherwise keeps `'pending'`.
- `find_pending_cleanup_by_chat` filters: `chat_id = $1 AND cleanup_status = 'pending' AND deleted_at IS NOT NULL` (attachments must be soft-deleted to be cleanup-eligible).

### 1.2 Implement in infra layer

File: `src/infra/db/repo/attachment_repo.rs`

- `find_pending_cleanup_by_chat`: SeaORM `find()` with filter on `chat_id`, `cleanup_status = 'pending'`, `deleted_at IS NOT NULL`.
- `mark_cleanup_done`: `update_many()` with CAS guard `cleanup_status = 'pending'`, set `cleanup_status = 'done'`, `cleanup_updated_at = now()`.
- `record_cleanup_attempt`: Raw SQL or SeaORM expression update — `SET cleanup_attempts = cleanup_attempts + 1, last_cleanup_error = $error, cleanup_updated_at = now()` + conditional `cleanup_status = CASE WHEN cleanup_attempts + 1 >= $max THEN 'failed' ELSE 'pending' END` with CAS guard `cleanup_status = 'pending'`. Return the resulting status via `RETURNING cleanup_status` or a follow-up read.
- `mark_attachments_pending_for_chat`: `update_many()` filter `chat_id = $1 AND cleanup_status IS NULL AND deleted_at IS NULL`, set `cleanup_status = 'pending'`, `cleanup_updated_at = now()`. (Note: `deleted_at IS NULL` because at this point in the TX the chat is being soft-deleted but individual attachments haven't been soft-deleted yet — they get `cleanup_status = 'pending'` as the marker.)

### 1.3 Add `VectorStoreRepository` system-scoped methods

File: `src/domain/repos/vector_store_repo.rs`

The existing `find_by_chat()` and `delete()` require `AccessScope`. Background workers don't have one. Add unscoped variants:

```rust
/// Find vector store row for a chat (system context, no access scope).
/// `chat_id` is globally unique — no tenant scoping needed.
async fn find_by_chat_system<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
) -> Result<Option<VectorStoreModel>, DomainError>;

/// Hard-delete vector store row (system context, no access scope).
async fn delete_system<C: DBRunner>(
    &self,
    runner: &C,
    id: Uuid,
) -> Result<u64, DomainError>;
```

Implementation: Same as existing methods but without `.secure().scope_with(scope)` — filter by `chat_id` (or `id`) directly.

### 1.4 Test helper updates

File: `src/domain/service/test_helpers.rs`

Add mock implementations for the new trait methods in `MockAttachmentRepository` and `MockVectorStoreRepository` (or whichever mock pattern is used).

## Acceptance Criteria

- [ ] All new trait methods compile and have infra implementations
- [ ] Unit tests for each repo method (SQLite in-memory)
- [ ] CAS guards prevent double-transitions (e.g., marking `done` twice is idempotent, returns 0)
- [ ] `record_cleanup_attempt` correctly transitions to `failed` at threshold
