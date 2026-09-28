# Entity registration scenarios

End-to-end registration scenarios cover HTTP submission, outbox admission, persisted outcomes and reads. The local launcher uses **SQLite**; these tests do not prove PostgreSQL/MySQL or tenant/PDP behavior.

TR-REG-001–003 and TR-REG-101–102 have tests and fixture files today. The later IDs
specify planned e2e tests; their fixture files will be added with those tests. Each
scenario describes its starting entities, requests, terminal item outcomes and
observable reads. Separate scenarios may share one pytest setup or test function.

Unless a scenario says otherwise, each mutation uses a fresh `Idempotency-Key`.
An accepted mutation returns a JSON receipt with `202` and a `Location` for
`GET /operations/{operation_id}`. Polling reaches `completed`; that operation
status reports progress, while its items report success or failure. A synchronous
refusal returns a Problem response without an operation `Location` and admits no
batch item. Reads mentioned below use an explicit selection for the fields asserted.
All fixture IDs belong to a fresh namespace, except the deliberately disallowed
vendor in TR-REG-901.

Valid root Type Schema fixtures use the GTS §4.4.1 closed-envelope pattern: the top level rejects undeclared properties and `payload` is the explicit open extension point. The intentionally unresolved derived schema inherits no known root constraints and is not closed locally. Instances carry `payload`, even when it is empty.

## Successful registration

### TR-REG-001 — Create a Type Schema

**Given:** a new dependency-free [person schema](../fixtures/registration/person_schema.json).

**When:** submit this item alone and await completion.

**Then:**

1. The item succeeds at resource version 1.
2. Reading by GTS ID returns the submitted Type Schema and materialized traits.
3. Reading by `gts_uuid` returns the same body and timestamps.

### TR-REG-002 — Register an Instance in a later operation

**Given:** a [schema](../fixtures/registration/person_schema.json) and conforming [Instance](../fixtures/registration/person_instance.json).

**When:**

1. Submit and complete the schema operation.
2. Submit and complete the Instance under a new key.

**Then:** the Instance succeeds at version 1 and reads back with its exact content; schema-only artifacts are absent, not `null`.

### TR-REG-003 — Register an Instance before its schema in one batch

**Given:** [person_schema](../fixtures/registration/person_schema.json) and its conforming [person_instance](../fixtures/registration/person_instance.json) are both absent from a fresh namespace.

**When:** submit [person_instance](../fixtures/registration/person_instance.json) before [person_schema](../fixtures/registration/person_schema.json) in one batch.

**Then:** both succeed at version 1 and read back with the expected kind and content.

This checks batch-level ordering, not every graph-ordering case.

## Partial success and refusals

### TR-REG-101 — Preserve partial success and structured failures

**Given:**

- A: valid, independent [person schema](../fixtures/registration/person_schema.json).
- B: [missing_ref_schema.json](../fixtures/registration/missing_ref_schema.json), referencing absent `absent.v1~`.
- C: [blocked_instance.json](../fixtures/registration/blocked_instance.json), an Instance conforming to B.

**When:** submit exactly `[C, B, A]` in one request and await completion.

**Then:**

| Item | Status | Resource version | Error reason |
|---|---|---|---|
| A | succeeded | 1 | No error |
| B | failed | null | dependency_not_found |
| C | failed | null | blocked_by_dependency |

B also reports `dependency_kind=ref` and the missing ID. A is readable; B and C return RFC-9457 `404` responses.

### TR-REG-102 — Refuse a Type Schema whose `$id` does not name its item

**Given:** the [person schema](../fixtures/registration/person_schema.json) and its conforming [Instance](../fixtures/registration/person_instance.json), both absent, with the schema's `content.$id` replaced by one of: absent, a non-string, a malformed URI, or the `gts://` URI of another Type Schema.

**When:** submit [person_instance](../fixtures/registration/person_instance.json) before [person_schema](../fixtures/registration/person_schema.json) in one request.

**Then:**

1. The request is refused synchronously with `400` RFC-9457 `invalid_argument` naming the schema's `gts_id` as `resource_name`, with one field violation on `entity`, reason `VALIDATION_FAILED`, whose description names the expected `gts://<gts_id>`.
2. No `202`, operation or `Location` is returned, so nothing is admitted.
3. Neither the schema nor its valid batch neighbour, the Instance, is readable; both return `404`.

## Request identity and idempotency

### TR-REG-201 — Replay returns the original operation after the entity changes

**Given:** the [person schema](../fixtures/registration/person_schema.json) was
created under key K1. Save its `operation_id`, terminal operation body and
receipt `Location`. Revise that schema under K2, and confirm its current
`resource_version` is 2.

**When:** send the byte-for-byte same creation request again under K1, then
follow the returned `Location`.

**Then:**

1. The replay responds with `200` and a receipt, not an inline operation body.
   It has the original `operation_id`, `status=completed` and `replayed=true`;
   `Idempotency-Replayed: true` and `Location` are present, while `Retry-After`
   is absent.
2. Polling returns the original completed operation and its version-1 creation
   result, even though the entity is now at version 2.
3. Reading the entity still returns the revised version-2 content. Replay
   creates no new revision.

### TR-REG-202 — Reusing a key for a different request is a conflict

**Given:** K1 completed a successful creation of the [person schema](../fixtures/registration/person_schema.json)
at version 1. Prepare a request for the same ID with different authored content.

**When:** submit that different request under K1.

**Then:** the response is synchronous `409` RFC-9457 `already_exists`, naming
the original operation as the conflicting resource. It has no new operation
`Location`. Polling the original ID still yields the original outcome, and
reading the schema still returns its version-1 content.

### TR-REG-203 — A new key does not turn duplicate creation into replay

**Given:** the [person schema](../fixtures/registration/person_schema.json)
was created at version 1 under K1.

**When:** submit the identical creation candidate without
`expected_resource_version` under a new key K2 and await completion.

**Then:** K2 receives its own `202` receipt and completed operation, but its
item has `status=failed`, `resource_version=null` and
`error.reason=already_exists`. The existing schema remains at version 1 with
the same content and timestamps. This differs from both K1 replay
(TR-REG-201) and an update with a matching precondition (TR-REG-303).

## Content revisions and optimistic preconditions

### TR-REG-301 — Add an optional property at a closed schema level

**Given:** the [person schema](../fixtures/registration/person_schema.json)
is active at version 1. Its root rejects undeclared properties. Prepare the
same authored schema with an optional string `nickname` property at that root;
leave existing constraints intact.

**When:** submit the revised schema with `expected_resource_version=1` and
await completion.

**Then:** the item `succeeded` at resource version 2. Exact read returns the
new authored property; GTS ID, deterministic `gts_uuid`, lifecycle and
`created_at` remain the same. This is the compatible side of TR-REG-501.

### TR-REG-302 — Revise an Instance value without revising its Type Schema

**Given:** the [person schema](../fixtures/registration/person_schema.json)
and [person Instance](../fixtures/registration/person_instance.json) are
registered at version 1. Prepare the same Instance ID with `name` changed
from `Alice` to `Alicia`.

**When:** submit the Instance with `expected_resource_version=1` and await
completion.

**Then:** the Instance item `succeeded` at version 2 and exact read returns
`Alicia` under the same GTS ID and Registry Reference. The Type Schema
remains at version 1 with unchanged authored content.

### TR-REG-303 — Equal current Instance content is unchanged

**Given:** the [person schema](../fixtures/registration/person_schema.json)
and [person Instance](../fixtures/registration/person_instance.json) are
registered. Retain the Instance's version-1 content and `origin` timestamps.

**When:** submit the same Instance ID and authored content under a new key
with `expected_resource_version=1`.

**Then:** the operation item is `unchanged` with
`resource_version=1` and no error. Exact read returns the same content,
version and `updated_at`. The new operation does not create a content
revision.

### TR-REG-304 — A stale writer reads again and retries

**Given:** two callers A and B read the [person schema](../fixtures/registration/person_schema.json)
at version 1 and independently prepare different compatible title revisions.

**When:**

1. B submits with `expected_resource_version=1` and completes at version 2.
2. A submits its older version-1 precondition under another key, reads again
   after refusal, and resubmits its intended change under a third key with
   `expected_resource_version=2`.

**Then:** A's stale request is accepted with `202` but its item finishes
`failed`, `resource_version=null` and `error.reason=precondition_failed`;
there is no HTTP `412` and B's content remains current. A's reconciled
request succeeds at version 3, which exact read returns.

### TR-REG-305 — Registration cannot reuse a tombstoned name

**Given:** register an isolated [person schema](../fixtures/registration/person_schema.json),
then complete its deletion with `expected_resource_version=1`. Exact read
returns its tombstone at version 2.

**When:** under separate new keys, submit the same ID once as a creation
(without `expected_resource_version`) and once as a content revision with
`expected_resource_version=2`.

**Then:** both requests receive `202` and terminal registration operations.
Creation fails with `already_exists`; revision fails with `entity_deleted`.
Neither changes the readable version-2 tombstone, its content or its
Registry Reference.

## Dependency graph and partial admission

### TR-REG-401 — Register a derived Type Schema before its base

**Given:** a new base Type Schema with a closed root and open `payload`
extension point, plus a valid derived Type Schema whose GTS ID names that
base and whose own constraint requires `payload.employee_id`. Neither is
registered.

**When:** submit `[derived, base]` in one batch and await completion.

**Then:** both items `succeeded` at version 1. Exact reads return each
authored document; the derived read's `resolved_schema` includes the base's
constraints and the derived `employee_id` constraint. Request order did not
require a separate prerequisite operation.

### TR-REG-402 — Resolve an in-batch `$ref` target submitted later

**Given:** two new, independently named Type Schemas: a valid target and a
referrer whose authored `$ref` names that target. The target is absent before
the request.

**When:** submit `[referrer, target]` in one batch.

**Then:** both items `succeeded` at version 1. Reading the referrer's
`resolved_schema` shows the target's constraints, and both authored
documents retain their exact `$ref` and `$id` values.

### TR-REG-403 — Reconcile an Instance after its missing schema arrives

**Given:** the [person Instance](../fixtures/registration/person_instance.json)
and its [person schema](../fixtures/registration/person_schema.json) are
both absent.

**When:**

1. Submit the Instance alone under K1 and await its failure.
2. Submit and complete the schema under K2.
3. Submit the same Instance under K3 and await completion.

**Then:** K1's item is `failed` with `dependency_not_found`,
`dependency_kind=conforming_type` and `dependency_id` equal to the schema
ID; the Instance is initially absent. K2 and K3 each `succeeded` at version
1. The Instance is readable after K3, while polling K1 still reports its
original failure. Separate operations require the caller to await the schema
before retrying the Instance.

### TR-REG-404 — Invalid Instance content does not block a valid batch neighbour

**Given:** the [person schema](../fixtures/registration/person_schema.json)
is registered. Prepare two new Instances of that type: one with valid
`name` and `payload`, and one missing the required `name`.

**When:** submit both Instances in one batch and await completion.

**Then:** the valid item `succeeded` at version 1 and reads back with its
content. The invalid item is `failed` with `error.reason=invalid_value`
and `resource_version=null`; its key returns `404`. The operation as a
whole is `completed` even though an item failed.

### TR-REG-405 — An in-batch failed revision blocks its new referrer

**Given:** a registered base Type Schema at version 1. Prepare an
incompatible revision that removes a property accepted by its current
closed schema, plus a new Type Schema whose `$ref` names that base ID.

**When:** submit `[referrer, base revision]` in one batch with the base's
`expected_resource_version=1`.

**Then:** the base revision is `failed` with
`incompatible_with_baseline`; the referrer is `failed` with
`blocked_by_dependency`. The stored version-1 base remains readable and
unchanged, but the referrer does not fall back to that stored revision and
remains absent.

### TR-REG-406 — A cycle fails beside an independent success

**Given:** new schemas A and B whose `$ref` and/or derivation edges form
one cycle, and new schema C with no dependency on either. All three IDs
are absent.

**When:** submit C, B and A in one batch and await completion.

**Then:** A and B each finish `failed` with `invalid_schema` and no
resource version; both remain absent. C `succeeded` at version 1 and is
readable. `completed` on the operation does not imply that every item
succeeded.

## Compatibility and dependent safety

### TR-REG-501 — Adding a property at an open level is incompatible

**Given:** a registered Type Schema at version 1 whose root is closed but
whose `payload` level is open with `additionalProperties: true`. Prepare
a revision that adds an optional, typed `payload.employee_id` property
while leaving the rest of the schema unchanged.

**When:** submit the revision with `expected_resource_version=1`.

**Then:** its item fails with `incompatible_with_baseline` and
`resource_version=null`. Exact read still returns the version-1 authored
schema and origin. At an open level, the old schema accepted values under
that property name which the new type constraint would reject; compare
with the successful closed-level addition in TR-REG-301.

### TR-REG-502 — A live direct Instance prevents an abstract transition

**Given:** a concrete Type Schema with `x-gts-abstract=false` and one
active, directly conforming Instance. Retain the schema's version-1 body
and the Instance's body.

**When:** revise only `x-gts-abstract` to `true` under
`expected_resource_version=1`.

**Then:** the schema item fails with `dependent_invalid` and no resource
version. The schema remains concrete at version 1; the Instance remains
active and unchanged.

### TR-REG-503 — A live derived Type Schema prevents a final transition

**Given:** a base Type Schema with `x-gts-final=false` and an active
derived Type Schema whose identifier names it. Retain both version-1
bodies.

**When:** revise the base to `x-gts-final=true` with
`expected_resource_version=1`.

**Then:** the base item fails with `dependent_invalid`; both schemas
remain at version 1 with their prior authored and effective content.
The refusal comes from the existing dependant even though the authored
change is otherwise only an annotation.

### TR-REG-504 — Publish a breaking contract under a new major

**Given:** a registered `person.v1~` Type Schema, optionally with a
conforming v1 Instance. Prepare `person.v2~` in the same version family
with an intentionally incompatible accepted-value set and its own
matching `$id`.

**When:** submit v2 as a creation without
`expected_resource_version`.

**Then:** v2 `succeeded` at version 1 with a different GTS ID and
Registry Reference. Exact reads of v1 and v2 return their respective
content; the v1 Instance still conforms to the exact v1 identifier.
No compatibility verdict is required across different majors.

## Minor versions and version-family shape

### TR-REG-601 — Order contiguous minors within one batch

**Given:** new compatible Type Schemas `person.v1.0~` and
`person.v1.1~` in one family, each with its own matching `$id`.
Neither minor exists.

**When:** submit `[v1.1, v1.0]` in one batch.

**Then:** both items `succeeded` at their own resource version 1.
Exact reads find both distinct GTS IDs and Registry References; neither
is synthesized as the major-only `person.v1~` identity.

### TR-REG-602 — Refuse a minor with a missing predecessor

**Given:** `person.v1.0~` is registered, while `person.v1.1~` is absent.
Prepare a compatible `person.v1.2~` candidate.

**When:** submit v1.2 alone and await completion.

**Then:** its item is `failed` with `missing_predecessor` and
`resource_version=null`. V1.2 remains absent; v1.0 remains readable.
The registry does not skip the absent v1.1 baseline.

### TR-REG-603 — One major cannot mix major-only and minor-bearing shapes

**Given:** two fresh families A and B. A has a registered major-only
`v1~`. B has a registered first minor `v1.0~`. Neither family has the
opposite shape.

**When:** submit A's `v1.0~` and B's `v1~` as creations, using separate
operations or independent batch items.

**Then:** each candidate fails with `family_shape_conflict` and no
resource version. Reads still find A's major-only and B's first minor,
and do not find either rejected ID.

### TR-REG-604 — Refuse a content revision of a minor-bearing schema

**Given:** `person.v1.0~` is registered at resource version 1.
Prepare changed content for that same ID.

**When:** submit it with `expected_resource_version=1`.

**Then:** acceptance refuses the request synchronously with `400`
RFC-9457 `invalid_argument` and a field violation for
`expected_resource_version`. There is no `202` or operation `Location`;
exact read still returns the original version-1 document. The refusal
does not depend on whether the named minor currently exists.

### TR-REG-605 — A failed predecessor blocks the next minor

**Given:** both `person.v1.0~` and `person.v1.1~` are absent.
The v1.0 candidate refers to a missing schema; v1.1 would otherwise be
compatible with a valid predecessor.

**When:** submit `[v1.1, v1.0]` in one batch.

**Then:** v1.0 fails with `dependency_not_found`; v1.1 fails with
`blocked_by_predecessor`. Neither ID becomes readable. This reason
differs from the `blocked_by_dependency` of a failed authored `$ref` or
conforming-type edge.

## Dry-run admission

### TR-REG-701 — Predict creation, then commit under a new key

**Given:** a new [person schema](../fixtures/registration/person_schema.json)
and its conforming [person Instance](../fixtures/registration/person_instance.json).

**When:**

1. Submit `[instance, schema]` with `dry_run=true` under K1 and poll.
2. Confirm that neither ID is readable.
3. Submit the same items with `dry_run=false` under K2 and poll.

**Then:** the dry-run operation has `dry_run=true` and both items
`succeeded` with `resource_version=null`; it reserves neither ID.
The commit operation has `dry_run=false` and both items `succeeded`
at version 1; both are then readable. K2 must differ from K1 because
the mode participates in the idempotency fingerprint.

### TR-REG-702 — A mixed dry run predicts the committed item verdicts

**Given:** a batch like TR-REG-101: one independent valid schema A, one
schema B with a missing `$ref`, and one Instance C conforming to B.
All IDs are absent.

**When:** submit `[C, B, A]` as a dry run under K1, verify that no ID
was written, then commit the same batch under K2 without an intervening
entity mutation.

**Then:** both completed operations agree per GTS ID on item `status`
and failure `reason`: A succeeds, B fails `dependency_not_found`,
and C fails `blocked_by_dependency`. Dry-run A has no resource version;
committed A has version 1 and is readable. B and C stay absent in
both modes. The comparison does not require equal operation IDs,
timestamps or successful-item versions.

## Managed schema dialect and unstable major-zero profile

### TR-REG-801 — Refuse an inadmissible schema dialect before admission

**Given:** an absent [person schema](../fixtures/registration/person_schema.json)
and its valid [Instance](../fixtures/registration/person_instance.json).
Replace the schema's top-level `$schema` with one of: absent, a
non-Draft-07 dialect, or a Draft-07 root with a divergent nested
`$schema`.

**When:** submit `[instance, schema]` in one request.

**Then:** the entire request receives synchronous `400` RFC-9457
`invalid_argument` and no operation `Location`. Neither ID is
readable. The registry does not admit the valid batch neighbour and
does not infer a default dialect.

### TR-REG-802 — Major zero allows breaking revisions but no registered Instance

**Given:** a valid Type Schema `person.v0~` is registered at version 1.
Prepare a revision whose accepted-value set is incompatible with the
first definition. Also prepare a new conforming Instance whose **own
last segment** is stable `v1`, so its only unstable marker is the
conforming Type Schema's `v0~` segment.

**When:** submit the schema revision with
`expected_resource_version=1`, then submit the Instance under a new key.

**Then:** the breaking v0 revision `succeeded` at version 2 and is
readable. The Instance request receives `202`, but its item finishes
`failed` with `instance_of_major_zero` and no resource version; the
Instance remains absent. This is distinct from the synchronous
identifier refusal for an Instance whose own last segment is v0.

### TR-REG-803 — Stable schemas cannot derive from or `$ref` major zero

**Given:** a registered `base.v0~` Type Schema. Prepare two new stable
`v1~` schemas: one derives directly from that base, and the other
contains an authored `$ref` to it.

**When:** submit both candidates in a registration batch and poll.

**Then:** acceptance returns `202`. The derived candidate finishes
`failed` with `stable_derives_from_major_zero`; the referrer finishes
`failed` with `stable_refs_major_zero`. Neither is readable. These are
worker item outcomes in the current API, not synchronous HTTP
refusals.

## Deployment policy and compatibility waiver

### TR-REG-901 — A closed region refuses a non-platform vendor

**Given:** a new valid Type Schema whose GTS ID has a last-segment
vendor other than `cf`, such as an `acme` root Type Schema, and a
matching authored `$id`. The local launcher has the default closed
`registration_policy`.

**When:** submit it as a creation.

**Then:** acceptance refuses it synchronously with `400` RFC-9457
`failed_precondition`, identifying the closed region and
`allowed_vendors` parameter. There is no operation `Location` and
the ID is not readable. A `cf`-vendor ID would not exercise this
policy gate.

### TR-REG-902 — Disabled `force` is a synchronous refusal

**Given:** a registered first minor `person.v1.0~` and a candidate
`person.v1.1~` carrying `force=true`. The local launcher keeps
`allow_compatibility_force=false`.

**When:** submit the candidate, including a `dry_run=true` variant.

**Then:** each request is refused before `202` with `400` RFC-9457
`invalid_argument` and a `force` field violation. Neither creates
an operation or v1.1 entity.

### TR-REG-903 — Enabled `force` records a waived minor transition

**Given:** a separate launcher profile with
`allow_compatibility_force=true`. A stable `person.v1.0~` schema is
registered. Its `person.v1.1~` candidate is incompatible with v1.0
but carries `force=true`.

**When:** submit v1.1 and await completion, then read both minors with
`$select=content,provenance`.

**Then:** v1.1 `succeeded` at version 1, with its incompatible content
and `provenance.compat_forced=true`. V1.0 remains unchanged and reports
`compat_forced=false`. The waiver belongs only to the cross-minor
comparison and does not revise v1.0. This scenario needs an explicit
force-enabled e2e profile; it does not run under the default launcher.
