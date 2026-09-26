# Phase 3: Repository Layer — Orphan Query + CAS

## Goal

Add query and CAS methods to `TurnRepository` for orphan detection and finalization. Pure data access, no domain logic.

## Current State

- `TurnRepository` trait (`src/domain/repos/turn_repo.rs`) has `cas_update_state(CasTerminalParams)` which checks only `WHERE id = :id AND state = 'running'` — no `last_progress_at` or `deleted_at` guard.
- No method to discover orphan candidates.
- All existing query/mutation methods require `AccessScope`.

## Design Constraints

From DESIGN.md (mandatory P1 invariants):
- Orphan detection MUST use `last_progress_at` (stale-progress), NOT raw age from `started_at`.
- The terminal UPDATE itself MUST re-check ALL orphan predicates — not just `state = 'running'`.
- If `rows_affected = 0`, the turn was already finalized, soft-deleted, or made progress → MUST skip.
- Stale-progress predicate MUST participate in both candidate discovery AND final conditional update.

## Tasks

### 3.1 Add `find_orphan_candidates` to TurnRepository trait

File: `src/domain/repos/turn_repo.rs`

```rust
/// Find running turns with stale progress (orphan candidates).
///
/// No `AccessScope` — system-level background worker query under leader election.
/// Returns at most `limit` rows ordered by oldest progress first.
async fn find_orphan_candidates<C: DBRunner>(
    &self,
    runner: &C,
    timeout_secs: u64,
    limit: u32,
) -> Result<Vec<TurnModel>, DomainError>;
```

Design notes:
- No `AccessScope` — the orphan watchdog runs under leader election, is single-instance, and queries all tenants. This follows the same pattern as the cleanup worker's unscoped repo methods (Phase 1 of cleanup-worker plan).
- `limit` bounds the batch size to prevent unbounded result sets. Remaining orphans are picked up on the next tick.
- Ordered by `last_progress_at ASC` so the oldest orphans are processed first.

### 3.2 Add `cas_finalize_orphan` to TurnRepository trait

File: `src/domain/repos/turn_repo.rs`

```rust
/// CAS update for orphan finalization with full predicate re-check.
///
/// The terminal UPDATE re-checks ALL orphan predicates:
/// `state = 'running' AND deleted_at IS NULL AND last_progress_at <= now() - timeout`.
/// This prevents "false orphan finalization after renewed progress" (DESIGN.md P1 invariant).
///
/// Returns `rows_affected`:
/// - `0`: turn is no longer orphan-eligible (already finalized, soft-deleted, or progress renewed)
/// - `1`: turn transitioned to `Failed` with `error_code = 'orphan_timeout'`
async fn cas_finalize_orphan<C: DBRunner>(
    &self,
    runner: &C,
    turn_id: Uuid,
    timeout_secs: u64,
) -> Result<u64, DomainError>;
```

### 3.3 Implement `find_orphan_candidates` in infra

File: `src/infra/db/repo/turn_repo.rs`

The query:
```sql
SELECT * FROM chat_turns
WHERE state = 'running'
  AND deleted_at IS NULL
  AND last_progress_at <= now() - interval '1 second' * :timeout_secs
ORDER BY last_progress_at ASC
LIMIT :limit
```

Implementation approach:
- Use SeaORM `find()` with `Condition::all()` for `state = Running`, `deleted_at IS NULL`.
- For the `last_progress_at` interval arithmetic, use `Expr::cust_with_values` or raw SQL expression, since SeaORM doesn't have native interval subtraction.
- **Backend-specific SQL**: Postgres uses `now() - interval '1 second' * $1`. SQLite uses `datetime('now', '-' || $1 || ' seconds')`. Use a conditional expression or raw query with backend detection.
- No `.secure().scope_with()` — unscoped per AD-3.

**Postgres expression:**
```rust
Column::LastProgressAt.lte(
    Expr::cust_with_values("now() - interval '1 second' * $1", [timeout_secs as i64])
)
```

**SQLite expression:**
```rust
Column::LastProgressAt.lte(
    Expr::cust_with_values("datetime('now', '-' || $1 || ' seconds')", [timeout_secs as i64])
)
```

### 3.4 Implement `cas_finalize_orphan` in infra

File: `src/infra/db/repo/turn_repo.rs`

The CAS update:
```sql
UPDATE chat_turns
   SET state = 'failed',
       error_code = 'orphan_timeout',
       completed_at = now(),
       updated_at = now()
 WHERE id = :turn_id
   AND state = 'running'
   AND deleted_at IS NULL
   AND last_progress_at <= now() - interval '1 second' * :timeout_secs
```

Implementation:
```rust
async fn cas_finalize_orphan<C: DBRunner>(
    &self,
    runner: &C,
    turn_id: Uuid,
    timeout_secs: u64,
) -> Result<u64, DomainError> {
    let now = OffsetDateTime::now_utc();
    let result = TurnEntity::update_many()
        .col_expr(Column::State, Expr::value(TurnState::Failed.into_value()))
        .col_expr(Column::ErrorCode, Expr::value(Some("orphan_timeout".to_owned())))
        .col_expr(Column::CompletedAt, Expr::value(now))
        .col_expr(Column::UpdatedAt, Expr::value(now))
        .filter(
            Condition::all()
                .add(Column::Id.eq(turn_id))
                .add(Column::State.eq(TurnState::Running))
                .add(Column::DeletedAt.is_null())
                .add(/* last_progress_at <= now() - timeout — backend-specific expr */),
        )
        .exec(runner)
        .await?;
    Ok(result.rows_affected)
}
```

**Critical**: The `last_progress_at` predicate is re-checked in the CAS. If a streaming task refreshed progress between `find_orphan_candidates` and this CAS, `rows_affected = 0` and the turn is safely skipped.

No `.secure().scope_with()` — unscoped per AD-3.

## Open Questions

- **Backend detection pattern**: How does the existing codebase handle backend-specific SQL (Postgres vs SQLite)? Check if there's an existing utility or pattern for conditional expressions. The migration already handles this via separate up/down blocks.

## Acceptance Criteria

- [ ] `find_orphan_candidates` returns only running, non-deleted, stale turns
- [ ] `find_orphan_candidates` excludes turns with recent progress
- [ ] `find_orphan_candidates` excludes soft-deleted turns
- [ ] `find_orphan_candidates` excludes terminal turns (completed/failed/cancelled)
- [ ] `find_orphan_candidates` respects `limit`
- [ ] `cas_finalize_orphan` returns 1 and transitions to `Failed/orphan_timeout`
- [ ] `cas_finalize_orphan` returns 0 on already-terminal turn
- [ ] `cas_finalize_orphan` returns 0 if progress was renewed (critical safety invariant)
- [ ] `cas_finalize_orphan` returns 0 on soft-deleted turn
- [ ] Works with both Postgres and SQLite backends
