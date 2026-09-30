# Plan: pull the Lifecycle design back onto the seams that already exist

Status: applied to the Lifecycle documents on 2026-09-30 as D-159–D-168 (items 1–11); item 5 of
the sequence, the Workflow mirror, is not done and lives in the Workflow worktree. Scope: the places where the current Lifecycle documents (working tree
on `d4b95a1a8` plus the uncommitted PriceBook remediation, D-150–D-158 / ADR-0008) diverge from a
seam that is **already built or already decided** on `diffora/bss/products` at `16705a243`, or in
the sibling Workflow design at `e0c24ba50`. Where the design invents a new contract although an
existing one can carry the need, the fix is to adopt the existing seam and shrink the ask to the
genuine residual. Where nothing exists, the ask stays, but its DTOs are aligned to the existing
vocabulary so a producer can implement it without translation.

This plan changes no normative text by itself. Each item names the existing seam with its source,
the diverging Lifecycle text, the fix, and the files to edit. Items are ordered so that stored
shapes change first and registers last.

Existing seams used as the reference:

| Seam | Source on `bss/products` |
|---|---|
| Pricing read contract | `pricing/pricing/src/api/rest/read_contract.rs`, `read_contract/dto.rs`, `domain/resolve.rs`, `docs/design/07-read-contract-events.md`, D-419–D-425 |
| Pricing plan read | `docs/design/04-plans.md` (`GET /plans/{id}`: `published_rev`, revision `state`, `available_from`) |
| Pricing consumer access | D-424 (system subjects `bss-rating.system`, `bss-subscriptions.system`; `plan:read`, `price:read`) |
| Pricing SKU references | DECISIONS line 304 (references of published/superseded revisions held until Subscriptions reports no pins), D-410 (plan retirement deferred) |
| Products SDK | `products-sdk/src/{api,models,references}.rs` (`Sku.sellable`, `Lifecycle`, `ReferenceRegistryV1` for owner `pricing` only) |
| Subscriptions | `SEAMS.md` §I SUB-O1–O6, SUB-G1, SUB-P8; SUB-D-29; `design/01-foundation-lifecycle.md` (create key, OSS-async activate, three instants, void, closed cancel reasons) |
| Rating | T-D-36/37/38, D-415 (no quote), `design/11-consumer-contracts.md` (per-line evaluate, quote-time context) |
| Approval | `gears/bss/libs/approval` (`Engine`, `ApprovalSubject`, `Store`), used by Pricing and Products |
| Ledger | `ledger-sdk/src/api.rs` `LedgerClientV1` |
| Workflow sibling | `orders-workflow/docs` at `e0c24ba50` |

## 1. Pin shape: adopt the resolve DTO nesting (F15)

**Existing seam.** `PricingResolveItemDto.chains: Vec<PricingResolveChainDto>` (dto.rs:66), each
chain `{dim_value, uncovered, binding?}` (dto.rs:89–94), binding `{price_id, dim_used, pinned_from,
price, min_fee, eligibility, effective_from, effective_to, temporary_until, ends_on, keep_for_bound}`
(dto.rs:98–119). SUB-D-29 stores one binding per `(item, chain)` per period.

**Diverging text.** `DESIGN.md` §4.3 stored shape: `items[].selected_dim_value`, `items[].price_id`,
one `items[].binding` (lines 5525–5528); `selected_items[].selected_dim_value` (line 5501).

**Fix.**
- Replace the singular members with `items[].chains[]`, each slot `{dim_value, outcome: bound |
  uncovered, binding?}` using the DTO's field names verbatim. Keep `selected_dim_value` as the
  customer's choice at item scope; it is not a chain field.
- One slot per `(line_id, item_id, dim_value)`; null `dim_value` is the default slot. Quantity and
  `included_qty` stay at item scope and are never multiplied by chain count.
- Acceptance rule: a consumed charged item must have every selected chain bound; the remaining
  slots of the matrix are stored as answered, uncovered ones included, so that activation and the
  first period fact can carry the same matrix. Unselected optional items and unpriced included
  items produce no slots.
- Pin adapter unchanged: own-chain binding goes back as the bare `price_id`; default fallback for
  value `v` goes back as `price_id:v` (resolve.rs:207–210 refuses the other encoding as
  `PIN_FOREIGN`). Deduplicate identical provider pins per request.
- Capacity: keep 200 lines and 200 consumed items; add a working cap of 1,000 chain slots per
  order (bound and uncovered, before pin deduplication) because `MAX_PINS = 1_000`
  (resolve.rs:27) and a single item's matrix is not splittable by `item_id`. Overflow is
  `purchase-capacity-exceeded`, never truncation.

**Edit.** `DESIGN.md` §4.3 (stored shape, own/default pins, capacity); `features/02-capture.md`
(line composition), `features/03-gate-and-pin.md` §3.1/§3.2, `features/04-versioning.md` (what an
amendment re-assesses), `features/08-read-and-authz.md` (`OrderVersionView` pins); new decision
after D-158; `docs/reviews/fixtures/orders-lifecycle-capacity.py` gains a chain-slot case.

## 2. Pricing access: follow D-424, do not invent a seller parameter

**Existing seam.** Resolve scopes the revision to `ctx.subject_tenant_id()` (read_contract.rs:374)
and demands `plan:read` through `PolicyEnforcer` (read_contract.rs:183–189); the pinned price read
demands `price:read`. D-424: Rating and Subscriptions call resolve as system subjects listed in
Pricing PRD §2.2, and resolve reads SKU versions itself as Pricing's actor, so a consumer needs no
Products grant. The Products registry admits only owner `pricing` with `PRICING_SYSTEM_ACTOR`
(reference_registry.rs:56–62); that door is not for consumers.

**Diverging text.** `UPSTREAM_REQS.md` §2.2 `…-upreq-pricing-catalog-tenant-reads` asks for an
"explicit seller/catalog tenant" as an input of a new SDK; `DESIGN.md` §4.1 (line 5458) proposes
`PricingPurchaseV1::assess` with its own seller scope; §3.5 says "Proposed seller-scoped owning SDK".

**Fix.**
- Ask for a `bss-orders.system` system subject in Pricing PRD §2.2 with `plan:read` and
  `price:read`, exactly as the two existing consumers. The Orders adapter builds the system
  context for the **seller tenant of the order** and calls the read as that subject; the seller
  scope is an adapter input, not a Pricing API parameter.
- Keep the buyer/payer/resource axes on the Orders side. Cross-tenant authority is the PDP grant
  on the system subject, as it is for Rating and Subscriptions; no delegation proof is presented
  to Pricing.
- Configure the subject the way the scheduler's `system` actor is configured (01 §3.7, D-115):
  fail closed when missing.

**Edit.** `UPSTREAM_REQS.md` §2.2 (rewrite the tenant-reads requirement around the D-424 pattern);
`DESIGN.md` §3.5 and gate external dependencies (line 5294); `DESIGN.md` 08 §3.5 (service identity
roster); `DECISIONS.md` note under D-151.

## 3. Read contract: consume `PricingReadV1` over the existing reads, keep only the residual verdict as an ask

**Existing seam.** Three reads exist and are golden-tested: `GET /resolve` (revision facts,
`state` published|superseded, book, currency, `currency_minor_digits`, `rounding_policy`, items with
`treatment`, `qty_min`, `included_qty`, `sku_version`, invoice inputs, meter, chains),
`GET /prices/{id}`, `GET /plans/{id}` (`published_rev`, revision `state`, `available_from`).
Resolve answers an uncovered chain as data (D-420) and accepts a superseded revision
(read_contract.rs:421). `available_from` null means "at publish" (plan.rs:42, 152). The Atlas's
step 1 is the same trait over these DTOs; Subscriptions and Rating need it too.

**Diverging text.** `DESIGN.md` §4.1 (lines 5456–5480) and `UPSTREAM_REQS.md`
`…-upreq-pricing-purchase-assessment` make one new operation, `assess`, the owner of every predicate,
so nothing is evaluable until a producer designs a contract that does not exist.

**Fix.** Split the predicate set by source:

| Predicate | Source | Status |
|---|---|---|
| Revision exists, belongs to the seller, is `published` (not `superseded`) | `GET /plans/{id}.published_rev == line.plan_revision_id`, resolve `state` | Existing read |
| Available on the assessment date | `available_from` (null = published) | Existing read |
| Item membership, treatment, `qty_min`, included allowance | resolve `items[]` | Existing read |
| Selected chain coverage | resolve `chains[].uncovered == false` for every consumed selected chain | Existing read |
| SKU version and descriptors as of date | resolve `sku_version`, invoice inputs, meter | Existing read |
| SKU sellable and lifecycle | `Sku.sellable`, `Sku.lifecycle` (products-sdk models.rs) via `ProductsClient::get_sku`, or a `sellable`/`lifecycle` echo in resolve | Existing read; the echo is a small ask |
| Market applicability of a selected dimension value | none | Residual ask (Atlas decision 1) |
| Purchase eligibility beyond the above (owner policy) | none | Residual ask (`SellabilityV1`) |
| Activation deadline / initial acceptance evidence | none | Residual ask (item 4) |

- Consume the first six through `PricingReadV1 { resolve, price, current_revision }` with the DTO
  field names verbatim; register the ask for that trait as the Atlas does, jointly with Rating
  and Subscriptions.
- Keep `catalog-predicate-unevaluable` only for the residual rows. Do not keep the whole gate
  unevaluable because one row has no owner.
- Keep the nine local checks (tenant axes, contract, market consistency, currency, duplicates,
  overlap, capacity, dates, acceptance) where they are.

**Edit.** `DESIGN.md` §4.1 (replace the prose with the table and the trait shape);
`UPSTREAM_REQS.md` §2.2 (split `…-pricing-purchase-assessment` into `…-pricing-read-sdk` (trait over
the three reads) and a narrowed `…-pricing-purchase-verdict` (residual rows)); `features/03` §3.1;
`DECISIONS.md` D-151 amendment.

## 4. Initial price: verify by comparison against the existing signup resolve, do not require a new resolve mode (F1/F16)

**Existing seam.** A signup sends no pins and binds the price in force on the date (D-420 rule 1).
Any pin sent goes through `renewal` (resolve.rs:288) and can walk to an `all` successor. Pricing
refuses a price whose window starts in the past (`WINDOW_START_IN_PAST`), so the same revision,
date and pins give the same bindings once the date has passed. SUB-D-29: Subscriptions resolves at
activation without pins.

**Diverging text.** `DESIGN.md` §4.3 "Initial activation, renewal and expiry" requires "a separate
initial-acceptance mode over the authenticated immutable order version"; `UPSTREAM_REQS.md`
`…-upreq-initial-binding-acceptance` asks Pricing, Products and Subscriptions to jointly supply a
new protocol before any activation can happen.

**Fix.**
- Subscriptions activates exactly as today: signup resolve on the activation date, no pins.
- Before committing `active`, Subscriptions compares the answered matrix with the accepted
  `chains[]` of the order version it reads through `get_version`: equality of `price_id` per
  `(item, dim_value)` slot for every consumed slot. Equal: activate and store those bindings as the
  first period's pins. Different: refuse the activation with a closed reason; Workflow maps it to
  `order-binding-expired` and compensates. No new Pricing mode, no fabricated `pinned_from`.
- The activation deadline stays as the Orders-side early check (submit, and Workflow before
  dispatch), computed by the producer from the earliest `ends_on`/`temporary_until` of the accepted
  matrix; but the authority is the comparison at the receiver, which needs no clock agreement.
- F16 follows: the first period fact carries the pins Subscriptions stored at activation. Rating
  resolving at period start with those pins on a date that has passed yields the same bindings;
  the residual is only Rating resolving ahead of the date, which T-D-37 already forbids. Record
  that as the closing argument for F16 instead of a new binding-read SDK; keep the `ends_on` cut
  owner (Atlas decision 7) as the one open item.

**Edit.** `DESIGN.md` §4.3 (initial activation paragraphs; `activation_deadline` becomes an early
check, `acceptance_evidence` becomes the version reference the receiver compares against);
`UPSTREAM_REQS.md` §2.1 D-157 paragraph and §2.2 initial-binding requirement (rewrite as
"compare-at-activation"); `features/03` §2.3 and §5.3; `features/06` completion semantics;
`docs/reviews/2026-09-29-bss-seam-counterpart-amendments.md` Subscriptions and Pricing sections;
`DECISIONS.md` D-152 amendment; ADR-0008 consequences.

## 5. Overlap key: stay on SUB-G1's registry key, drop "never derived from SKU IDs"

**Existing seam.** SUB-G1 (SEAMS:129): the key is the registry-owned
`catalogSubscriptionProductKey`, "bound to a published SKU/product key"; SUB-O5 (SEAMS:158) follows
SUB-P8's shape: the neighbour submits keys, Subscriptions answers. Products removed the Product
entity but keeps SKUs; resolve names each item's `sku_id` and `charge_kind`.

**Diverging text.** `DESIGN.md` tables at lines 2634 and 2661 and §4.2 line 5033: "Opaque
authoritative key … never derived from plan/SKU IDs"; `UPSTREAM_REQS.md` §2.10 asks an unnamed
"catalog/product-policy owner" for `OverlapPolicyV1::resolve_keys`; D-153 says `plan_id` is not a
default but names no target.

**Fix.**
- Name the target: the key remains SUB-G1's registry SKU key; the PriceBook candidate is the SKU of
  the line's paid `recurring` item(s), read from resolve. Orders still stores the key it is given
  and never computes it, but the ask goes to Subscriptions (owner of the cardinality rule) with
  that derivation as the proposal, not to an undefined owner.
- Keep D-126's count amendment to SUB-O5 and the atomic-enforcement ask unchanged.
- Remove "never derived from plan/SKU IDs" from the two table rows and §4.2; replace with
  "registry-owned, SUB-G1; stored as received".

**Edit.** `DESIGN.md` lines 2634, 2661, 5033 and the surrounding §4.2 text; `UPSTREAM_REQS.md`
§2.10 `…-catalog-subscription-product-key`; `DECISIONS.md` D-153 and Q-05.

## 6. SKU protection: rely on the revision's references, narrow the ask

**Existing seam.** Every item of a published or superseded revision holds a `plan_item` reference
on its SKU, and Pricing keeps it "until Subscriptions reports that no subscription pins the
revision" (Pricing DECISIONS line 304); plan retirement is deferred (D-410); Products refuses
retirement with `SKU_REFERENCED` while a reference is live (retire.rs:57–64). An order can only
name items of a published revision (item 3), so every SKU an accepted order names is protected by
Pricing's own reference for as long as the revision is referenced, which today is forever.

**Diverging text.** `DESIGN.md` §4.3 "Reference protection and activation handoff" (lines
5612–5625) and `UPSTREAM_REQS.md` §2.10 `…-upreq-sku-protection` ask Products for an
Orders-authorized owner/kind, reserve/confirm/release and a transfer protocol; the counterpart
proposal drafts `ReferenceKind::OrderLine`.

**Fix.**
- State that protection is inherited from the accepted revision's `plan_item` references and needs
  no Orders reservation.
- Narrow the ask to the release trigger: when Pricing's release report is implemented, the count of
  "subscriptions that pin the revision" must include in-flight orders that have accepted it (drafts
  before create are invisible to SUB-P8). That is one row in the Subscriptions report or one read
  of Orders' in-flight claims, not a registry change.
- Forced retirement remains an administrative override to document, not to prevent.

**Edit.** `DESIGN.md` §4.3 protection paragraph; `UPSTREAM_REQS.md` §2.10 `…-sku-protection`
(rewrite); `DECISIONS.md` D-157 note; counterpart amendments Products section (drop the enum).

## 7. Totals: keep the Rating ask, align its request to the resolve vocabulary

**Existing seam.** No quote anywhere (D-415, D-419); Rating rates per line and leaves order
summation to the caller (T-D-36, `design/11`); periods are `month | year`; one-time charges are not
rated and are billed at activation (T-D-18/36); the minimum-fee floor is Rating's (T-D-38).

**Diverging text.** `DESIGN.md` §4.4 mentions quarterly cycles; `UPSTREAM_REQS.md` §2.2 Rating
requirements describe the request in Orders terms (selected bindings, descriptor provenance).

**Fix.** The whole-order figure and TCV stay a Rating ask (nothing existing supplies them). Align
the request to `lines[{line_id, plan_revision_id, items[{item_id, quantity, chains[]}]}]` with the
resolve DTO's names so Rating can rate from the same object Subscriptions will store. Drop the
quarterly example; state `month | year` per PriceBook. Keep fail-closed submit.

**Edit.** `DESIGN.md` §4.4 (line 5642–5680); `UPSTREAM_REQS.md` `…-rating-evaluation`,
`…-tcv-with-annualisation`.

## 8. Subscriptions handoff: map to the existing instants and the OSS-async activate

**Existing seam.** Create is deduplicated on `(orderingTenantId, create, client key)`
(01-foundation-lifecycle.md:129–136). Activate is a `TransitionRequest` that commits its intent and
completes on the OSS confirmation (`pending → approved → applied`, `oss_unconfirmed` on timeout,
01:253, 394). Three instants: `contractEffectiveAt`, `serviceActivatedAt`, `customerAcceptedAt`
(01:384–391). Void from draft is not resource-affecting (SUB-O3). Cancel reasons are a closed enum
(SUB-O1).

**Diverging text.** `UPSTREAM_REQS.md` §2.1 D-157 paragraph leaves the acceptance mapping
undefined ("not an implicit timestamp copy"); `DESIGN.md` line 914 defines a completed line as
`activated` without saying which Subscriptions state that is.

**Fix.**
- Mapping: Lifecycle acceptance instant → `customerAcceptedAt` with the Lifecycle acceptance
  record as provenance; actual activation → `serviceActivatedAt` (SUB-O10); `contractEffectiveAt`
  from the referenced Contract, never from an order date.
- `activated` in `FulfillmentAcknowledgement` means the subscription's transition reached
  `applied`; `approved` (intent accepted, OSS pending) is not completion. `oss_unconfirmed` is a
  provisioning failure on Workflow's existing path.
- Keep SUB-O1/O2/O5/O9/O10 as they are. Reference the Workflow PRD's "a compensation that cannot
  complete escalates" (Workflow PRD line 455) and state that the order stays `in_fulfillment`,
  expiry-exempt, until evidence with `no_active_subscription_remains = true` exists; no third leg.

**Edit.** `UPSTREAM_REQS.md` §2.1 (D-157 paragraph); `DESIGN.md` Workflow SDK contract (line
914) and 06 §4.2; `features/06` completion and failure contracts.

## 9. Approval: name the library, not a "Generic Approval service"

**Existing seam.** `cf-gears-bss-approval` (`Engine`, `ApprovalSubject<R>`, `Store<R>`) is built and
embedded by Pricing and Products inside their own transactions. No approval service exists;
upstream's `gears/approval-service` PRD is a stub.

**Diverging text.** `PRD.md` lines 124, 239, 472, 1062, 1070 and `DESIGN.md` §3.5 row "Generic
Approval service … Unspecified today".

**Fix.** Rename the dependency to "the approval policy owner: Workflow's approval adapter, which
may embed `cf-gears-bss-approval` as Pricing and Products do". Lifecycle's role (reflect a verdict
with its authority) is unchanged. Record Atlas decision 6/13 as the open host question.

**Edit.** `PRD.md` glossary and §13; `DESIGN.md` §3.5 row; `UPSTREAM_REQS.md` §2.6 already says
this and stays.

## 10. Billing chain: separate Ledger from invoicing, tax and Payments (F17)

**Existing seam.** `LedgerClientV1` posts settled money (`settle_payment`, `allocate_payment`,
`return_payment`, `record_dispute_phase`). Nothing generates invoices, values at-sale facts, or
answers tax.

**Diverging text.** `UPSTREAM_REQS.md` §4: "the billing chain is specified as `gears/bss/ledger`,
so both asks must be raised against those specifications".

**Fix.** Four rows: invoicing/at-sale valuation (unowned), indicative tax (unowned), payment
authorization/PSP (unowned), Ledger (existing, accounting only). External references travel
order → Workflow create → Subscriptions billable fact → invoice, snapshotted at first handoff.

**Edit.** `UPSTREAM_REQS.md` §2.3 and §4; `PRD.md` §13 row for the billing chain.

## 11. Registers and validation

- `DECISIONS.md`: one entry per item above after D-158 (D-159–D-168), each citing the existing
  seam it adopts; annotate D-151/D-152/D-153/D-157 as amended.
- ADR-0008: amend "Consequences" (compare-at-activation replaces the initial-acceptance mode;
  protection inherited; key stays SUB-G1's).
- `UPSTREAM_REQS.md` §3 priorities and the "PriceBook readiness additions" list: remove the IDs
  that no longer exist, add the split ones.
- `DECOMPOSITION.md` §3.1: the release prerequisites shrink to `PricingReadV1`, the `bss-orders.system`
  grant, the Rating SDK, SUB-O1/O2/O5/O9/O10, the residual purchase verdict, and the Workflow
  mirror.
- Counterpart amendments review: update the Pricing, Products and Subscriptions sections and the
  closure register (F1, F3, F5, F6, F12, F15, F16, F17 rows).
- Validation: relative links, `git diff --check`, `cfs validate` on PRD, UPSTREAM_REQS, ADR-0003,
  ADR-0008 against the recorded baseline; rerun the capacity fixture with the chain-slot case.

## Sequence

1. Item 1 (stored shape) and item 3 (predicate table) together: they define the object every
   later item refers to.
2. Item 2 (access) and item 4 (compare-at-activation), which depend on the shape.
3. Items 5, 6, 7, 8, 9, 10: independent wording changes.
4. Item 11: registers, ADR, decomposition, reviews, validation.
5. Separately, in the Workflow worktree: mirror items 1, 4 and 8 (bindings passed to create,
   deadline check before dispatch, `applied` as completion, refusal mapping to
   `order-binding-expired`), and replace the catalog topology source.

What this plan does not do: it does not make the ten Atlas decisions. After it, the ones still
needed before order taking are decision 1 (residual purchase verdict owner), decision 4 (the
`bss-orders.system` grant), decision 5 (Rating totals), decision 7 (`ends_on` cut owner), decision 8
(Payments) and decision 9 (line dependencies). Decisions 2, 3, 6 and 10 are answered by items 5, 4,
8 and 6 respectively, pending counterpart agreement.
