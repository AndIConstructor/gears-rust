# Phase 7: Tests

## Goal

Comprehensive test coverage for the orphan watchdog across unit and integration layers.

## Test Infrastructure

- **SQLite in-memory DB** with all migrations applied (same as existing test helpers in `src/domain/service/test_helpers.rs`).
- **Mock `QuotaSettler`** and **`RecordingOutboxEnqueuer`** — follow existing patterns from `finalization_service.rs` tests.
- **Mock turn repo** for watchdog loop unit tests (isolate loop logic from real DB).
- **Noop metrics** for unit tests; real metrics for integration tests if needed.

## Unit Tests — Migration + Entity (Phase 1)

| Test | File | Asserts |
|------|------|---------|
| `create_turn_sets_last_progress_at` | `infra/db/repo/turn_repo.rs` | New running turn has `last_progress_at` approximately equal to `now()` |
| `terminal_turn_allows_null_last_progress_at` | `infra/db/repo/turn_repo.rs` | Completed/failed/cancelled turns can have `last_progress_at = NULL` |

## Unit Tests — Progress Updates (Phase 2)

| Test | File | Asserts |
|------|------|---------|
| `update_progress_at_updates_running_turn` | `infra/db/repo/turn_repo.rs` | `rows_affected = 1`, `last_progress_at` is updated to a more recent value |
| `update_progress_at_noop_on_terminal` | `infra/db/repo/turn_repo.rs` | `rows_affected = 0` for a completed turn |

## Unit Tests — Repo Methods (Phase 3)

| Test | File | Asserts |
|------|------|---------|
| `find_orphan_candidates_returns_stale_running` | `infra/db/repo/turn_repo.rs` | Turn with `last_progress_at` older than timeout is found |
| `find_orphan_candidates_excludes_recent_progress` | `infra/db/repo/turn_repo.rs` | Turn with recent `last_progress_at` not returned |
| `find_orphan_candidates_excludes_deleted` | `infra/db/repo/turn_repo.rs` | Soft-deleted running turn not returned |
| `find_orphan_candidates_excludes_terminal` | `infra/db/repo/turn_repo.rs` | Completed/failed/cancelled turns not returned |
| `find_orphan_candidates_respects_limit` | `infra/db/repo/turn_repo.rs` | Returns at most `limit` rows |
| `find_orphan_candidates_orders_by_oldest_first` | `infra/db/repo/turn_repo.rs` | Oldest `last_progress_at` comes first |
| `cas_finalize_orphan_transitions_to_failed` | `infra/db/repo/turn_repo.rs` | `rows_affected = 1`, state = `Failed`, `error_code = "orphan_timeout"`, `completed_at` set |
| `cas_finalize_orphan_noop_if_already_terminal` | `infra/db/repo/turn_repo.rs` | `rows_affected = 0` on completed turn |
| `cas_finalize_orphan_noop_if_progress_renewed` | `infra/db/repo/turn_repo.rs` | `rows_affected = 0` when `last_progress_at` was refreshed after candidate discovery — **critical safety test** |
| `cas_finalize_orphan_noop_if_deleted` | `infra/db/repo/turn_repo.rs` | `rows_affected = 0` on soft-deleted running turn |

## Unit Tests — Orphan Finalization (Phase 4)

| Test | File | Asserts |
|------|------|---------|
| `finalize_orphan_cas_winner` | `domain/service/finalization_service.rs` | Returns `true`, quota debited (estimated), usage event enqueued in `RecordingOutboxEnqueuer` |
| `finalize_orphan_cas_loser` | `domain/service/finalization_service.rs` | Returns `false`, `RecordingOutboxEnqueuer` has no new events, `MockQuotaSettler` not called |
| `finalize_orphan_billing_is_aborted_estimated` | `domain/model/billing_outcome.rs` | Already exists: `orphan_timeout_derives_aborted_estimated` at line 183-188. Verify it still passes. |
| `finalize_orphan_no_message_persisted` | `domain/service/finalization_service.rs` | No `insert_assistant_message` call on mock message repo |
| `finalize_orphan_missing_quota_fields_skips_settlement` | `domain/service/finalization_service.rs` | Turn with `effective_model = NULL` still transitions to Failed, `MockQuotaSettler` not called, warning logged |

## Unit Tests — Watchdog Loop (Phase 5)

| Test | File | Asserts |
|------|------|---------|
| `disabled_returns_immediately` | `infra/workers/orphan_watchdog.rs` | Already exists — update to pass new deps |
| `shutdown_on_cancel` | `infra/workers/orphan_watchdog.rs` | Already exists — update to pass new deps |
| `scan_finds_and_finalizes_orphan` | `infra/workers/orphan_watchdog.rs` | Mock turn_repo returns 1 candidate, finalization_svc called once, finalized metric recorded |
| `scan_empty_is_noop` | `infra/workers/orphan_watchdog.rs` | Mock turn_repo returns 0 candidates, no finalization calls |
| `scan_continues_after_individual_error` | `infra/workers/orphan_watchdog.rs` | Finalization errors on turn 1 don't prevent processing turn 2 |
| `shutdown_between_candidates` | `infra/workers/orphan_watchdog.rs` | Cancel fired mid-batch, exits cleanly after current candidate |

## Integration Tests

File: `tests/orphan_integration.rs` (new) or added to existing integration test file.

| Test | Setup | Asserts |
|------|-------|---------|
| `e2e_orphan_detection_and_finalization` | Create running turn with `last_progress_at` = 10 minutes ago. Run one watchdog tick. | Turn state = `Failed`, `error_code = "orphan_timeout"`. Quota debited (estimated). Usage event in outbox with `outcome = "aborted"`. |
| `no_false_orphan_after_renewed_progress` | Create running turn with recent `last_progress_at` (within timeout). Run watchdog tick. | Turn still `Running`. No outbox events. No metrics. |
| `idempotent_double_scan` | Create orphan turn. Run watchdog tick twice. | First tick: finalized. Second tick: candidate not found (already terminal) or CAS returns 0. No duplicate outbox events. |
| `concurrent_finalization_cas_safety` | Create orphan turn. Run orphan CAS and normal `cas_update_state` concurrently. | Exactly one wins. No duplicate quota settlement. No duplicate outbox events. |
| `deleted_turn_not_finalized` | Create running turn with stale progress, then soft-delete it. Run watchdog tick. | Turn not finalized by watchdog (CAS fails on `deleted_at IS NULL`). |

## Existing Tests to Verify

These tests should continue passing without modification:

- `orphan_timeout_derives_aborted_estimated` — billing_outcome.rs:183-188
- `disabled_returns_immediately` — orphan_watchdog.rs:71 (updated for new deps)
- `shutdown_on_cancel` — orphan_watchdog.rs:83 (updated for new deps)
- All existing `finalize_turn_cas` tests in `finalization_service.rs`

## Acceptance Criteria

- [ ] All unit tests pass: `cargo test -p mini-chat`
- [ ] Integration tests cover: happy path, CAS safety, renewed-progress safety, idempotency
- [ ] No `#[ignore]` tests without documented reason
- [ ] `cas_finalize_orphan_noop_if_progress_renewed` specifically validates the P1 invariant
- [ ] `concurrent_finalization_cas_safety` validates at-most-once finalization
- [ ] Existing billing derivation test for `orphan_timeout` still passes
