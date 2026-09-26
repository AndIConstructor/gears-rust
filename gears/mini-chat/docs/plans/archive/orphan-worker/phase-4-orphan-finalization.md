# Phase 4: Orphan Finalization — Domain Logic

## Goal

Add `finalize_orphan_turn` method to `FinalizationService` that uses the orphan CAS and reuses billing derivation, quota settlement, and outbox enqueue — all within a single DB transaction.

## Current State

- `FinalizationService::finalize_turn_cas(FinalizationInput)` is the universal finalization function. It requires many streaming-context fields (`accumulated_text`, `usage`, `ttft_ms`, `provider_response_id`, `message_id`, etc.) that are not available to the orphan watchdog.
- `derive_billing_outcome` at `src/domain/model/billing_outcome.rs:88-93` already maps `Failed + orphan_timeout → Aborted/Estimated`.
- `QuotaSettler::settle_in_tx` takes `SettlementInput` with fields: `tenant_id`, `user_id`, `effective_model`, `policy_version_applied`, `reserve_tokens`, `max_output_tokens_applied`, `reserved_credits_micro`, `minimal_generation_floor_applied`, `settlement_path`, `period_starts`, `web_search_calls`, `code_interpreter_calls`.
- The turn row (`TurnModel`) contains all quota fields: `tenant_id`, `requester_user_id`, `effective_model`, `policy_version_applied`, `reserve_tokens`, `max_output_tokens_applied`, `reserved_credits_micro`, `minimal_generation_floor_applied`.

## Design Constraints

From DESIGN.md:
- MUST NOT implement a second, divergent finalization path — reuse `derive_billing_outcome`, `QuotaSettler`, `OutboxEnqueuer`.
- Billing outcome for orphan timeout: `ABORTED` (not `FAILED`), `settlement_method = estimated`.
- Outbox usage payload: `outcome = "aborted"`, `settlement_method = "estimated"`.
- Orphan path has NO provider usage, NO accumulated text, NO message to persist.

## Tasks

### 4.1 Define `OrphanFinalizationInput`

File: `src/domain/model/finalization.rs`

```rust
/// Simplified input for orphan finalization.
/// Built from the `TurnModel` row — no streaming context available.
#[domain_model]
#[derive(Debug, Clone)]
pub struct OrphanFinalizationInput {
    pub turn_id: Uuid,
    pub tenant_id: Uuid,
    pub chat_id: Uuid,
    pub request_id: Uuid,
    /// From `requester_user_id`. Nullable because some turns may have system requesters.
    pub user_id: Option<Uuid>,
    pub effective_model: Option<String>,
    pub reserve_tokens: Option<i64>,
    pub max_output_tokens_applied: Option<i32>,
    pub reserved_credits_micro: Option<i64>,
    pub policy_version_applied: Option<i64>,
    pub minimal_generation_floor_applied: Option<i32>,
    /// `started_at` — used to derive `period_starts` for quota settlement.
    pub started_at: OffsetDateTime,
}
```

Add a constructor from `TurnModel`:

```rust
impl OrphanFinalizationInput {
    pub fn from_turn(turn: &TurnModel) -> Self {
        Self {
            turn_id: turn.id,
            tenant_id: turn.tenant_id,
            chat_id: turn.chat_id,
            request_id: turn.request_id,
            user_id: turn.requester_user_id,
            effective_model: turn.effective_model.clone(),
            reserve_tokens: turn.reserve_tokens,
            max_output_tokens_applied: turn.max_output_tokens_applied,
            reserved_credits_micro: turn.reserved_credits_micro,
            policy_version_applied: turn.policy_version_applied,
            minimal_generation_floor_applied: turn.minimal_generation_floor_applied,
            started_at: turn.started_at,
        }
    }
}
```

### 4.2 Add `finalize_orphan_turn` to FinalizationService

File: `src/domain/service/finalization_service.rs`

```rust
/// Finalize an orphan turn within a single database transaction.
///
/// Steps:
/// 1. CAS finalize orphan (re-checks all predicates)
/// 2. If CAS won: derive billing, settle quota, enqueue usage event
/// 3. Return whether CAS was won
///
/// Called by the orphan watchdog for each candidate. Safe under retries
/// and concurrent finalization — CAS ensures at-most-once.
pub async fn finalize_orphan_turn(
    &self,
    input: OrphanFinalizationInput,
    timeout_secs: u64,
) -> Result<bool, DomainError>
```

Transaction body:

```
1. turn_repo.cas_finalize_orphan(tx, input.turn_id, timeout_secs)
   → rows_affected = 0 → return Ok(false)  // CAS loser

2. derive_billing_outcome(BillingDerivationInput {
       terminal_state: Failed,
       error_code: Some("orphan_timeout"),
       has_usage: false,
   })
   → Aborted / Estimated  (confirmed by existing tests)

3. Guard: if quota fields are available (effective_model, user_id, reserve_tokens
   are all Some), build SettlementInput and call quota_settler.settle_in_tx().
   If any required field is None → log warning, skip settlement (turn still
   transitions to Failed, but quota is not debited — acceptable edge case
   for turns that failed during preflight).

4. Build UsageEvent:
   - terminal_state: "failed"
   - billing_outcome: "aborted"
   - settlement_method: "estimated"
   - usage: None (no provider usage)
   - error_code: "orphan_timeout"
   - All quota fields from turn row
   Call outbox_enqueuer.enqueue_usage_event(tx, usage_event)

5. After transaction commit: outbox_enqueuer.flush()

6. Return Ok(true)
```

### 4.3 Construct `AccessScope` from turn row

The `QuotaSettler::settle_in_tx` requires `&AccessScope`. Construct a tenant-scoped scope:

```rust
use toolkit_security::{AccessScope, ScopeConstraint, ScopeFilter};

let scope = AccessScope::from_constraints(vec![
    ScopeConstraint::new(vec![
        ScopeFilter::eq(pep_properties::OWNER_TENANT_ID, input.tenant_id),
    ]),
]);
```

Check the test helpers for the exact construction pattern — they build `AccessScope` for similar background-worker contexts.

### 4.4 Handle missing quota fields

Some turns may have `NULL` quota fields (e.g., turns that crashed before preflight completed). In this case:

```rust
let has_quota_fields = input.effective_model.is_some()
    && input.user_id.is_some()
    && input.reserve_tokens.is_some()
    && input.policy_version_applied.is_some();

if has_quota_fields {
    // Build SettlementInput and settle
} else {
    warn!(turn_id = %input.turn_id, "orphan turn missing quota fields, skipping settlement");
}
```

The CAS update to `Failed/orphan_timeout` still succeeds (it doesn't depend on quota fields). The user is unblocked, which is the primary goal. The uncommitted quota reserve will eventually be reconciled by the quota reconciliation job (if one exists) or will expire with the quota period.

### 4.5 Build `SettlementInput` from turn row

```rust
let settlement_input = SettlementInput {
    tenant_id: input.tenant_id,
    user_id: input.user_id.unwrap(),  // guarded by has_quota_fields check
    effective_model: input.effective_model.clone().unwrap(),
    policy_version_applied: input.policy_version_applied.unwrap(),
    reserve_tokens: input.reserve_tokens.unwrap(),
    max_output_tokens_applied: input.max_output_tokens_applied.unwrap_or(0),
    reserved_credits_micro: input.reserved_credits_micro.unwrap_or(0),
    minimal_generation_floor_applied: input.minimal_generation_floor_applied.unwrap_or(0),
    settlement_path: SettlementPath::Estimated,
    period_starts: /* see Open Questions */,
    web_search_calls: 0,
    code_interpreter_calls: 0,
};
```

### 4.6 Build audit event from turn row

The orphan path enqueues an audit event (`AuditEnvelope::Turn`) alongside the usage event, same as the normal finalization path. Fields not available from `chat_turns` use sensible defaults:

```rust
AuditEnvelope::Turn(TurnAuditEvent {
    event_type: TurnAuditEventType::TurnFailed,
    timestamp: OffsetDateTime::now_utc(),        // time of watchdog finalization
    tenant_id: input.tenant_id,
    requester_type: /* parse turn.requester_type String → RequesterType */,
    trace_id: None,                               // no active span in background worker
    user_id: input.user_id.unwrap_or(Uuid::nil()),
    chat_id: input.chat_id,
    turn_id: input.turn_id,
    request_id: input.request_id,
    selected_model: input.effective_model.clone().unwrap_or_default(),  // *
    effective_model: input.effective_model.clone().unwrap_or_default(),
    policy_version_applied: input.policy_version_applied.map(|v| v as u64),
    usage: AuditUsageTokens {                     // no provider usage available
        input_tokens: 0,
        output_tokens: 0,
        model: input.effective_model.clone(),
        cache_read_input_tokens: Some(0),
        cache_write_input_tokens: Some(0),
        reasoning_tokens: Some(0),
    },
    latency_ms: LatencyMs { ttft_ms: None, total_ms: None },
    policy_decisions: PolicyDecisions {
        license: None,
        quota: QuotaDecision {
            decision: "unknown".to_owned(),       // *
            quota_scope: None,
            downgrade_from: None,                  // *
            downgrade_reason: None,                // *
        },
    },
    error_code: Some("orphan_timeout".to_owned()),
    prompt: None,
    response: None,
    attachments: vec![],
    tool_calls: None,
})
```

Fields marked `*` are not persisted in `chat_turns` — they use best-effort defaults:
- `selected_model` = `effective_model` (pre-downgrade model not stored)
- `quota_decision` = `"unknown"` (decision not stored)
- `downgrade_from` / `downgrade_reason` = `None`

`timestamp` is `now()` — reflects when the watchdog finalized the turn, not when the pod crashed. This is correct: the audit event records the finalization action, consistent with how `build_turn_audit_envelope` works in the normal path.

**Future improvement**: persist `quota_decision`, `selected_model`, `downgrade_from`, `downgrade_reason` in `chat_turns` so any background finalizer can reconstruct a complete audit event. Out of scope for this plan.

## Open Questions

- **`period_starts` for `SettlementInput`**: The `Estimated` settlement path needs `period_starts` to update the correct quota usage rows. Options:
  (a) Query `quota_usage` table within the transaction for existing reserve rows for this turn's tenant+user+model+period.
  (b) Derive from `input.started_at` — the turn was started on a specific date, compute the daily and monthly period starts from that date.
  (c) Use empty vec if `QuotaService::settle_in_tx` handles missing period_starts on the Estimated path (investigate).
  **Recommendation**: Option (b) — derive from `started_at` using the same period computation as the preflight path. This avoids an extra query and is deterministic.

## Acceptance Criteria

- [ ] CAS winner: turn transitions to `Failed/orphan_timeout`, quota debited (estimated), usage event enqueued
- [ ] CAS loser: returns `false`, no side effects (no quota, no outbox)
- [ ] Missing quota fields: turn still finalized, settlement skipped with warning
- [ ] No `accumulated_text` or message persistence in orphan path
- [ ] `derive_billing_outcome` produces `Aborted/Estimated` for `orphan_timeout` (already verified by existing test at `billing_outcome.rs:183-188`)
- [ ] Outbox `flush()` called post-commit on CAS win
