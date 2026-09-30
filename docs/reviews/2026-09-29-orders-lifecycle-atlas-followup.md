# Orders Lifecycle alignment with the full BSS Seam Atlas

**Verdict: partially aligned. The PriceBook migration fixes much of the old model, but three gaps
remain in the amended Lifecycle documents. We cannot claim full Atlas or end-to-end conformance.**

This is a follow-up to the [original review](2026-09-29-orders-lifecycle-seam-review.md), reviewing the
actual edited Lifecycle documents rather than the pre-remediation baseline. Findings F15–F17 extend
the original register; they qualify the earlier F1/F4/F14 design-closure claims.

## Evidence and scope

- On 2026-09-29, `git ls-remote https://github.com/diffora/gears-rust.git refs/heads/bss/products`
  returned `16705a243f44e2a48eeb6ec903d2be34c854cf0d`. The inspected clean local checkout has that
  exact HEAD. No fetch/checkout or modification of the producer branch was needed.
- The recovered Atlas is the complete 824-line `atlas.txt`, plus its extracted traits, pins/needs
  and decisions. It labels its traits draft/proposed. Its header cites `bss/pricebook @ c932222`,
  while the extracted signatures mention `5afbf97`; code facts below are checked against the
  user-requested `bss/products` HEAD instead of treating either artifact baseline as current code.
- Pricing/Products/Ledger/approval: source code and golden fixture inspection. Rating/Subscriptions:
  their branch design decisions and contracts; they do not have implementing SDKs here.
- Lifecycle: amended local working tree. Workflow ordering was additionally checked against the
  existing sibling design checkout at `e0c24ba50`; that is a separate source, not the branch HEAD.
- GitHub's browser fetch was unavailable; authenticated Git access and the exact matching clean
  checkout supplied the code evidence. No runtime or Rust test suite was executed. Reading a golden
  fixture confirms its specified expected response, not that its test passed during this audit.

## Remaining findings

### F15 — HIGH: one binding per item cannot represent the Atlas's multi-chain subscription

**Lifecycle evidence:** [purchase/OrderPin shape](../../gears/bss/orders-lifecycle/docs/DESIGN.md#contract-03-4-3)
uses `selected_items[].selected_dim_value`, then `items[].price_id`, `items[].dim_used` and one
`items[].binding` for each item in the complete revision roster. It does not specify repeated item
IDs or a nested chain-binding collection.

**Producer evidence:** Pricing exposes `items[].chains[]`, not a single binding per item:
[resolve DTO](https://github.com/diffora/gears-rust/blob/16705a243f44e2a48eeb6ec903d2be34c854cf0d/gears/bss/pricing/pricing/src/api/rest/read_contract/dto.rs#L33).
The [default-pin golden](https://github.com/diffora/gears-rust/blob/16705a243f44e2a48eeb6ec903d2be34c854cf0d/gears/bss/pricing/pricing/tests/contract/resolve_default_pin_moves.json)
contains five chains for the single storage item: default, eu, us, apac and latam. On 2026-10-05,
us uses its own price while apac still uses the default. The Atlas §06 subscription holds all those
bindings; Subscriptions SUB-D-29 also describes the whole matrix.

**Impact:** the current Orders shape cannot losslessly hand off that accepted matrix. Splitting
one storage item into multiple acquisition lines would create multiple subscriptions and change
minimum-fee grouping. Restricting an item to one selected dimension could be a separate product
policy, but no such restriction is agreed with Subscriptions or the Atlas.

**Required correction:** keep selected item composition separate from held chain bindings, e.g.
`items[].bindings[{dim_value, dim_used, price_id, ...}]`, with uniqueness on `(line, item, held value)`.
Specify whether the subscription acquires the complete matrix or an explicitly restricted set,
including behavior for newly registered/removed values. Preserve own-price versus default-fallback
pin encoding and exact descriptor provenance.

**Capacity consequence (reopens part of F14):** 200 consumed items does not bound the number of
chains/pins. Define a separate held-binding limit and split/reject strategy, including the case of
one item alone exceeding Pricing's 1,000-pin request cap. The prior synthetic 200-item sizing does
not test this case. Repeated items must not multiply included allowances or minimum fees.

**Acceptance case:** one acquisition line, one storage item with all five chains, exact round-trip
through acceptance, activation and period facts; us and apac remain distinct; one shared default
price incurs the owning Rating floor once for the subscription/period, not once per chain.

### F16 — HIGH: the initial accepted price is not carried through the first Rating period

**Lifecycle evidence:** the amended pin and [counterpart proposal](2026-09-29-bss-seam-counterpart-amendments.md#subscriptions-initial-binding-overlap-and-provisioning)
require Subscriptions to activate on accepted prices without a renewal walk. The Rating amendment
specifies exact-price **pre-purchase** evaluation. Neither amendment specifies how the first
`BillableItemCreated`/period-binding read tells period Rating to preserve that initial mode.

**Counterpart evidence:** Atlas §07 and
[Rating T-D-37's read-contract amendment](https://github.com/diffora/gears-rust/blob/16705a243f44e2a48eeb6ec903d2be34c854cf0d/gears/bss/rating/docs/DECISIONS.md#L91)
resolve at period start with the subscription's pins. The
[renewal golden](https://github.com/diffora/gears-rust/blob/16705a243f44e2a48eeb6ec903d2be34c854cf0d/gears/bss/pricing/pricing/tests/contract/resolve_renewal_walk.json)
explicitly maps pin `price:10` to `price:12` on 2026-12-05. Passing the accepted pin alone does not
stop that walk. Subscriptions and Rating both currently describe cutting at `ends_on`; Atlas
open decision 7 asks who owns the cuts and how bindings reach Rating.

**Impact (inference from those contracts):** even if Subscriptions activates at accepted A, period
Rating can immediately walk to B. The initial-price promise could fail at billing after apparently
successful activation. The same-contract issue applies to descriptor provenance and chain coverage
if the two consumers independently resolve at different times.

**Required correction:** extend the reciprocal proposal to **period evaluation**, with authenticated
initial-versus-renewal provenance and exact initial slice bindings, an agreed period-fact or binding-read
transport, and explicit owner/timing for `ends_on` cuts. Do not precompute an unknown future cut's
replacement binding or freeze A past its contractual end. Specify when normal renewal begins and
how replay recovers the original rated inputs. Orders need not own that downstream store, but its
commercial promise depends on the contract existing.

**Acceptance case:** submit A; publish B (`all`); activate before the accepted deadline; first
rated charge uses the agreed accepted A slice. At an actual `ends_on`, the chosen owner resolves the
next slice on the agreed date. Later renewal follows normal rules. Test lost-response/replay and
binding/descriptor equality between Subscriptions and Rating.

### F17 — MEDIUM: the upstream register still assigns billing-document responsibility to Ledger

**Lifecycle evidence:** [UPSTREAM_REQS §4](../../gears/bss/orders-lifecycle/docs/UPSTREAM_REQS.md#4-traceability)
says the billing chain is specified as `gears/bss/ledger`, that both billing asks must target that
specification, and that Payments alone has no specification/register. Section 2.3 separately leaves
the external-reference hop to invoices unspecified.

**Producer evidence:** Atlas components/trait map distinguishes missing Billing/invoicing and PSP
integration from the built Ledger. The
[Ledger SDK](https://github.com/diffora/gears-rust/blob/16705a243f44e2a48eeb6ec903d2be34c854cf0d/gears/bss/ledger/ledger-sdk/src/api.rs#L22)
provides balanced posting, settlement, allocation, returns, disputes and accounting reads; that is
not an invoice-generation, billable-item valuation or indicative-tax contract.

**Impact:** external-reference propagation and at-sale valuation cannot be considered assigned to a
provider merely because Ledger exists. The new proposal correctly separates payment authorization
from settlement, but this older ownership claim remains contradictory.

**Required correction:** distinguish Billing/invoice/valuation, tax, PSP/Payments and Ledger owners
in the register. Leave genuinely unowned capabilities explicit. Choose the source/provenance route
for order/line external references through Subscriptions facts or authorized event/version reads,
and keep the fee-free compensation reason separate from reversal of already posted at-sale money.

## Full Atlas coverage matrix

“Aligned” below means the local design follows the source model or correctly declares a missing
capability. It does not assert an implemented or jointly accepted SDK.

| Atlas area | Current Lifecycle alignment | Remaining work |
|---|---|---|
| PriceBook replaces catalog version/frontier/cohort | Aligned | Old names remain only as traceability/history where annotated |
| Plan revision per acquisition; immutable approved prices | Aligned | Seller-scoped SDK still missing |
| Paid/optional/included items and unpriced allowances | Aligned for item selection | Multi-chain shape is F15 |
| Own-chain versus default-value pin encoding | Aligned for one binding | Extend across matrix, dedup and limits under F15 |
| Three charge kinds; no `one_time_setup` | Aligned | One-time preview is not a Rating runtime unit |
| Resolve is not purchase eligibility | Aligned as explicit producer requirement | Shared gate/currentness/availability not built |
| Seller-tenant reads and authorization | Aligned as explicit requirement | Products registry remains Pricing-only; new grants/API needed |
| Initial price hold versus renewal | Initial-activation target specified | Joint deadline policy open; first billing handoff missing, F16 |
| Per-period pins, descriptors and `ends_on` | Boundary recognized | Transport, cut ownership and initial-mode propagation open, F16 |
| Overlap key after Product removal | Correctly open; no assumed `plan_id` | Authoritative owner, counts/limits, atomic active admission required |
| SKU protection in flight | Correctly open; avoids enum-only fix | Prove existing references or implement durable handoff/protection |
| Totals/TCV ownership | Rating-owned target, fail-closed | Producer currently lacks whole-order/TCV contract; proposal unagreed |
| Minimum fees | Rating-owned, per-price grouping retained | Verify grouping across multi-chain handoff under F15/F16 |
| One-time billing and compensation | Downstream ownership retained | PriceBook replacement for legacy phase/charge-line occurrence identity and Billing valuation remains upstream work |
| Workflow approval and line mapping | Stronger than lossy Atlas draft | All verdict authorities and explicit completion line IDs specified |
| Begin-fulfillment/spawn fence/amendment approval | Existing sibling design aligns | Atlas PRD warnings are not proof those operations are absent from the detailed design |
| Workflow SDK and immutable reads | Local contract specified | Crate/consumer implementation and event schema adoption pending |
| Create/activate/void/cancel, start, retry and compensation | Reciprocal target specified | Producer SDK/closed errors/recovery still pending; keep Workflow transition-status lookup, not assumed activation-failure events |
| Provisioning dependencies between lines | Correctly open | PriceBook optionals do not supply a cross-plan topology; owner needed |
| Approval library versus policy | Aligned | Built Engine/ApprovalSubject/Store is not an Orders policy service; host/routing/thresholds open |
| Payment authorization versus Ledger settlement | Aligned | Payments/PSP owner absent; settlement is no substitute |
| Billing documents, at-sale valuation, tax | Partly aligned | Stale Ledger ownership claim is F17 |
| Rating composition reads, usage attribution/feed, Billing periodState | Transitive dependencies acknowledged by Atlas, not Lifecycle APIs | Must remain in Rating/Subscriptions release prerequisites; Orders should not invent those implementations |
| Contracts, Account Management, event broker/platform | Outside Atlas's stated coverage | Existing Lifecycle requirements remain; this audit does not certify them |

## Verification and conclusion

Read the full recovered artifact, checked the requested remote branch SHA, inspected relevant
Pricing/Products/Ledger/approval source and Rating/Subscriptions decisions, and decoded Pricing's
golden JSON to verify the five-chain example and renewal-walk counterexample. The branch has 15
contract JSON files today; the Atlas's count of 14 is historical, not evidence of an extra tested
runtime capability. This review changes no normative contract or producer branch.

The next local correction should address F15 and F16 together, then fix F17's ownership wording and
update the capacity/closure records. The original F1–F14 list remains useful, but its “design corrected”
labels must not be read as closing these newly identified chain/first-billing gaps. Existing open
SDKs and owner decisions still prevent claiming end-to-end alignment after those local fixes.
