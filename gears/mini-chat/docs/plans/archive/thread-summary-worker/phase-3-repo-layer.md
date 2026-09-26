# Phase 3: Repository Layer — Thread Summary Persistence

## Goal

Implement real `ThreadSummaryRepository` methods: `get_latest` (replace stub), `upsert_with_cas`
(CAS-protected frontier advance), and `mark_messages_compressed` (set `is_compressed = true`
on the summarized range).

## Current State

- `ThreadSummaryRepository` trait at `src/domain/repos/thread_summary_repo.rs:20-31` has
  only `get_latest()` returning `Option<ThreadSummaryModel>`.
- Infra impl at `src/infra/db/repo/thread_summary_repo.rs` always returns `Ok(None)`.
- `messages` entity has `is_compressed: bool` (always `false`, line 33).
- SeaORM entity for `thread_summaries` created in Phase 1.
- Message index: `(chat_id, created_at, id) WHERE deleted_at IS NULL` — supports the
  range query needed by the handler.

## Tasks

### 3.1 Implement real `get_latest`

File: `src/infra/db/repo/thread_summary_repo.rs`

Replace the stub with a real DB query:

```rust
async fn get_latest<C: DBRunner>(
    &self,
    runner: &C,
    _scope: &AccessScope,
    chat_id: Uuid,
) -> Result<Option<ThreadSummaryModel>, DomainError> {
    let row = thread_summary::Entity::find()
        .filter(thread_summary::Column::ChatId.eq(chat_id))
        .one(runner.as_ref())
        .await
        .map_err(|e| DomainError::internal(format!("thread_summary query: {e}")))?;

    Ok(row.map(|r| ThreadSummaryModel {
        content: r.summary_text,
        frontier: SummaryFrontier {
            created_at: r.summarized_up_to_created_at,
            message_id: r.summarized_up_to_message_id,
        },
        token_estimate: r.token_estimate,
    }))
}
```

Note: `_scope` is unused because `thread_summaries` has no independent authorization
(accessed through parent chat, per DESIGN.md). The scope parameter is kept for API
consistency with other repository traits.

### 3.2 Add `upsert_with_cas` to trait and impl

File: `src/domain/repos/thread_summary_repo.rs`

```rust
/// CAS-protected upsert: insert or update the summary only if the stored frontier
/// matches `expected_base_frontier`.
///
/// Returns the number of rows affected:
/// - 1 = success (frontier advanced)
/// - 0 = CAS conflict (another handler already advanced the frontier)
///
/// If no `thread_summaries` row exists and `expected_base_frontier` is `None`,
/// inserts a new row (first summary for this chat).
async fn upsert_with_cas<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
    tenant_id: Uuid,
    expected_base_frontier: Option<&SummaryFrontier>,
    new_frontier: &SummaryFrontier,
    summary_text: &str,
    token_estimate: i32,
) -> Result<u64, DomainError>;
```

File: `src/infra/db/repo/thread_summary_repo.rs`

Implementation uses raw SQL for the atomic CAS because SeaORM's update API does not
natively support conditional UPDATE with composite WHERE on nullable pairs:

**Case 1: First summary (`expected_base_frontier` is `None`)**

```sql
INSERT INTO thread_summaries (
    id, tenant_id, chat_id, summary_text,
    summarized_up_to_created_at, summarized_up_to_message_id,
    token_estimate, created_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, now(), now())
ON CONFLICT (chat_id) DO NOTHING
```

`DO NOTHING` ensures that if another handler already inserted a row, we get 0 rows
affected (CAS semantics). The first INSERT wins.

**Case 2: Subsequent summary (`expected_base_frontier` is `Some`)**

```sql
UPDATE thread_summaries
SET summary_text = $1,
    summarized_up_to_created_at = $2,
    summarized_up_to_message_id = $3,
    token_estimate = $4,
    updated_at = now()
WHERE chat_id = $5
  AND summarized_up_to_created_at = $6
  AND summarized_up_to_message_id = $7
```

The WHERE clause on the composite frontier is the CAS guard. Returns 0 if the frontier
was already advanced by another handler.

### 3.3 Add `mark_messages_compressed` to `MessageRepository`

File: `src/domain/repos/message_repo.rs` (trait extension)

```rust
/// Mark messages in the given range as compressed (included in a thread summary).
///
/// Sets `is_compressed = true` for all non-deleted messages in
/// `(base_frontier, target_frontier]` ordered by `(created_at ASC, id ASC)`.
/// Returns the number of rows updated.
async fn mark_messages_compressed<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
    base_frontier: Option<&SummaryFrontier>,
    target_frontier: &SummaryFrontier,
) -> Result<u64, DomainError>;
```

File: `src/infra/db/repo/message_repo.rs` (implementation)

Uses raw SQL for the composite range predicate:

**When `base_frontier` is `None` (first summary):**
```sql
UPDATE messages
SET is_compressed = true
WHERE chat_id = $1
  AND deleted_at IS NULL
  AND is_compressed = false
  AND (created_at, id) <= ($2, $3)
```

**When `base_frontier` is `Some`:**
```sql
UPDATE messages
SET is_compressed = true
WHERE chat_id = $1
  AND deleted_at IS NULL
  AND is_compressed = false
  AND (created_at, id) > ($2, $3)
  AND (created_at, id) <= ($4, $5)
```

The row-value comparison `(created_at, id)` leverages Postgres tuple comparison for
the strict total order.

### 3.4 Add `fetch_messages_in_range` to `MessageRepository`

File: `src/domain/repos/message_repo.rs` (trait extension)

The handler needs to load the actual message content for the LLM summarization prompt:

```rust
/// Load non-deleted, non-compressed messages in the range (base_frontier, target_frontier].
///
/// Returns messages ordered by `(created_at ASC, id ASC)`.
/// Per DESIGN.md: messages appended after `frozen_target_frontier` are excluded.
async fn fetch_messages_in_range<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
    base_frontier: Option<&SummaryFrontier>,
    target_frontier: &SummaryFrontier,
) -> Result<Vec<MessageModel>, DomainError>;
```

File: `src/infra/db/repo/message_repo.rs`

Similar SQL to `mark_messages_compressed` but SELECT instead of UPDATE, with
`ORDER BY created_at ASC, id ASC`.

The `WHERE` clauses match the DESIGN.md range selection rules (section "Thread Summary -
Stable Range and Commit Invariant"):
1. `deleted_at IS NULL`
2. `is_compressed = false`
3. `(created_at, id) > base_frontier` (omitted if first summary)
4. `(created_at, id) <= frozen_target_frontier`

### 3.5 Add `find_latest_message_before` to `MessageRepository`

The trigger needs to determine the `frozen_target_frontier` — the last message that should
be included in the summary. This is the latest non-deleted message at the time the trigger
fires (which is the just-persisted assistant message from the current turn, or the user
message if no assistant message was persisted):

```rust
/// Find the most recent non-deleted message for a chat.
///
/// Returns `(created_at, message_id)` for use as `frozen_target_frontier`.
/// Returns `None` if no messages exist.
async fn find_latest_message<C: DBRunner>(
    &self,
    runner: &C,
    chat_id: Uuid,
) -> Result<Option<SummaryFrontier>, DomainError>;
```

```sql
SELECT created_at, id
FROM messages
WHERE chat_id = $1 AND deleted_at IS NULL
ORDER BY created_at DESC, id DESC
LIMIT 1
```

## Acceptance Criteria

- [ ] `get_latest` returns real data from `thread_summaries` table (not `Ok(None)` stub)
- [ ] `upsert_with_cas` returns 1 on success, 0 on CAS conflict
- [ ] `upsert_with_cas` handles first-summary INSERT with `ON CONFLICT DO NOTHING`
- [ ] `mark_messages_compressed` updates exactly the messages in the specified range
- [ ] `fetch_messages_in_range` returns correct messages in `(created_at ASC, id ASC)` order
- [ ] `find_latest_message` returns the most recent non-deleted message
- [ ] All range queries use the composite `(created_at, id)` tuple comparison
- [ ] Existing tests pass (stub removal is backward-compatible since no data existed)
