# Plan: close the three remaining Atlas alignment gaps

Status: proposed, 2026-09-29. Scope: F15–F17 from the
[full Atlas audit](2026-09-29-orders-lifecycle-atlas-followup.md), against its pinned producer revision
`16705a243f44e2a48eeb6ec903d2be34c854cf0d` and the current local Lifecycle amendments.
This plan does not change the normative contracts or claim producer agreement.

The deliverable is a coherent Lifecycle design plus concrete reciprocal amendments for Pricing,
Subscriptions, Rating and Billing. Implementing their SDKs and running integration tests remains a
separate delivery stage. Recheck the source revision before implementation; retain the prior local
work and stable traceability IDs where the underlying obligation is unchanged.

## 1. F15: model an item separately from its chain bindings

**Recommended contract:** keep one acquisition line per subscription and one composition entry per
item. Store a nested collection of chain outcomes/bindings beneath that item. Do not repeat items or
create subscriptions merely to represent dimension values.

Proposed shape, to be finalized in the reciprocal DTO contract:

```text
OrderPin.items[] {
  item_id, sku_id, treatment, selected, quantity, included_qty,
  sku_version, descriptor_snapshot,
  chains[] {
    dim_value,                 // held value; null is the default slot
    outcome: bound | uncovered,
    binding? {
      price_id, dim_used,      // null dim_used means the default price supplied this value
      eligibility, boundaries, ends_on, model, min_fee_context
    }
  }
}
```

- Replace singular `selected_dim_value`/`price_id`/`binding` assumptions in capture, stored pins,
  purchase assessment, exact-price evaluation, provisioning and read contracts.
- Recommend accepting the complete assessed chain matrix for each consumed priced item, matching
  the Atlas example. Keep optional-item selection separate from chain coverage. Unselected optional
  items and unpriced included allowances do not manufacture bindings.
- Record uncovered slots explicitly. Pricing's purchase policy must define which coverage is
  required for admission and how later use of an uncovered value is refused. Do not assume that
  every registered value must be covered, or that an uncovered value is free. If a restricted-value
  product mode is needed, make it an explicit counterpart policy, not an accidental DTO limitation.
- Enforce one slot per `(line_id, item_id, dim_value)` with canonical null/default semantics.
  Keep quantity and included allowance at item scope; chain expansion must not multiply them.
- Preserve the existing pin adapter: own-chain binding becomes the bare price ID; default fallback
  for value `v` becomes `price_id:v`. Deduplicate provider pins without losing consumer associations.
- Freeze accepted chain membership through initial activation. Specify renewal membership separately:
  new registry values follow the agreed signup rule; removed values retained by pins remain resolvable.
  Subscriptions must not silently expand the accepted initial sale by reading today's matrix.

**Capacity proposal:** retain the existing line/item and serialized-byte limits; add a working cap
of **1,000 total consumed chain slots per order**, counting bound and uncovered slots before provider
pin deduplication. This is an Orders admission proposal, not a Pricing limit or a measured capacity
claim. Fail the whole assessment on overflow; never truncate a matrix. Split Pricing calls only at
supported revision/item boundaries and retain the common assessment/deadline. A single item above the
cap is rejected: current `item_id` filtering does not justify splitting its chains into independent
requests. Check matrix size even when signup sends zero pins. Reconcile the 200-subrequest budget
with the new shape and preserve the 1 MiB version and 64 KiB event constraints.

**Acceptance checks:** the Atlas storage item round-trips five chains in one subscription; us binds
its own price while apac retains the default; unpriced included items produce no pins; duplicates,
uncovered required values and overflow refuse deterministically. Test exactly-at/above chain and
byte limits, including a single oversized item. Two values sharing a price share Rating's minimum
fee per subscription/period; two own prices retain separate floors.

## 2. F16: carry accepted prices through period Rating

**Recommended ownership:** Subscriptions owns durable period bindings and the schedule of price-end
cuts; Pricing owns resolution; Rating owns monetary evaluation. Rating consumes the exact recorded
bindings for a slice and does not independently walk them again. This amends both SUB-D-29 and
T-D-37's current overlapping resolution responsibilities; it requires counterpart adoption.

1. At activation, Subscriptions records authenticated provenance to the accepted order/version/line,
   the complete F15 matrix, exact descriptors and an explicit `initial_accepted` mode. Activation
   still checks the deadline/protection/occupancy and recovers committed outcomes before reevaluation.
2. The initial bindings apply from actual activation until the first period boundary or a relevant
   `ends_on`, whichever comes first. Publishing B with eligibility `all` alone does not change that
   initial slice. Do not use `effective_to` as a cut or extend A beyond its valid `ends_on`.
3. At a due `ends_on`, Subscriptions resolves the affected bindings on that date, persists the result
   and advances the slice history. Unaffected chains retain their bindings. Do not calculate unknown
   future replacement prices early. At the next period boundary, normal renewal resolves all held
   chains under Pricing's existing rules. Resolution mode distinguishes initial acceptance, a due
   price-end transition and ordinary renewal.
4. Specify a Subscriptions-owned, authorized binding-read SDK. The period fact carries the source
   identity and binding-set reference rather than embedding an unbounded matrix. The SDK returns
   explicit immutable slice revisions, provenance, coverage interval, completion/readiness status
   and exact bindings. Rating pins the returned revision when recording an evaluation; replay reads
   that revision, never today's mutable set. This is a proposed new contract, not an existing SDK.
5. Define how later due slices become available: durable readiness/retry notification or reconciliation,
   stable slice identities, contiguous non-overlapping coverage and an explicit incomplete response.
   Rating cannot interpret an incomplete/unavailable set as zero usage or free service. An advance
   charge covering an unresolved future cut needs an explicit deferral or correction contract; do
   not treat a future replacement price as already known.
6. Rating may read immutable money by exact price ID, but must not invoke normal renewal resolve on
   an accepted initial slice. Preserve item/chain associations, descriptor versions, minimum-fee
   grouping and coverage proration. One-time valuation stays in Billing's at-sale path; it must also
   reference the accepted binding rather than select the latest price.
7. Version the period-fact/read contracts together with their consumers. Preserve the existing
   period-fact identity, late-delivery semantics and adjustment rules; any slice notification is
   not an extra billable period. Specify retention and source authorization for historical replay.

**Decisions to record with the owners:** acceptance duration remains Product-owned; price-end or
protection revocation after acceptance needs an explicit effective-time policy; the above slice owner,
SDK, readiness transport and advance-billing behavior are proposals until adopted. Record these as
readiness blockers, not as completed runtime features.

**Acceptance checks:** accept A, publish B (`all`), activate within the deadline, and verify the first
rated slice still uses A. Test an `ends_on` inside the first period, a later renewal to B, a `new`
successor that normal renewal does not take, delayed/duplicate period facts, missing binding reads,
crash recovery at a cut and replay after subsequent price/descriptor changes. Include the F15
multi-chain case and shared minimum-fee coverage across slices. No duplicate billable period or
second price walk is allowed.

## 3. F17: correct Billing, tax, Payments and Ledger ownership

Replace the register's claim that Ledger supplies the billing chain with explicit capabilities:

| Capability | Proposed responsible component | Contract consequence |
|---|---|---|
| Invoice generation and at-sale valuation | Billing/invoicing; provider remains unassigned | Consume billable facts and accepted price provenance; Ledger availability does not satisfy this dependency |
| Indicative and invoice tax | Tax capability; provider remains unassigned | Preview uses an explicit indicative read; tax is not added to stored pre-tax totals |
| Authorization and payment-provider interaction | Payments/PSP capability; provider remains unassigned | Keep authorization outcome/recovery separate from settled money |
| Balanced postings, settlement, allocation, returns, disputes | Existing Ledger | Use its actual accounting SDK; no invented invoice/tax API |

**External-reference route recommendation:** Lifecycle → Workflow provisioning → Subscriptions
billable facts → Billing documents. Carry order/version/line identity and order/line external
references with explicit provenance. Because administrative references are mutable without a
commercial version bump, define a durable, revision-identified snapshot at the first provisioning
handoff and reuse it on retries. Later edits do not silently rewrite an emitted billing fact; specify
an explicit update/correction rule or document that they affect later handoffs only. Do not assume
an immutable commercial-version read reconstructs old administrative values. Keep references within
the reserved payload budgets; use an immutable reference bundle if necessary.

Keep operational compensation distinct from financial reversal: `order_compensation` must avoid
an early-termination fee, but that does not reverse money already billed. Billing must specify
idempotent reversal/credit handling linked to the original at-sale fact. Workflow must not block
its operational compensation acknowledgement waiting for a credit note.

**Acceptance checks:** external references survive order → subscription → billable fact → invoice;
a retry after an administrative edit retains the chosen original snapshot; missing Billing/tax
providers remain explicit blockers; already-posted money receives an idempotent Billing correction
without an unintended cancellation fee. Ledger settlement is never accepted as payment authorization.

## Files and implementation sequence

| Package | Local changes | Reciprocal changes |
|---|---|---|
| A — F15 shape and capacity | DESIGN pin/schema/read sections; PRD item semantics; features 02/03/04/08; capacity fixtures | Pricing purchase DTO; Rating quote inputs; Subscriptions create/activation binding DTO |
| B — F16 period handoff, after A | DESIGN acceptance/renewal contract; UPSTREAM_REQS; features 03/06; readiness/decomposition | Subscriptions SUB-D-29 and period-event/read contracts; Rating T-D-37/T-D-38 and period consumer; Workflow provisioning provenance |
| C — F17 ownership and reference route | UPSTREAM_REQS §§2.3/4; PRD affected integration wording; event/read/provenance text where required | Workflow create payload; Subscriptions facts; Billing expectations including at-sale valuation and reversal |
| D — Reconcile and validate | DECISIONS and ADR-0008 amendment; counterpart proposal; explainer; audit/closure/validation records | Explicit agreement and implementation status for each producer/consumer contract |

Execute A → B, then C and the complete consistency pass D. C can be drafted independently. Reserve
new decision/requirement IDs only after checking the current registers. Keep prior decisions as
annotated history. Do not edit neighbouring worktrees, send owner messages or create runtime crates
as part of this documentation scope.

## Validation and completion criteria

- Search every active Lifecycle and counterpart contract for singular-binding assumptions and stale
  Ledger ownership. Check producer outputs against consumer fields, error/retry behavior and tenant scope.
- Add concrete serialized multi-chain DTO fixtures and the price A/B timeline. Verify pin round-trip,
  association/uniqueness, capacity refusal and immutable-reference behavior with small meaningful checks.
  Label illustrative fixtures separately from executable producer/consumer tests.
- Run link/anchor checks, `git diff --check` and the registered-artifact CFS validation after resolving
  its configuration prerequisites. Compare errors to the recorded baseline; do not hide existing failures.
- Update F15–F17 and the reopened parts of F1/F4/F14 individually. Local design completion means all
  three gaps have coherent contracts and every missing owner/SDK is an explicit dependency.
- Runtime completion additionally requires counterpart adoption, implementing SDKs/wire schemas,
  production serializer bounds, and the multi-chain/first-period/replay/financial-provenance scenarios
  passing end to end. Documentation or synthetic fixtures cannot establish runtime compatibility.
