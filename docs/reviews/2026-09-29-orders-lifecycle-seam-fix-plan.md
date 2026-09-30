# Orders Lifecycle seam remediation plan

Status: design remediation drafted, 2026-09-29; available static validation complete. Based on the [14-finding review](2026-09-29-orders-lifecycle-seam-review.md) and its pinned source revisions. This document supersedes the migration sequence proposed in the recovered Claude conversation; it does not itself adopt new commercial policy.

## Outcome and scope

Produce a Lifecycle design that consumes PriceBook correctly, exposes a complete Workflow SDK contract, and names exact reciprocal requirements for Pricing, Products, Rating and Subscriptions. Each finding must end as either a verified design correction or a concrete, explicitly open upstream dependency. Recording an ask does not make the runtime seam implemented.

The immediate deliverable is the design change in this worktree plus reviewable counterpart amendment proposals. Runtime crates, deployments and edits to other owners' working branches are subsequent work. Before any implementation, recheck the pinned baselines against the intended integration revisions and inspect applicable repository/workflow prerequisites. Reuse the current findings rather than restarting the review.

## Recommended positions

These are recommendations to make the design concrete. Commercial choices requiring counterpart agreement remain identified as proposals until that agreement is recorded.

| Topic | Recommended target | Ownership / unresolved part |
|---|---|---|
| Initial price binding | Preserve the accepted order's prices through an explicit initial-binding contract, separate from Pricing renewal resolve. Activation must not silently select a successor. | Pricing + Subscriptions + Orders must adopt it; no such implemented API is assumed. |
| Commercial deadline | Give the accepted binding an absolute activation deadline, independent of state TTLs. Do not extend it on hold/resume. Apply a conservative ceiling at explicit/temporary price end unless Pricing explicitly supports an exception. | Product sets the commercial duration; Pricing defines price-end policy. Specify the exact clock, timezone/date conversion and comparison boundary. No invented default duration. |
| Expired binding | Stop new activation. Before provisioning, require a freshly evaluated/approved version through an admissible path. After fulfillment starts, use the existing compensation/failure path if necessary; never silently reprice or introduce an unreviewed amendment edge. | Orders + Workflow + Subscriptions; specify partial activation and timeout/retry behavior. |
| Sellability | Keep one owning prospective-purchase evaluator in Pricing, incorporating Products facts. Orders adopts it and adds only Orders-specific predicates. | Proposed Pricing SDK capability; absence stays fail-closed. Do not duplicate Pricing policy in Orders as an interim implementation. |
| Overlap identity | Consume an opaque, authoritative overlap key; preserve product-family policy unless explicitly changed. Do not choose `plan_id` merely because it exists. | Subscriptions + product policy/catalog owner must define derivation, scope and cardinality. |
| Totals / TCV | Keep required totals fail-closed. Rating supplies complete order totals and TCV; Orders stores them verbatim. | Rating must amend its caller-summation contract. Optional totals require a separate coordinated product change. |
| Approval | Keep Lifecycle as verdict reflector and single transition writer. Specify an Orders approval-policy adapter consumed by Workflow; assess embedding the existing approval library behind that boundary. | Workflow/approval owner chooses the host and policy. A library is not an already available remote policy service. |
| SKU protection | First prove what existing plan/item references protect; add order reservations only if needed. Specify an atomic protection handoff rather than relying on a final read. | Products + Subscriptions + Orders; forced retirement policy remains explicit. |
| Events | Prefer bounded event projections plus authorized immutable-version reads when full expanded bindings cannot fit. | Lifecycle + Workflow + other affected consumers; preserve required provenance and explicitly version changed contracts. |

## Work packages and dependency order

Each package is a reviewable document change. Keep reciprocal contracts together in the review even if their implementation lands in different repositories or branches.

### P0 — Baseline, decisions and closure register

**Covers:** all findings; establishes the status of F1–F14.

- Add a PriceBook adoption ADR using the next available ADR number; reference Rating T-D-37/T-D-38 and Subscriptions SUB-D-29 as decided adoption with deferred adapters.
- Add decision/proposal entries for the positions above, retaining superseded history and stable requirement IDs where semantics remain the same. Recheck available decision IDs before assigning them.
- Build a seam table with producer, consumer, operation, request/response fields, tenant scope, timing, errors, retry/idempotency contract, owning document and implementation status.
- Reconcile conflicting `SUB-O*` aliases against the canonical Subscriptions seam register; do not create another independent numbering system.
- Add a counterpart-amendment document containing concrete proposed changes for the inspected Pricing, Products, Rating, Subscriptions and Workflow baselines. Include exact target files/sections, not just owner names.

**Files:** Lifecycle `ADR/`, `DECISIONS.md`, `UPSTREAM_REQS.md`, `DECOMPOSITION.md`; a proposed `docs/reviews/2026-09-29-bss-seam-counterpart-amendments.md`.

**Exit:** every finding has a work package, responsible gear and explicit closure criterion; proposed capabilities are distinguishable from built ones.

### P1 — Complete the Lifecycle–Workflow SDK contract

**Depends on:** P0. **Covers:** F9 and the interface portion of F13. Can proceed before the commercial decisions settle.

- Derive the SDK operations from Lifecycle's existing normative contracts: approval reflection, begin fulfillment, spawn signal, acknowledgement, workflow cancellation, hold/resume and authorized reads.
- Include deciding authority on all four approval verdicts and denial reason only on denial.
- Require explicit order-line/result/subscription mappings for completion; preserve exact roster and uniqueness validation.
- Carry expected version, idempotency key, correlation, closed failure reasons and compensation evidence. Specify outcomes needed for timeout replay, stale versions, held orders and previously committed transitions.
- Bind SDK and REST entry points to the same application/transition behavior and authorization. Do not import the Atlas signatures verbatim.
- Preserve amendment verdict retrieval, durable begin-fulfillment, the spawn fence, and compensation ordering in the Workflow counterpart proposal.

**Files:** Lifecycle `DESIGN.md` API contracts, `features/01-foundation.md`, `features/06-workflow-seam.md`, `features/08-read-and-authz.md`; proposed Workflow changes to `design/01`, `03`, `05`, `06`, `08`, `09`, `10` and its PRD where needed.

**Exit:** a request/response example for every operation is representable on both sides; all four approval verdicts and reordered completion lines preserve their required data.

### P2 — Specify purchase inputs, seller access and the shared gate

**Depends on:** P0. **Covers:** F3–F5.

- Define an order line as one subscription acquisition with a plan/revision identity and selected items. Define per-item quantities, optional selections and dimensions, preserving included allowances and unpriced included items.
- Freeze revision per line and assessment time/date per run; allow different plans/revisions across one basket. Pin currency/scale and identify the source of rounding policy.
- Separate selected dimension from the binding's own/default chain. Document exact conversion to Pricing's pin representation and how foreign/duplicate pins are rejected.
- Specify the shared prospective-purchase verdict: current revision, availability including null semantics, selected-chain coverage, quantity bounds, relevant SKU lifecycle/sellability and currency/market policy. Explicitly settle composition-only SKU behavior rather than requiring every component to be independently sellable.
- Distinguish new-purchase eligibility from activation of an already accepted binding. Define amendment as a new assessment without treating retries or activation as new signups.
- Specify seller-scoped Pricing/Products SDK requests, PDP authorization and error normalization; distinguish resource, ordering, payer and seller identities. Remove assumptions that a system actor grants cross-tenant access.
- Retain tri-state diagnostics and whole-basket consistency. Register missing capabilities as dependencies; no REST fallback or policy duplication.

**Files:** Lifecycle `features/02-capture.md`, `03-gate-and-pin.md`, `04-versioning.md`, `08-read-and-authz.md`; corresponding `DESIGN.md` schemas/contracts; Pricing/Products asks in `UPSTREAM_REQS.md`; matching producer-side proposals.

**Exit:** worked examples cover two plans in one order, an optional item, an included allowance, own-chain/default fallback, foreign seller scope and unavailable predicates.

### P3 — Define accepted bindings and activation admission

**Depends on:** P2. **Covers:** F1, F2, F12 and initial-binding parts of F10.

- Specify the accepted binding's versioned shape: source order/version/line, seller/catalog identity, plan revision, selected items/quantities/dimensions, exact price IDs, descriptor provenance, assessment time, applicable period boundaries and activation deadline. Distinguish this from non-authoritative displayed totals.
- Define how Subscriptions accepts that binding without a renewal walk, and when ordinary renewal rules begin. Specify behavior for superseded revisions, newer `all`/`new` prices, explicit closure and temporary-price ends.
- Define first-period slicing at `ends_on`; holding an initial price must not silently erase contractual period boundaries.
- Enforce deadline/protection validity at the receiving activation admission/commit boundary, not just in Workflow's earlier read. Define exactly-once retry after activation committed but its response was lost.
- Cover deadline expiry before wave 1, during wave 1, between activations and while held. Preserve evidence-gated compensation for partially activated orders.
- Specify durable SKU reservation/protection ownership if needed, retries/reconciliation, amendment replacement, terminal release and handoff to subscription protection. A passing read is not a reservation.
- Use the existing state machine where it supports the selected behavior; if an additional edge is actually necessary, revise the transition table, guards and event contract explicitly in this package.

**Files:** Lifecycle `features/03`, `04`, `05`, `06`, `07`; `DESIGN.md` version/binding schemas and activation rules; `DECISIONS.md`, `UPSTREAM_REQS.md`; reciprocal Pricing, Products, Subscriptions and Workflow proposals.

**Exit:** submit at price A then activate after B starts has one explicit outcome; deadlines and retirement cannot be bypassed by holds, concurrent activation or replay. Joint adoption is required to mark this seam agreed.

### P4 — Reconcile occupancy and provisioning

**Depends on:** P2; final provisioning DTO incorporates P3. **Covers:** F6, remaining F10, F11.

- Define authoritative overlap-key derivation and tenant namespace, and retain it with each accepted line. If ownership remains unresolved, leave the production gate blocked rather than substitute `plan_id`.
- Mirror the batched active-count/limit/provenance contract into Subscriptions. Exclude wave-1 drafts; specify effective limits and atomic activation enforcement, including multiple orders and non-Orders creation paths.
- Specify create/activate/void/cancel fields, reverse order/version/line references, actual activation start with no backdating, acceptance provenance and correlation.
- Preserve Workflow's versioned/wave/rebuild-aware idempotency keys and the distinction between draft void and activated cancellation. Add fee-free order compensation to Subscriptions' closed reason set and downstream consumers.
- Define the owner and SDK for revision-stamped provisioning topology. Map new line/item references to explicit dependencies; distinguish a verified empty graph from missing topology. Freeze before begin-fulfillment.

**Files:** Lifecycle overlap schemas, `features/03` and `06`, `UPSTREAM_REQS.md`; Subscriptions `SEAMS.md`, lifecycle/consumer/event designs; Workflow `design/04`, `05`, `06`, `10` in the counterpart proposal.

**Exit:** both sides use the same key and counts; concurrent activation is protected at Subscriptions' commit; retries do not duplicate creation; unknown topology cannot authorize fulfillment.

### P5 — Reconcile evaluation, approval and payment inputs

**Depends on:** P2; binding semantics from P3. **Covers:** F7, F8, F13.

- Define Rating's pre-purchase request with seller/payer/resource context, contract/market, selected items, exact accepted price inputs, quantities and term/cycle. A supplied order-price snapshot must not accidentally invoke renewal semantics in Rating's adapter either.
- Define whole-order and per-line figures with currency, scale and rounding, three charge kinds, usage exclusions, minimum fees and annualized net pre-tax TCV. Specify one-time preview versus downstream at-sale billing ownership.
- Define explicit unsupported/unavailable/excluded discount and promotion behavior. Keep totals required; preserve Preview's existing intentionally withheld-TCV cases rather than treating all missing TCV alike.
- Specify the Orders approval-policy adapter, its library integration option, named authority and failure posture. Align Workflow's required TCV payload and amendment re-approval.
- Map payment authorization amount/provenance separately from TCV. Retain Payments as an open owner contract with idempotent request identity and pending-outcome lookup; do not claim Ledger settlement supplies authorization.

**Files:** Lifecycle totals schemas, `features/03`, `05`, `06`, `UPSTREAM_REQS.md`, `DECISIONS.md`; Rating pre-purchase PRD/design; Workflow approval/payment design proposals.

**Exit:** monetary outputs require no Orders arithmetic; approval and payment receive the figures they require; missing evaluation cannot silently bypass approval.

### P6 — Propagate the design and enforce capacity bounds

**Depends on:** P1–P5 contract drafts. **Covers:** F14 and all document ripple.

- Update all active copies in `DESIGN.md` and feature files, public reads, event schemas/examples, failure catalogs, permission matrices and decomposition prerequisites.
- Record explicit PRD amendments for catalog/version replacement, item selection, charge kinds and accepted-binding policy. Mark superseded decision history rather than deleting it.
- Define bounded item/pin counts, request splitting, deduplication, whole-run deadlines and failure behavior for partial upstream responses. Do not preserve old latency claims without measurement.
- Choose event projections versus authorized versioned reads using worst-case payload sizing. Preserve billing/external-reference provenance, version identity, replayability, retention and access control. Register compatibility/version changes on both publishers and consumers.
- Update the explainer only after the normative design has settled, using the same binding and activation semantics.

**Exit:** the largest allowed basket has a representable bounded request/event strategy; no active contract still depends on removed frontier/cohort/product-key/setup-charge concepts without an explicit migration note.

### P7 — Validate and re-review against both sides

**Depends on:** P6.

- Resolve the installed CFS workflow prerequisites and supported validation command, then run validation for the changed registered artifacts. Do not treat CFS as coverage for every design slice: repository artifact configuration explicitly excludes some BSS slice patterns from autodetection.
- Check links, traceability IDs, reason enums, schema references, operation/event counts and transition-table consistency. Search for legacy terms and classify each remaining occurrence as history, migration mapping or unresolved dependency.
- Walk the review's acceptance scenarios using concrete request/response/event fixtures. Validate the proposed DTOs and serialize bounded payload examples when executable schema tooling is available; do not confuse illustrative fixtures with runtime tests.
- Recheck each caller request against its producer response and each emitted event against the consumer's required fields. Include authorization, ordering, error/retry, concurrency and commercial timing behavior.
- Publish an F1–F14 closure table with evidence and separate statuses: corrected in Lifecycle, counterpart proposed, counterpart agreed, implemented and runtime verified.

**Design exit:** no hidden incompatible assumption; every unimplemented or unagreed operation is an explicit readiness blocker. **Runtime exit, later:** owning SDKs exist, consumers integrate them and the concurrency/replay/price-change scenarios pass end to end.

## Practical execution order

Start P0 and P1, then draft P2. P2 enables the concrete initial-binding proposal (P3), overlap/provisioning contract (P4), and evaluation contract (P5). Resolve their shared business choices before treating dependent text as accepted. Fold them into the complete design in P6 and perform P7 against both source and consumer contracts.

No team contact is required to prepare the concrete reciprocal amendments. Agreement is an engineering dependency to record before closing the affected seam, not a reason to postpone the drafting work.

## Finding coverage

| Findings | Main package | Closure evidence |
|---|---|---|
| F1, F2 | P3 | Explicit initial binding, independent deadline, activation/renewal examples |
| F3, F4, F5 | P2 | Shared gate, purchase/pin adapter, seller-authorized producer contract |
| F6 | P4 | Identical key/count/limit semantics and receiver-side atomic enforcement |
| F7, F8 | P5 | Complete Rating result and consistent required-total flow |
| F9 | P1 | All authorities and explicit completion mappings preserved |
| F10 | P3 + P4 | Matched create/activate/void/cancel, start, provenance and retry contracts |
| F11 | P4 | Owning topology interface and completeness contract |
| F12 | P3 | Proven reference protection or explicit reservation/handoff protocol |
| F13 | P1 + P5 | Approval ownership and adapter contract, separate from library availability |
| F14 | P6 | Bounded request strategy and serialized event-size evidence |

Planning validation: this plan maps all 14 findings to work and acceptance criteria. No implementation checks or cross-team agreement are claimed by creating this file.

## Execution status

P0–P6 are drafted in this worktree. P7 static checks and illustrative sizing are recorded in the
[validation record](2026-09-29-orders-lifecycle-seam-validation.md). The
[counterpart amendments](2026-09-29-bss-seam-counterpart-amendments.md) contain the closure register.
Owner agreement, production schema sizing and executable integration checks remain pending; no
producer SDK or runtime seam is claimed complete.
