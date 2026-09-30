# Orders Lifecycle: round-2 review of the existing-seams fixes (D-159–D-168)

Date: 2026-09-30. Scope: the Lifecycle working tree after the
[existing-seams fix plan](2026-09-30-orders-lifecycle-existing-seams-fix-plan.md) was applied.
Producer baseline `diffora/bss/products` @ `16705a243`; Workflow sibling @ `e0c24ba50`.

Status: all recommendations applied to the Lifecycle documents on 2026-09-30 (decisions 1–4 taken
as recommended; D-160, D-161, D-162, D-164 amended in place). The Workflow deltas W1–W4 remain for
the Workflow worktree.

Method: three independent lenses (internal consistency; every factual claim in the new text checked
against the branch code and docs; implementability plus the Workflow cross-check), then every
featured finding re-read in the cited files by the coordinating session. Nothing was modified.

**Verdict.** The seam facts behind D-159, D-163, D-164, D-165, D-166, D-167 hold. Three things must
change before the design is buildable: `activation_deadline` has no producer any more, so every
submit would refuse; the comparison at activation uses the wrong resolve mode and refuses orders
Pricing would honour; and feature 03's CDSL still implements the withdrawn `assess` contract and
the old opaque overlap key. Below that are definitions an engineer would have to guess, and a set
of stale sentences left by the fix pass.

## Findings

### CRITICAL

**R2-1. Nobody produces `activation_deadline`, so every submit refuses.**
`DESIGN.md` §4.1 lists the deadline as a residual ask; §4.3 says "derived by the producer";
`UPSTREAM_REQS.md` §2.2 says "the producer of the assessment also returns `activation_deadline`".
`PricingPurchaseV1::assess` was withdrawn by D-161, `SellabilityV1` does not return a deadline, and
the field is required and non-null with a missing interval mapped to a 503. Nothing in Products or
Pricing holds a "maximum acceptance interval".
*Fix:* derive it locally as `min(earliest exclusive end over the consumed bound slots, assessed_at +
max_acceptance_interval)`; `max_acceptance_interval` is an Orders-owned, seller-scoped setting
configured like the date policy and failing closed with a local reason when unset. State that this is
date arithmetic over stored producer fields, not pricing arithmetic. Remove the row from the
residual asks. With the pinned comparison of R2-2, the per-slot end is `min(ends_on, temporary_until)`
(pinned-holder semantics, Pricing D-425); a known `all` successor is caught at activation, not early.

**R2-2. The comparison at activation uses a signup resolve and over-refuses.**
D-162 compares a **signup** resolve (no pins) on the activation date. Pricing's signup picks the price
in force regardless of eligibility (`domain/price.rs` `version_at`), so a `new` successor published
between submit and activation makes the answer differ and the order is refused, although a pinned
holder keeps the accepted price bound (`domain/resolve.rs` `walk` stops before `new`, `binds_on`
holds, apply marks the price `keep_for_bound`). The same happens for a temporary promotion pair
that starts and ends inside the interval: the return price is a new row with the same money.
*Fix:* compare a **pinned** resolve. Subscriptions sends the accepted bindings as pins, encoded by
the own-chain/default rule of §4.3, on the activation date, and refuses only when a consumed slot's
`binding.price_id` differs from the accepted one, that is only when the walk actually moved the
binding (an `all` successor started, or the accepted price ended). A non-null `pinned_from` is
accepted as provenance. Document the temporary-pair case as an accepted refusal (the walk also
lands on the return price). This replaces yesterday's argument that "supplying pins does not
implement a hold": the comparison is what turns the pinned resolve into a verified hold.

**R2-3. Feature 03 still implements the withdrawn contract.**
`features/03-gate-and-pin.md` §3.1 (line 150) and §3.6 steps 5 and 8 (lines 325, 333) still "adopt the
owning predicate set by reference" and normalize a tri-state `assess` answer; step 4 (line 323)
resolves "the opaque authoritative overlap key and namespace … no `plan_id`/SKU fallback"; the port
table (line 191) has a "binding composition" port; predicate 4 (line 477) registers
`catalog-predicates-unavailable` where DESIGN §4.1 registers `catalog-predicate-unevaluable`.
`DESIGN.md` port-budget rows 4901–4904 say the same.
*Fix:* rewrite §3.1 and steps 4, 5, 8 around the §4.1 table (six rows evaluated locally from
`PricingReadV1`, three residual rows through `SellabilityV1`), replace the key step with "request
the SUB-G1 key from Subscriptions for every proposed line", drop the composition port, relabel the
budget rows, and align predicate 4's reason.

### HIGH

**R2-4. "Consumed slot" is undefined, and every-slot comparison refuses valid orders.**
§4.3 stores "every slot it was answered" and compares "every consumed slot". If every stored slot
counts, a price change on a chain the customer did not pick, or a registry value removed or added,
refuses the order. There is also no rule turning `selected_dim_value` into a slot: the item DTO has
no dimension key, a null selection on a dimensioned item is unspecified, and an unregistered
selected value returns no chain.
*Fix:* consumed slot = the chain whose `dim_value` equals `selected_dim_value` (null = default
chain) of each consumed charged item; no such chain is `catalog-predicate-failed`
(`dim-value-unknown`); an item is dimensioned iff resolve answers a non-null chain, and a null
selection on one is allowed only when the default chain is bound. Compare and derive the deadline
over exactly that set; other slots are stored for information. Also compare
`sku_version.published_version`, or state that descriptors follow the activation date and the
order keeps its own as history (Subscriptions pins descriptors per period under SUB-D-29).

**R2-5. The currentness predicate has a type error.**
§4.1 writes `current_revision(plan_id).published_rev == plan_revision_id`. `PricingPlanDto.published_rev`
is `Option<i32>`, a revision number; `available_from` lives on `revisions[]`
(`PricingPlanRevisionHeader { id, rev_no, book_id, state, available_from, published_at }`).
`UPSTREAM_REQS.md` also says the 15 golden files freeze `current_revision`; none covers `GET /plans/{id}`.
*Fix:* the `revisions[]` entry with `id == plan_revision_id` has `state == published` and
`rev_no == published_rev`; read `available_from` from it; spell the three trait signatures; map
resolve 404 → `pricing-revision-absent`, 409 `REVISION_NOT_PUBLISHED` → `catalog-predicate-failed`;
correct the golden-file sentence.

**R2-6. The determinism and "Rating reaches the same bindings" claims are overstated.**
`WINDOW_START_IN_PAST` is `effective_from < today`, so a price starting today can be approved later
the same day; registry values and tenant settings are read at request time; Rating resolves at the
period start (T-D-37), which is not the activation date, and `active` commits at the OSS
confirmation. `DESIGN.md` §4.3 and DECISIONS D-162 claim both.
*Fix:* drop the determinism claim. In the Subscriptions amendment state: the comparison resolve runs
at the `applied` commit, the bindings it accepted are stored as the first period's pins, and the
first period's start is that date or Rating reads the stored `price_id`s for that period. The
`ends_on` cut owner stays Atlas decision 7. Also register that SUB-P5's "published plan" gate at
`create` must admit an accepted revision that was superseded after submit (resolve still answers
it; `create` today would not).

**R2-7. "No Products grant" contradicts the predicate that calls Products.**
D-160 and `UPSTREAM_REQS.md` §2.2 say no Products grant is needed; §4.1 reads `Sku.sellable`/
`Sku.lifecycle` through `ProductsClient::get_sku`, which is "within the caller's authorized scope"
(products-sdk `api.rs`) and runs a `sku:read` PDP check.
*Fix:* register a Products SKU read grant for `bss-orders.system` (recommended; the mechanism
exists) and drop "no Products grant"; keep the resolve echo as a later optimization.

**R2-8. The path Subscriptions uses to read the order version at activation is undefined.**
D-162 has Subscriptions read `get_version`; DESIGN 08 §4.3's event-consumer path grants the
Subscriptions principal reads only for "validating lifecycle notifications" under finite order-ID
restrictions, and nothing says who issues the per-order grant before Workflow calls `activate`.
Subscriptions would also depend on `orders-lifecycle-sdk` while Orders depends on Subscriptions'
occupancy read.
*Fix:* widen the path's purpose to "validate notifications and verify accepted content at
activation"; name the grant source (the order-ID set Workflow is granted for the same order); record
the SDK-level dependency pair as accepted, with no runtime loop.

**R2-9. Stale normative sentences left by the fix pass.**
- `DESIGN.md` 03 §3.8 (line 5451): seller tenant "named explicitly on the call rather than inferred
  from the caller's `SecurityContext`" → the `bss-orders.system` context built for the seller (D-160).
- `DESIGN.md` `orders_gate_outcome.catalog_scope_key` (5368, 5404): "upstream contract must provide
  a stable key encoding" → the slot identity is `(item_id, dim_value)` with a declared token for the
  default slot; drop the bundle "component plan" wording here and in `features/03` 208–209,
  `features/01` 421.
- `PRD.md` 298 (`fr-order-submit`): "adopt Pricing's shared prospective-purchase gate by reference …
  opaque namespaced key" → the six read rows plus the residual verdict; SUB-G1 key.
- `PRD.md` 1103: "(Generic Approval)" → Workflow's approval adapter (D-166).
- `UPSTREAM_REQS.md` §2.3 external reference (255–263) still says the hop is unspecified → the D-168
  route, snapshotted at the first handoff; add the external reference to the create inputs (130).
- `UPSTREAM_REQS.md` 370 "pass accepted initial bindings to Subscriptions" → pass the accepted
  version reference; Subscriptions reads `chains[]`. Fold `…-pricing-bundle-sellability` (224) into
  `…-pricing-read-sdk` (composition is what resolve returns) plus the residual verdict.

### MEDIUM

**R2-10. The 1,000-slot cap is per order, but Pricing's limit is per request, and a request is one line.**
`MAX_PINS` bounds one `GET /resolve`, which covers one revision; uncovered slots produce no pins.
*Fix:* cap **bound** slots at 1,000 per line; keep the 200-line, 200-item and 1 MiB limits; add the
case to the capacity fixture.

**R2-11. Definitions and cross-references to complete.**
- The composed read and the fulfillment inputs (DESIGN 08 §4.2, `features/08` 90, `UPSTREAM_REQS`
  334) must name `activation_deadline` and `accepted_version_ref`, and carry the full `OrderPin`.
- `features/06` 639: `order-binding-expired` also covers Subscriptions' refused comparison.
- `DESIGN.md` §4.3 preamble (5518), ADR-0003 line 31, ADR-0008 drivers 40 and consequences 64–67:
  still say initial binding / seller scope are upstream SDK asks.
- `PRD.md` 117, 179, 302, 1102: "separate acceptance contract", "accepted-binding evidence",
  "receiver-validated contract" → the pinned comparison.
- `features/01` 334: add the slot cap to the admission guards; `DESIGN.md` 2646: `billing_cycle ∈
  {month, year}`.
- Preview evaluates the deadline check and reports it but never refuses on it; `purpose` is local
  diagnostic metadata now that no producer receives it.
- D-163: say which SKU keys a line with two or more paid recurring items.

### LOW

**R2-12. Citations and wording.** D-160 cites "Pricing PRD §2.2 with `plan:read`/`price:read`"; the
subjects and grants are in D-424, and the tenant-scoped context pattern is Pricing's
`reference_ticker::system_actor` (`SecurityContext::builder().subject_tenant_id(tenant)`), which needs
an in-process SDK. D-164 cites "phase 3 plan rev 2"; the decision is D-414. "The Ledger only posts
settled money" is wrong: `LedgerClientV1` also has balanced postings, AR invoice balances, credit
application and revenue recognition; say "GL posting and settlement target; no invoices, no at-sale
valuation, no tax". `UPSTREAM_REQS.md` §4's decisions list omits D-150–D-168; line 775 lacks its
full stop.

## Workflow deltas (sibling design, not edited)

- **W1.** `order-binding-expired` does not exist in Workflow (0 hits); its abort reasons are
  `overlap-collision`/`market-divergence` only, and a Subscriptions price refusal would surface as
  `wave2-activation-failed`. Add the reason and map the refusal. Either check the deadline per
  wave-2 intent or let Lifecycle drop "Workflow checks it again before dispatch".
- **W2.** Workflow never maps Subscriptions' `applied`/`approved`/`oss_unconfirmed` (0 hits); its
  reconcile step can record `approved` as `activated`. State `activated ⇔ applied`,
  `oss_unconfirmed` → `wave2-activation-failed`.
- **W3.** The create envelope has `orderId/orderVersion/orderLineId` and the start instant but not the
  acceptance instant or `contractId`; `binding_reference` is a price-pin-era leftover.
- **W4.** 176 lines say "Generic Approval", 62 say "Catalog" (topology "owned by Catalog"), 3 say
  `catalogPricePin`, 0 say PriceBook; 116 links point at `orders-lifecycle/docs/design/*.md`, which
  the split to `features/` breaks.

## Decisions this round puts to the owner

1. Pinned-resolve comparison instead of signup comparison (R2-2). Recommended: yes.
2. `activation_deadline` derived locally with an Orders-owned seller-scoped maximum interval
   (R2-1). Recommended: yes; it removes the last blocking producer ask from the submit path.
3. A Products SKU read grant for `bss-orders.system` versus waiting for a resolve echo (R2-7).
   Recommended: the grant.
4. Slot cap per line on bound slots (R2-10). Recommended: yes.
