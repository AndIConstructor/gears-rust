# PriceBook seam remediation: validation record

Date: 2026-09-29. Scope: documentation and proposed contracts, against the source revisions pinned in
[the review](2026-09-29-orders-lifecycle-seam-review.md). No Rust runtime, producer SDK, remote branch
or deployment changed. This is not end-to-end integration verification.

## Delivered

P0–P6 design changes are drafted in Lifecycle PRD/DESIGN, all eight feature contracts, decisions,
upstream requirements, decomposition and ADR-0008. The
[counterpart amendment and F1–F14 closure register](2026-09-29-bss-seam-counterpart-amendments.md)
separates Lifecycle corrections from owner agreement and implementation. The
[updated explainer](2026-09-29-orders-lifecycle-explainer.md) describes the same target.

## Static checks

- `git diff --check`: passed.
- Relative Markdown file/heading links and local source links with line suffixes: no newly broken
  targets in the changed/new documents. Historical review line numbers identify the original review
  baseline; they are not updated to imply the findings still exist at those lines after remediation.
- CFS 1.5.9: `cfs validate --artifact <path> --skip-code --output <json>` run on changed registered
  artifacts; tables of contents regenerated for changed headings. PRD, UPSTREAM_REQS and ADR-0003
  and ADR-0008 pass. DECISIONS is not a registered artifact, so its links were checked separately.
- Existing CFS failures were compared with `HEAD` copies in a temporary registered artifact tree,
  removed after validation. The modified existing artifacts introduce no new error identities.
  CFS does **not** pass globally:

| Artifact | Current errors | Baseline errors | Class |
|---|---:|---:|---|
| DESIGN | 146 | 146 | Existing definition heading placement |
| DECOMPOSITION | 6 | 15 | Existing task-definition references; nine removed by readiness rewrite |
| Feature 01 | 5 | 5 | Existing heading placement / ID-kind constraints |
| Feature 02 | 4 | 4 | Existing ID-kind constraints |
| Feature 03 | 3 | 3 | Existing ID-kind constraints |
| Features 04, 05, 06 | 2 each | 2 each | Existing required headings |
| Features 07, 08 | 3 each | 3 each | Existing required headings |

The new ADR initially lacked a DESIGN reference; that reference was added and ADR validation passes.
Existing template errors were not hidden by changing registry configuration or suppressing checks.

## Serialized capacity examples

Run `python3 docs/reviews/fixtures/orders-lifecycle-capacity.py` from repository root.
This uses synthetic compact UTF-8 JSON with UUIDs, 200 completion mappings and a 2 KiB envelope
reserve. It is **not** the production EventV1 serializer, nor proof of the worst-case wire schema.

| Illustrative input | Serialized bytes | Expected result |
|---|---:|---|
| 200-line completion, 64-byte external references | 41,269 | Fits 64 KiB |
| 200-line completion, 256-byte external references | 79,861 | Must be refused before admission/commit |
| 200-item version, 4,096-byte descriptors | 832,472 | Fits 1 MiB |
| 200-item version, 8,192-byte descriptors | 1,651,672 | Must be refused before admission/commit |

All four assertions pass. The examples demonstrate why count and byte limits must both apply.
The design requires reserving worst-case terminal-event space before admission, preserving that
budget across administrative edits, and validating every event type with the actual serializer.
Concrete wire field limits, consumer compatibility and performance measurements remain release
prerequisites; these illustrative results cannot establish them.

## Contract review and remaining validation

The worked cases in the counterpart proposal cover successor prices, exclusive deadline/replay,
own/default pins, optional and included items, multiple plans, seller authorization, active-count
races, missing approval authority/TCV, explicit completion mapping and partial compensation.
They are design walkthroughs. No running integration or compiled DTO fixtures exist for these
proposed SDKs in this worktree.

P7's available static checks are complete. Its executable producer/consumer conformance, all-event
worst-case sizing and concurrency/replay tests await the owning schemas/SDKs. Joint commercial
choices (duration, overlap policy, retirement, topology and approval-policy ownership) remain open.
The closure register intentionally marks all runtime statuses pending.
