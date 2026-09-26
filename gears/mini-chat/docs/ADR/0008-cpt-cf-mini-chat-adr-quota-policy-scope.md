---
status: accepted
date: 2026-09-26
---

# P1 scope of quota, policy and licensing controls

**ID**: `cpt-cf-mini-chat-adr-quota-policy-scope`

## Context and Problem Statement

PRD §5.4 and §5.6, DESIGN §5 and ADR-0003 specify several cost-control and policy mechanisms beyond the implemented credit quotas, tool quotas, downgrade cascade and kill switches. This ADR records which of them P1 does not provide and how the implementation behaves instead.

## Decision Drivers

* Quota documentation is read by billing and support. It must describe what is enforced.
* No large new features in the P1 alignment.
* Limitations that are harmless with the bundled static policy plugin but risky with a remote CCM plugin must be explicit.

## Considered Options

* Implement the missing mechanisms.
* Record the gaps with status and the conditions for revisiting them.

## Decision Outcome

Chosen option: "Record the gaps with status and the conditions for revisiting them".

| Capability | Requirement / design | Status | Current behaviour |
|---|---|---|---|
| Per-user daily image-input quota (default 50) and `image_inputs` / `image_upload_bytes` counters | `cpt-cf-mini-chat-fr-quota-enforcement`, PRD §5.2, §9 | Not implemented | Only `max_images_per_message` (default 4) is enforced. The counter columns exist in `quota_usage` and stay 0. |
| Per-message image bytes cap (`image_bytes_exceeded`) | PRD §5.4 | Not implemented | The per-file upload size limit applies. |
| PolicySnapshot in-memory cache, DB persistence, `POST /internal/policy:notify` | DESIGN §5.2.3, §5.2.7, §5.2.8, Appendix B.3 | Future | Every preflight and settlement asks the policy plugin for the version and snapshot. Settlement does so inside the finalization transaction. With the bundled in-process static plugin (fixed version 1) this is cheap and cannot fail. A remote CCM plugin needs the cache and a pre-fetched snapshot first. |
| Per-model estimation budgets from the policy snapshot | DESIGN §5.2.1, A.1, B.3 | Not implemented | The preflight reserve uses the gear configuration section `estimation_budgets` for every model. `ModelCatalogEntry.estimation_budgets` is parsed from the snapshot but not read, so budget changes need a configuration change and restart, not a policy version bump. |
| Billing of agentic knowledge-search iterations | DESIGN §4 "Knowledge Search" | Not implemented | Each `search_knowledge` iteration is a provider call, but only the final iteration's usage is settled. The feature is off by default (`knowledge_search.enabled = false`). |
| License gate on the `ai_chat` feature | `cpt-cf-mini-chat-fr-license-gate`, `cpt-cf-mini-chat-constraint-license-gate` | Accepted interim | Routes require the platform base license feature (`CORE_GLOBAL_BASE_LICENSE_FEATURE`) until the license plugin exposes `ai_chat` (TODO in `mini-chat/src/api/rest/routes/mod.rs`). |
| System tasks charged to a tenant operational bucket, audited and subject to kill switches | ADR-0003, DESIGN §3.2 "System Task Attribution Rules" | Future (P2+) | The thread-summary task emits a usage event with `billing_outcome = system_task`, `settlement_method = none`, `actual_credits_micro = 0` and `requester_type = system`. It emits no audit event and does not check kill switches. |

The daily web-search and code-interpreter quotas **are** implemented. They reject only requests that use the tool (`web_search.enabled`, or ready XLSX attachments for code interpreter).

### Consequences

* Good, because billing and support documentation matches enforcement.
* Bad, because the image quota and the system-task billing remain open product gaps.
* Bad, because switching to a remote CCM policy plugin requires implementing the snapshot cache first.

### Confirmation

* Unit tests in `mini-chat/src/domain/service/quota_service.rs` cover tool-quota gating and the downgrade cascade.
* E2E `testing/e2e/suites/mini_chat/test_quota_policy.py` covers 429, premium downgrade, `model_disabled`, the daily web-search and code-interpreter quotas and the quota status flags (usage is seeded per test user). Kill switches are fixed configuration of the E2E rig and are covered by unit tests (`quota_service.rs`, `attachment_service_test.rs`).

## More Information

* Revisit the PolicySnapshot items before any non-static policy plugin is deployed.
* Revisit the license gate when the license plugin exposes `ai_chat`.

## Traceability

* **PRD**: [PRD.md](../PRD.md) §5.2, §5.4, §5.6, §9
* **DESIGN**: [DESIGN.md](../DESIGN.md) §3.2, §5.2, Appendix B

This decision directly addresses the following requirements or design elements:

* `cpt-cf-mini-chat-fr-quota-enforcement`
* `cpt-cf-mini-chat-fr-license-gate`
* `cpt-cf-mini-chat-fr-quota-billing-architecture`
* `cpt-cf-mini-chat-nfr-cost-control`
* `cpt-cf-mini-chat-constraint-license-gate`
* `cpt-cf-mini-chat-adr-group-chat-usage-attribution`
