# Phase 6: Observability — Prometheus Metrics

## Goal

Wire the thread summary metrics specified in DESIGN.md into the trigger and handler paths.

## Current State

- `MiniChatMetricsPort` trait at `src/domain/ports/metrics.rs` defines the metrics interface.
- Existing metrics pattern: `record_*` methods on the trait, implemented via Prometheus
  counters/histograms in the infra metrics layer.
- DESIGN.md (lines 3522-3628) specifies:
  - `mini_chat_thread_summary_trigger_total{result}` (`scheduled|not_needed`)
  - `mini_chat_thread_summary_execution_total{result}` (`success|provider_error|timeout|retry`)
  - `mini_chat_thread_summary_cas_conflicts_total`
  - `mini_chat_summary_fallback_total`

## Tasks

### 6.1 Add metric methods to `MiniChatMetricsPort` trait

File: `src/domain/ports/metrics.rs`

```rust
/// Thread summary trigger evaluation result.
/// `result`: "scheduled" or "not_needed"
fn record_thread_summary_trigger(&self, result: &str);

/// Thread summary execution outcome.
/// `result`: "success", "provider_error", "timeout", or "retry"
fn record_thread_summary_execution(&self, result: &str);

/// Thread summary CAS conflict (another handler already advanced frontier).
fn record_thread_summary_cas_conflict(&self);

/// Summary fallback: previous summary kept because generation failed.
fn record_summary_fallback(&self);
```

### 6.2 Implement in infra metrics layer

File: `src/infra/metrics.rs` (or wherever the Prometheus impl lives)

Register counters:

```rust
// In the metrics struct:
thread_summary_trigger_total: IntCounterVec,       // label: "result"
thread_summary_execution_total: IntCounterVec,     // label: "result"
thread_summary_cas_conflicts_total: IntCounter,
summary_fallback_total: IntCounter,
```

Registration:

```rust
let thread_summary_trigger_total = register_int_counter_vec!(
    opts!("mini_chat_thread_summary_trigger_total", "Thread summary trigger evaluations"),
    &["result"]
)?;

let thread_summary_execution_total = register_int_counter_vec!(
    opts!("mini_chat_thread_summary_execution_total", "Thread summary execution outcomes"),
    &["result"]
)?;

let thread_summary_cas_conflicts_total = register_int_counter!(
    opts!("mini_chat_thread_summary_cas_conflicts_total", "Thread summary CAS conflicts")
)?;

let summary_fallback_total = register_int_counter!(
    opts!("mini_chat_summary_fallback_total", "Summary fallback (previous summary kept)")
)?;
```

### 6.3 Update noop/test metrics implementations

File: `src/domain/service/test_helpers.rs` (and any noop impl)

Add no-op implementations of the new metric methods so existing tests compile.

### 6.4 Verify metric emission points

Per Phases 4 and 5, verify these are called at the right places:

| Metric | Called from | When |
|--------|-----------|------|
| `record_thread_summary_trigger("scheduled")` | Phase 4 (trigger in finalization) | Outbox message enqueued |
| `record_thread_summary_trigger("not_needed")` | Phase 4 (trigger in finalization) | Threshold not exceeded or dedupe |
| `record_thread_summary_execution("success")` | Phase 5 (handler) | CAS commit succeeded |
| `record_thread_summary_execution("provider_error")` | Phase 5 (handler) | LLM call failed |
| `record_thread_summary_execution("retry")` | Phase 5 (handler) | Commit failed, returning Retry |
| `record_thread_summary_cas_conflict()` | Phase 5 (handler) | Pre-check or commit CAS lost |
| `record_summary_fallback()` | Phase 5 (handler) | LLM failed, keeping previous summary |

### 6.5 Alert rules (documentation only)

Per DESIGN.md (lines 3626-3628), the following alert rules should be configured in the
monitoring system (not implemented in code, documented for ops):

| Rule | Window | Severity |
|------|--------|----------|
| `rate(mini_chat_thread_summary_execution_total{result=~"provider_error\|timeout"}[15m])` sustained above threshold | 15m | warning |
| `mini_chat_outbox_dead_letter_backlog{kind="thread_summary"}` > 0 | 15m | critical |
| `mini_chat_thread_summary_cas_conflicts_total` above background threshold | 15m | warning |

## Acceptance Criteria

- [ ] All four metric families registered and exposed via `/metrics` endpoint
- [ ] Trigger metrics emitted in finalization path (Phase 4)
- [ ] Execution metrics emitted in handler (Phase 5)
- [ ] CAS conflict metric emitted on pre-check and commit CAS loss
- [ ] Fallback metric emitted when LLM fails
- [ ] Noop/test implementations compile
- [ ] Label cardinality bounded (only allowed `result` values, no unbounded labels)
