# BSS PriceBook: reciprocal contract amendments

Status: **proposed to counterpart owners**, 2026-09-29. No remote branches were edited and no owner
agreement or runtime implementation is asserted. Orders-side changes are in
[Lifecycle DESIGN](../../gears/bss/orders-lifecycle/docs/DESIGN.md) and ADR-0008.
Source baselines: Lifecycle `d4b95a1a8`, Pricing/Products/Rating/Subscriptions `16705a243`,
Workflow `e0c24ba50`. See the [review](2026-09-29-orders-lifecycle-seam-review.md) for source evidence.

## Contract conventions

All operations use owning SDK traits through ClientHub, with `SecurityContext` first and explicit
seller/catalog scope where relevant. Tenant IDs narrow an authorized lookup; they never confer
permission. Request-scoped deadlines encompass retries and batching. Business refusal, missing data,
permission denial and transient/unavailable service remain distinct at the producer boundary;
Lifecycle applies its documented buyer-safe normalization. Cross-tenant access requires real PDP
policy and scoped adapters; actor constants are not authorization.

Names below are concrete **proposed SDK names**, not exports found in the inspected code. Money
comes from Rating in integer minor units plus scale and rounding policy. Pricing's original money
objects remain exact decimal text; conversion and aggregation belong to Rating. Immutable order
references are `(order_id, order_version, line_id)` and are never replaced with today's version.

## Pricing: revisions, purchase assessment and initial acceptance

**Edit targets:** `gears/bss/pricing/docs/DESIGN.md`, `docs/design/04-plans.md`,
`docs/design/07-read-contract-events.md`, `docs/DECISIONS.md`, and the future public `pricing-sdk`.
Add the following consumer section beside D-419–D-425, preserving ordinary resolve/renewal semantics.

| Proposed operation | Input | Required output |
|---|---|---|
| `PricingPurchaseV1::revisions` | context, seller tenant, assessment ID/time/date, `{line_id, plan_id, plan_revision_id}[]` | Per-line found/missing revision, current published revision ID, state, availability/publication evidence, book/currency/scale/rounding and immutable item roster |
| `PricingPurchaseV1::assess` | same assessment/scope; revision roster; consumed item IDs, optional choices, quantities, selected dimensions; payer market; contract context as applicable | Complete per-item/chain tri-state outcomes, owner policy identity, market applicability, exact bindings, SKU version/descriptors and initial-acceptance evidence |
| `PricingReadV1::price` | context, seller tenant, approved price ID | Immutable stored price and owning entry/chain/book identity, regardless of current sale window |
| `PricingReadV1::resolve` | context, seller tenant, revision/date/item filter and renewal pins | Existing D-419 matrix semantics; explicitly not an initial-order hold |

`assess` must identify unselected optional items and unpriced included items separately from missing
prices. Every consumed charged item requires the selected chain to bind. A missing irrelevant matrix
cell is not a purchase failure. Predicate coverage includes currentness/availability, membership,
quantity, market and the owning SKU lifecycle/sellability rule. Define composition-only/deprecated
SKUs explicitly. A missing version as of assessment cannot fall back to a mutable head.

The assessment binds a revision **per line**, allowing different plans in one basket. It fixes selected
prices before Rating runs. Later successor publication does not change a completed assessment.
Availability null means at publication; assess on the UTC calendar date of the common assessment
instant. Any different commercial timezone must be an explicit versioned policy change across peers.

**Initial acceptance proposal:** return a finite exclusive `activation_deadline` plus policy identity
and verifiable acceptance/protection evidence tied to the exact selected roster. Deadline is no later
than the earliest explicit/temporary end at UTC midnight or the Product-specified maximum hold;
missing Product policy makes acceptance unavailable. Pricing must define and provide evidence of
whether an assessment at submit may remain accepted after price closure/revision supersession. A
successor alone must not reprice it. This capability does not exist merely because approved prices
are immutable and readable forever.

A read-only Preview may return indicative coverage/deadline feasibility but creates no commercial
reservation or accepted offer. Submit needs verifiable accepted evidence; if issuance requires durable
producer state, its idempotency identity is `(seller, order, proposed_version, authorized_principal, client_idempotency_key)` with
canonical original request fingerprint and lookup-by-request recovery, explicit expiry/release and reconciliation of uncommitted Orders
attempts. The producer may not leak perpetual reservations after a refused or crashed submit.
On uncertain issue outcome recover the existing receipt before reassessing; an expired recovered
receipt refuses and a fresh client key is needed for a new assessment. An amendment acquires the replacement evidence before the old accepted version becomes superseded;
failed amendments preserve the prior binding. Defining that issuance/lifecycle is part of the open
producer requirement, not an assumed Orders-owned cross-gear transaction.

**Acceptance cases:** A→B (`all`) between submit and activation; A→B (`new`); temporary/explicit end;
superseded revision; own-chain pin versus default fallback; multiple plans; selected optional and
unpriced included item; missing seller grants; amendment/submit failure after evidence issuance.

## Products: descriptors and protection

**Edit targets:** `gears/bss/products/docs/DESIGN.md`, reference/lifecycle design sections, decision
register and the future `products-sdk` contract. The current `ProductsClient::get_sku` reads a head;
`ReferenceRegistryV1::sku_version_as_of` and reference ownership are separately authorized surfaces.

First document the protection provided by existing `PriceBookEntry`/`PlanItem` references, including
superseded revision retention. If sufficient, expose verifiable proof and its validity for initial
acceptance; do not add an unnecessary Orders reservation. If insufficient, extend owner registration,
reference-kind enum, authorization, persistence constraints and reserve/confirm/release/recovery
consistently for an Orders owner. An enum-only change is not sufficient.

Proposed receipt fields: owner namespace, seller tenant, SKU/version roster, source assessment,
reservation/protection identity, status, validity limit and forced-retirement policy version.
Receipt validation must not be a racy head read. Define a durable transfer/continued-protection
protocol for Subscriptions: it establishes its protection before active admission, and order cleanup
cannot release the last protection of an active subscription. No distributed transaction is assumed.
Retries and reconciliation must converge after either side crashes. Forced retirement must have an
explicit refusal/revocation/compensation outcome; normal reserve protection is not proof against an
administrative override.

**Acceptance cases:** retirement before submit, after submit, during activation, after order
completion, and a lost handoff response. Preserve protection on failed amendment and on partial
activation compensation until downstream responsibility is settled.

## Subscriptions: initial binding, overlap and provisioning

**Edit targets:** `gears/bss/subscriptions/docs/SEAMS.md` SUB-G1/SUB-O1/O2/O5,
`DECISIONS.md` SUB-D-29, `design/01-foundation-lifecycle.md`, consumer/event contracts and the matching
SDK design. Preserve existing state semantics while adding explicit request fields.

| Proposed operation | Required request additions | Result / contract |
|---|---|---|
| `SubscriptionsProvisioningV1::create_draft` | ordering/resource/payer/seller identities; source order/version/line; accepted composition and evidence reference; explicit start intent; correlation; versioned Workflow create key | Draft ID/version and echoed source identity, with bidirectional provenance; no activation or billing yet |
| `activate` | draft ID, expected version, idempotency key, correlation, authenticated initial-binding reference | Initial activation validates source version/evidence, actual start, deadline, protection and overlap at admission; returns durable outcome with actual activation instant |
| `void` | draft ID/version and key | Non-resource-affecting cancellation of an unactivated draft |
| `cancel` | active subscription/version, key, mode and `order_compensation` reason | Compensation cancellation outside the fee/credit-generating early-termination class |
| `OverlapPolicyV1::resolve_keys` | seller context and prospective plan/revision/selections | Opaque namespaced key per line with stable policy/derivation provenance, or explicit no-key result |
| `SubscriptionsOccupancyV1::read` | context, distinct `(payer, overlap_key)[]` | Complete `active_count`, effective `max_concurrent_active`, policy provenance per key; drafts excluded |

The placement of `resolve_keys` may be a catalog-policy SDK rather than Subscriptions; the authoritative
owner and cross-seller/partner namespace must be agreed before implementation. `plan_id` is not the
default. Include examples where two plans belong to one exclusive family and where one partner buys
for multiple resource tenants. Preserve the independent one-in-flight-order invariant in Lifecycle.

Lifecycle reads are early aborts. Subscriptions must serialize cardinality enforcement at its own
active commit across Orders and non-Orders writers. A limit greater than one requires counts, not a
boolean. A receiver may reject after Workflow's check; that is a supported compensation path.

Activation uses the exact accepted initial prices, not `resolve(now, pins)`. Define the first billing
period and `ends_on` slices, followed by normal renewal. Use actual activation time for service start;
never backdate to a quoted date. Map Lifecycle acceptance evidence to Subscriptions' acceptance/date
trio explicitly; it is not an implicit timestamp copy. Lost-response replay returns committed success
before reevaluating an elapsed deadline. An uncommitted activation at/after the deadline refuses.
Specify how the receiver's admission/commit clock and the producer's deadline remain comparable.

Workflow create keys retain tenant/order/version/line/wave/kind and rebuild attempt. A line-only key
must not conflate amended orders or newly rebuilt drafts. Keep SUB-O1/O2/O5 canonical; register O9
(correlation) and O10 (explicit start) deliberately rather than reuse an existing number with a new
meaning. Propagate `order_compensation` to event consumers before release.

**Acceptance cases:** two concurrent activates; own wave-1 drafts; limit 2; changed partner resource;
activation deadline between the first and last line; replay after deadline; create then crash;
rebuild after draft TTL; compensation emits no early-termination fee/credit.

## Rating: exact-price pre-purchase result

**Edit targets:** `gears/bss/rating/docs/PRD.md` pre-purchase requirement,
`DECISIONS.md` T-D-37/T-D-38 and evaluation/consumer design. Explicitly replace caller-side order
summation with Rating-owned aggregation for this contract.

Proposed `PrePurchaseEvaluationV1::evaluate` request:

```text
context; assessment_id; assessed_at; resolve_date; seller/resource/payer identities;
contract reference; market; currency;
lines[{line_id, plan_revision_id, selected_items, exact_bindings,
       descriptor_provenance, term, billing_cycle}]
```

Result:

```text
assessment_id; echoed line/revision/selection/price identities;
currency; currency_minor_digits; rounding_policy; evaluation_policy_version;
items[{line_id, item_id, charge_kind, gross_minor?, net_minor?, discount_minor?,
       promotion_status, promotion_ref?, recurring_period?, usage_excluded}];
lines[{line_id, gross_minor, net_minor, discount_minor, recurring_by_cycle, one_time_minor}];
order{gross_minor, net_minor, discount_minor, recurring_by_cycle, one_time_minor, tcv_minor?};
min_fee_application; exclusions; tcv_withheld?  // only the documented Preview exception
```

Unknown/no-promotion/excluded states are explicit. No promotion may be represented as an explicitly
computed zero discount; unavailable evaluation must not be represented as zero. Usage has no committed
amount. `one_time` selection/preview does not create a usage/recurring evaluation unit; at-sale billing
remains owned downstream. Minimum-fee floors and quantity/coverage arithmetic are Rating's.

Exact bindings mean **do not perform a renewal walk**. Preserve price, dimension, descriptor and
assessment identities, including per-price minimum-fee grouping. TCV is net pre-tax, excludes usage,
counts one-time once and annualizes rolling recurring terms per supported cycle. Return labeled
per-cycle breakdowns; never sum monthly and annual rates into one unlabeled periodic rate. Preview
may withhold TCV for missing term/cycle exactly as Lifecycle specifies; submit may not.

**Acceptance cases:** monthly and annual lines; rolling term; one-time plus usage; own/default prices
sharing a minimum fee; successor published mid-evaluation; unsupported contract scope; missing quote
or TCV; integer-range/scale failure. Return a definite refusal/unavailable outcome, never truncate
money or let Orders convert it.

## Workflow: preserve sequencing and consume the complete SDK

**Edit targets:** `gears/bss/orders-workflow/docs/design/01-foundation.md`, `03-approval-execution.md`,
`04-fulfillment-plan.md`, `05-provisioning-intents.md`, `06-saga-and-compensation.md`,
`08-hold-and-cancel.md`, `09-read-and-authz.md`, `10-process-definition.md`, PRD and UPSTREAM_REQS.

Adopt [Lifecycle's SDK contract](../../gears/bss/orders-lifecycle/docs/DESIGN.md#orders-lifecycle-workflow-sdk):
all four verdicts carry authority, completion carries explicit line IDs and subscription IDs, and
immutable commercial content is fetched by `(order_id, version)`. Preserve application authorization,
existing key/retry semantics and all refusal handling. Keep current-state applicability checks separate
from historical version reads. No expanded pin needs to cross the serverless definition boundary;
steps read references inside the gear.

The new PriceBook item model changes plan-construction/provisioning inputs, not the proven ordering:
obtain/reflect approval after submit **and amendment**, freeze complete topology, commit begin-fulfillment,
create drafts, recheck, commit spawn fence, then activate. A deadline check before dispatch is useful
but not authority to activate; the receiver rechecks each admission. Add `order-binding-expired` to
failure mapping and event schemas. On expiry after any active line, stop dispatch and compensate
before reporting failure; do not fabricate a pre-activation rejection or auto-expire Lifecycle.
Protection or other receiver-admission failure maps to the existing provisioning-failure path with
its diagnostic cause preserved, unless a jointly added closed reason is agreed.

Define the topology producer with a batched `resolve_topology` request over accepted line/revision/item
references, returning a topology revision, per-line resolved marker, explicit dependency edges and
policy provenance. Empty-and-complete is valid; unknown or incomplete is not. PriceBook optional items
are not inter-subscription edges. The owner remains a release prerequisite, not an invented empty DAG.

Approval needs an explicit Orders policy adapter for required/not-required, routing and decisions.
The existing approval library may implement that adapter; its host/policy must be decided separately
from Lifecycle's verdict reflector. Missing TCV prevents the required approval request. Payment
amount/provenance is separate from TCV and remains an owning authorization contract with idempotent
request identity/pending lookup. Ledger settlement is not authorization.

Version-reference events introduce a commercial-content read dependency. Update consumers and PRD
wording together; preserve external-reference propagation to billing, immutable source identity,
retention/replay expectations and denied/unavailable-read recovery.

## Worked contract checks

These are design walkthroughs, not assertions that a running integration passed.

| Case | Required behavior | Prevented error |
|---|---|---|
| Price A accepted; B `all` starts before activation | Initial mode uses A while evidence valid; renewal may later choose B | Renewal resolver silently reprices initial sale |
| A ends at UTC midnight; activation occurs at that instant | Receiver refuses new activation; prior committed key still replays success | Inclusive deadline or expired-offer retry failure |
| Own `eu` price versus default fallback for `eu` | Own pin has null value; fallback pin carries `eu` | `PIN_FOREIGN` from confusing chosen value with pin override |
| Optional item unselected, matrix uncovered | No coverage obligation for that unselected item | Rejecting an otherwise valid purchase |
| Included usage allowance has no price entry | Preserve allowance with no invented price | Treating a free included item as unavailable |
| Two plans in one basket | Different revisions under common assessment, same explicit currency/market policy | One-revision-per-order restriction |
| Seller differs from caller/payer | Real seller-scoped PDP authorization; no tenant substitution | Cross-tenant leak or accidental caller catalog |
| Wave-1 drafts plus active limit 2 | Drafts excluded; active count plus pending checked; receiver serializes commit | Self-collision and concurrent over-allocation |
| Required approval without authority or TCV | Refuse missing authority/required figure | Optional-total approval bypass |
| Completed lines delivered in reverse order | Map by explicit line IDs and validate exact roster | Positional subscription misattribution |
| Deadline passes after first activation | Stop further dispatch; compensate first and void drafts; evidence before failure | Partial active order mislabeled rolled back |
| New revision after accepted version | Fetch and verify immutable accepted version; no current-content substitution | Stale event changes the customer's purchase |

## Closure register

**Revision 2026-09-30 (D-159–D-168, [existing-seams fix plan](2026-09-30-orders-lifecycle-existing-seams-fix-plan.md)):**
the Lifecycle side was pulled back onto the seams that already exist, which shrinks several of the
proposals above. Read the sections with these overrides:

- *Pricing*: `PricingPurchaseV1::revisions`/`assess` are withdrawn. Orders consumes
  `PricingReadV1 { resolve, price, current_revision }` over the three existing reads with their DTO
  names, as the Atlas's step 1 proposes, called as a `bss-orders.system` subject in the seller
  tenant (the D-424 pattern). The only residual Pricing ask is `SellabilityV1` for market
  applicability and any owner rule beyond the reads, plus `activation_deadline`. The
  receipt/issue-or-recover lifecycle is withdrawn: the reads write nothing and no receipt is needed.
- *Initial acceptance* (round 2, D-162): no new resolve mode. Subscriptions reads the accepted
  `chains[]` through `get_version`, sends the consumed slots as **pins** to the ordinary resolve on
  the activation date, and compares `binding.price_id` per consumed slot; the walk keeps the
  accepted price across `new` successors, so mismatch means an `all` successor or an ended price.
  Mismatch refuses (`accepted-price-mismatch`) and Workflow compensates. The comparison runs at the
  `applied` commit and its bindings are the first period's pins; SUB-P5's published-plan gate at
  `create` must admit a superseded accepted revision. The deadline is a locally derived early check.
- *Products*: `ReferenceKind::OrderLine`, the receipt and the transfer protocol are withdrawn.
  Protection is inherited from the revision's `plan_item` references (Pricing D-414); the one ask
  is that the revision-reference release report counts non-terminal orders that accepted the
  revision. New small ask: a SKU read grant for `bss-orders.system` on `ProductsClient::get_sku`.
- *Subscriptions*: `OverlapPolicyV1::resolve_keys` is withdrawn in favour of SUB-G1's registry key,
  with the paid recurring item's SKU as the proposed derivation. `create_draft` carries the accepted
  version reference and the `chains[]` matrix; `activate` performs the comparison. The acceptance
  mapping is fixed: acceptance instant → `customerAcceptedAt`, actual activation →
  `serviceActivatedAt`, `contractEffectiveAt` from the Contract; completion means `applied`.
- *Rating*: the request is the accepted matrix in resolve's vocabulary; periods are `month | year`.

**Follow-up qualification:** the [full Atlas audit](2026-09-29-orders-lifecycle-atlas-followup.md)
found F15 (multi-chain bindings), F16 (initial-price handoff to period Rating) and F17 (Billing/Ledger
ownership). These reopen parts of F1/F4/F14; the rows below record the first remediation pass, not
complete Atlas conformance.

“Design corrected” means the Lifecycle contract now addresses the finding; it does not mean the
counterpart has adopted it. All implementation/runtime columns remain pending.

| Finding | Lifecycle correction | Counterpart status | Runtime |
|---|---|---|---|
| F1 | Compare-at-activation over the ordinary signup resolve (D-162); exact-price Rating; normal renewal after | Subscriptions activate step; no Pricing change | Pending |
| F2 | Independent finite activation deadline, receiver admission, compensation | Product duration/clock policy open | Pending |
| F3 | Six predicates from `PricingReadV1` over the existing reads; residual `SellabilityV1` for market applicability (D-161) | `PricingReadV1` trait and residual verdict open | Pending |
| F4 | Selected items, per-line revision, own/default pin adapter | Pricing/Rating/Subscriptions proposal | Pending |
| F5 | D-424 pattern: `bss-orders.system` with `plan:read`/`price:read`, adapter in the seller tenant (D-160) | Pricing PRD §2.2 listing and PDP grant open | Pending |
| F6 | SUB-G1 registry key with the paid recurring item's SKU as proposed derivation (D-163); counts/limits and atomic activation | Subscriptions' derivation answer and SUB-O5 amendment open | Pending |
| F7 | Complete exact-binding totals, TCV and labeled cycles | Rating aggregation amendment open | Pending |
| F8 | Required totals remain fail-closed | Approval/payment input contracts open | Pending |
| F9 | All authorities and explicit completion mappings in SDK | Workflow signature adoption pending | Pending |
| F10 | Versioned provenance, start, idempotency and compensation asks | Subscriptions provisioning proposal | Pending |
| F11 | Complete revision-stamped topology required | Topology owner/API open | Pending |
| F12 | Protection inherited from the revision's `plan_item` references (D-164); no Orders reservation | Release report must count in-flight orders | Pending |
| F13 | Library availability separated from policy service | Orders approval-policy host open | Pending |
| F14 | 200 consumed items, 1,000 chain slots, 1 MiB version, bounded event/read strategy (D-159) | Capacity/consumer conformance pending | Pending |
| F15 | `items[].chains[]` nested as resolve answers the matrix; slot uniqueness and cap (D-159) | Subscriptions create/activate DTO adoption pending | Pending |
| F16 | First-period pins are the activation bindings the comparison accepted (D-162); residual is the `ends_on` cut owner | Atlas decision 7 open | Pending |
| F17 | Ledger separated from invoicing, tax and Payments (D-168) | Unowned capabilities remain explicit | Pending |
