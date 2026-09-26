# Phase 5: Observability

## Goal

Add Prometheus metrics specified in DESIGN.md for cleanup operations.

## Required Metrics (from DESIGN.md line 1800-1806)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `mini_chat_cleanup_completed_total` | counter | `resource_type=file\|vector_store` | Successful cleanup operations |
| `mini_chat_cleanup_failed_total` | counter | `resource_type=file` | Attachments reaching terminal `failed` |
| `mini_chat_cleanup_retry_total` | counter | `resource_type=file\|vector_store`, `reason` | Retries delegated to outbox |
| `mini_chat_cleanup_backlog` | gauge | `state=pending\|failed`, `resource_type=file` | Current cleanup backlog from attachment row states |
| `mini_chat_cleanup_vector_store_with_failed_attachments_total` | counter | — | VS deleted with at least one `failed` attachment |

## Tasks

### 5.1 Define metric constants

File: `src/infra/workers/cleanup_metrics.rs` (new file)

Use `prometheus` or whatever metrics crate the project uses. Check existing metrics patterns in the codebase.

```rust
use once_cell::sync::Lazy;
use prometheus::{IntCounterVec, IntGaugeVec, register_int_counter_vec, register_int_gauge_vec};

pub static CLEANUP_COMPLETED: Lazy<IntCounterVec> = Lazy::new(|| {
    register_int_counter_vec!(
        "mini_chat_cleanup_completed_total",
        "Successful provider cleanup operations",
        &["resource_type"]
    ).unwrap()
});

pub static CLEANUP_FAILED: Lazy<IntCounterVec> = Lazy::new(|| {
    register_int_counter_vec!(
        "mini_chat_cleanup_failed_total",
        "Attachments reaching terminal failed state",
        &["resource_type"]
    ).unwrap()
});

pub static CLEANUP_RETRY: Lazy<IntCounterVec> = Lazy::new(|| {
    register_int_counter_vec!(
        "mini_chat_cleanup_retry_total",
        "Cleanup retries delegated to shared outbox",
        &["resource_type", "reason"]
    ).unwrap()
});

pub static CLEANUP_BACKLOG: Lazy<IntGaugeVec> = Lazy::new(|| {
    register_int_gauge_vec!(
        "mini_chat_cleanup_backlog",
        "Current cleanup backlog by state",
        &["state", "resource_type"]
    ).unwrap()
});

pub static CLEANUP_VS_WITH_FAILED: Lazy<IntCounter> = Lazy::new(|| {
    register_int_counter!(
        "mini_chat_cleanup_vector_store_with_failed_attachments_total",
        "Vector store deletions with at least one failed attachment"
    ).unwrap()
});
```

### 5.2 Instrument handlers

Wire metric increments into `AttachmentCleanupHandler` and `ChatCleanupHandler`:

- On `mark_cleanup_done` → `CLEANUP_COMPLETED.with_label_values(&["file"]).inc()`
- On `record_cleanup_attempt` returning `"failed"` → `CLEANUP_FAILED.with_label_values(&["file"]).inc()`
- On `record_cleanup_attempt` returning `"pending"` → `CLEANUP_RETRY.with_label_values(&["file", &reason]).inc()`
- On VS delete success → `CLEANUP_COMPLETED.with_label_values(&["vector_store"]).inc()`
- On VS delete retry → `CLEANUP_RETRY.with_label_values(&["vector_store", &reason]).inc()`
- On VS deleted with failed attachments → `CLEANUP_VS_WITH_FAILED.inc()`

### 5.3 Backlog gauge

The backlog gauge needs periodic updates. Options:

1. **Update in handler** — decrement `pending` on each transition, increment `failed` on terminal failure. Risk: drift over restarts.
2. **Periodic query** — a background task runs `SELECT cleanup_status, COUNT(*) FROM attachments WHERE cleanup_status IS NOT NULL GROUP BY cleanup_status` and sets the gauge. More accurate.

Recommendation: Use option 2 — a lightweight periodic query (e.g., every 60s from the reconcile interval). This can be a simple addition to the existing worker loop or a separate metric-refresh tick.

## Acceptance Criteria

- [ ] All 5 metrics registered and exposed at `/metrics`
- [ ] Counter increments verified in unit tests
- [ ] Backlog gauge reflects actual DB state
- [ ] Metric names match DESIGN.md exactly
