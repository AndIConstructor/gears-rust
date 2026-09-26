# Orphan Watchdog Implementation Plan

Implements `cpt-cf-mini-chat-component-orphan-watchdog` (P1) from DESIGN.md.

## Context

When a Mini-Chat pod crashes mid-stream, the turn row stays stuck in `running` state
indefinitely — the user is locked out, the quota reserve remains uncommitted, and no
billing event is emitted. The orphan watchdog is a leader-elected periodic job that
detects these stalled turns via a durable `last_progress_at` timestamp and finalizes
them as `failed/orphan_timeout` with `ABORTED` billing outcome.

The watchdog stub already exists (`src/infra/workers/orphan_watchdog.rs`) — the
leader election loop runs and logs ticks, but performs no scan or finalization. This
plan fills in the real logic.

Two key prerequisites do NOT exist yet:

1. **`last_progress_at` column** — not in `chat_turns` entity, migration, or any repo
   method. Must be added before any orphan detection logic.

2. **Orphan-specific CAS** — the existing `cas_update_state` checks only
   `WHERE state = 'running'`. The orphan CAS must additionally re-check
   `deleted_at IS NULL AND last_progress_at <= now() - :timeout` to prevent
   false orphan finalization after renewed progress (DESIGN.md P1 invariant).

## Architecture Decisions

- **Outbox-driven billing, leader-elected scan** — the watchdog scan itself is NOT
  outbox-driven (unlike the cleanup worker). It is a periodic poll under leader
  election. Billing events are enqueued into the outbox within the CAS transaction.

- **Single finalization path** — per DESIGN.md: "MUST NOT implement a second, divergent
  finalization path." `finalize_orphan_turn()` is added to `FinalizationService`,
  reusing `derive_billing_outcome`, `QuotaSettler::settle_in_tx`, and
  `OutboxEnqueuer::enqueue_usage_event`. Key differences from the normal path:
  no message persistence, no provider usage, always `Estimated` settlement.

- **Unscoped repo methods** — `find_orphan_candidates` and `cas_finalize_orphan` omit
  `AccessScope`. Safe because: leader election guarantees single-instance execution,
  and the CAS guard ensures at-most-once finalization.

- **`AccessScope` from turn row** — constructed from the turn's `tenant_id` for quota
  settlement (same pattern as test helpers and cleanup worker).

- **Progress update throttling** — `update_progress_at` called only on `ClientSseEvent::Delta`
  (content deltas), throttled to at most once per ~30s via local `Instant`. Non-text events
  (tool status, lifecycle) do not advance liveness — delta-only heartbeat.

## Rust Guidelines Applied (M-* from Microsoft Pragmatic Rust Guidelines)

- **M-ERRORS-CANONICAL-STRUCTS**: orphan errors use existing `DomainError` variants.
- **M-LOG-STRUCTURED**: structured tracing with named fields (`turn_id`, `tenant_id`,
  `chat_id`, `timeout_secs`, `last_progress_at`).
- **M-PANIC-ON-BUG**: unreachable states panic; expected race conditions (CAS loser) log
  and skip.
- **M-CONCISE-NAMES**: `OrphanWatchdogDeps`, not `OrphanDetectionAndFinalizationManager`.
- **M-SERVICES-CLONE**: watchdog holds `Arc<T>` deps, is `Send + Sync`.
- **M-STATIC-VERIFICATION**: clippy clean, no `#[allow]` without `reason`.

## Phases

| Phase | File | Summary |
|-------|------|---------|
| 1 | [phase-1-migration-entity.md](phase-1-migration-entity.md) | Add `last_progress_at` column: migration, entity, `create_turn` init |
| 2 | [phase-2-progress-updates.md](phase-2-progress-updates.md) | Wire `last_progress_at` updates into the streaming path |
| 3 | [phase-3-repo-layer.md](phase-3-repo-layer.md) | `find_orphan_candidates` + `cas_finalize_orphan` on TurnRepository |
| 4 | [phase-4-orphan-finalization.md](phase-4-orphan-finalization.md) | `finalize_orphan_turn` on FinalizationService (CAS + quota + outbox) |
| 5 | [phase-5-watchdog-wiring.md](phase-5-watchdog-wiring.md) | Replace stub with real scan-finalize loop, inject deps |
| 6 | [phase-6-observability.md](phase-6-observability.md) | Metrics: detected, finalized, scan_duration |
| 7 | [phase-7-tests.md](phase-7-tests.md) | Unit + integration tests |

## Dependency Graph

```
Phase 1 ──┬──► Phase 2 (progress updates need the column)
           │
           └──► Phase 3 (orphan query uses last_progress_at)
                   │
                   └──► Phase 4 (finalization uses repo methods)
                           │
                           └──► Phase 5 (watchdog calls finalization service)
                                   │
Phase 6 ◄─────────────────────────┘ (metrics wired into watchdog + finalization)
Phase 7 depends on all above
```

Phases 2 and 3 can be developed in parallel after Phase 1.
