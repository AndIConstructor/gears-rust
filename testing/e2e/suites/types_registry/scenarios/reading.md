# Exact and batch read scenarios

Each scenario names its inputs and expected result. Registration and deletion in a Given are completed before the read.

## Exact read

### TR-READ-001 — Default read is document-free managed metadata

**Given:** target [person_schema](../fixtures/reading/person_schema.json) is registered alongside its [person_instance](../fixtures/reading/person_instance.json) and the independent [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) pair.

**When:** GET [person_schema](../fixtures/reading/person_schema.json) and [other_schema](../fixtures/reading/other_schema.json) by their distinct GTS IDs without `$select`.

**Then:** both `200` JSON bodies contain exactly `gts_id`, deterministic `gts_uuid`, `kind=type_schema`, `lifecycle_status=active`, and managed `origin` with `resource_version=1` and RFC 3339 `created_at`/`updated_at`; no authored or effective document is returned. Each body identifies its requested schema.

### TR-READ-002 — GTS ID and Registry Reference resolve to the same entity

**Given:** register [person_schema](../fixtures/reading/person_schema.json) with [person_instance](../fixtures/reading/person_instance.json), and [other_schema](../fixtures/reading/other_schema.json) with [other_instance](../fixtures/reading/other_instance.json). All four keys are distinct.

**When:** GET [person_schema](../fixtures/reading/person_schema.json) once by GTS ID and once by its returned `gts_uuid`, then GET [other_schema](../fixtures/reading/other_schema.json) by its GTS ID.

**Then:** the two [person_schema](../fixtures/reading/person_schema.json) bodies are identical, including `origin` and lifecycle. The [other_schema](../fixtures/reading/other_schema.json) body has its own identity and differs.

### TR-READ-003 — Authored content is selected independently

**Given:** registered [person_schema](../fixtures/reading/person_schema.json) with [person_instance](../fixtures/reading/person_instance.json), and [other_schema](../fixtures/reading/other_schema.json) with [other_instance](../fixtures/reading/other_instance.json).

**When:** GET all four keys with `$select=content`.

**Then:** each of the four bodies has its own `gts_id`, `gts_uuid`, `kind`, `lifecycle_status`, and whole authored `content` JSON; the two schemas report `kind=type_schema` and the two Instances report `kind=instance`. No body contains `origin` or an unselected schema-only artifact.

### TR-READ-004 — Effective documents are independently selectable

**Given:** registered [trait_base_schema](../fixtures/reading/trait_base_schema.json), [trait_derived_schema](../fixtures/reading/trait_derived_schema.json), and their conforming [trait_employee_instance](../fixtures/reading/trait_employee_instance.json), plus the independent [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) pair. The base contributes required top-level `name`, an open `payload` and trait `category=internal`, constrained to `internal|public`; the derived schema requires `payload.employee_id`. The [trait_derived_expected](../fixtures/reading/trait_derived_expected.json) fixture lists expected properties, without prescribing the full JSON layout of materialized artifacts.

**When:** GET the derived schema separately with `$select=resolved_schema`, `effective_traits`, and `effective_traits_schema`; also GET [trait_employee_instance](../fixtures/reading/trait_employee_instance.json) and [other_instance](../fixtures/reading/other_instance.json) with `$select=resolved_schema`. In a separate operation, submit [trait_derived_extra_field_instance](../fixtures/reading/trait_derived_extra_field_instance.json), which adds an undeclared top-level `rogue` field, and await its terminal outcome.

**Then:** each derived-schema response includes exactly its named document plus the mandatory `gts_id`, `gts_uuid`, `kind`, and `lifecycle_status`; selecting traits does not transfer `resolved_schema`. The resolved schema requires `payload.employee_id`; effective traits contain `category=internal`, and the effective traits schema constrains it to `internal|public`, as listed in [trait_derived_expected](../fixtures/reading/trait_derived_expected.json). Both Instance responses have only their mandatory identity, kind and lifecycle fields, without `resolved_schema`. The valid derived Instance succeeds; the separate extra-field item has `status=failed`, `resource_version=null`, and `error.reason=invalid_value`, and its key remains absent. Together they show that the inherited envelope is closed while `payload` accepts derived fields.

### TR-READ-005 — Provenance is one selected group

**Given:** register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace.

**When:** GET both schemas and both Instances with `$select=provenance`.

**Then:** each selected group contains exactly `gts_spec_version`, `gts_impl_version`, and `compat_forced`; both Instances carry an explicit JSON `null` for `compat_forced`, while both schemas carry `false`. No provenance member or owning-gear attribution appears at the entity's top level.

### TR-READ-006 — Inapplicable Instance artifacts are absent

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. [person_instance](../fixtures/reading/person_instance.json) is the target; [other_schema](../fixtures/reading/other_schema.json) is a contrasting Type Schema.

**When:** GET [person_instance](../fixtures/reading/person_instance.json) with a selection of `resolved_schema,effective_traits,effective_traits_schema`; also GET [other_schema](../fixtures/reading/other_schema.json) with `$select=resolved_schema`.

**Then:** the Instance's `200` body contains only `gts_id`, `gts_uuid`, `kind=instance`, and `lifecycle_status`; the three Type Schema-only fields are absent rather than JSON `null`. The distinct schema response contains `resolved_schema`.

### TR-READ-007 — A projected tombstone remains distinguishable from absence

**Given:** [person_schema](../fixtures/reading/person_schema.json) is registered beside live [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) and [device_schema](../fixtures/reading/device_schema.json)/[device_instance](../fixtures/reading/device_instance.json) pairs. Read person with `$select=content` and retain its ETag, then delete person with a completed operation. [person_instance](../fixtures/reading/person_instance.json) is absent, so it cannot block deletion.

**When:** exact-GET the deleted person's key with `$select=content` and its old ETag in `If-None-Match`; repeat with the tombstone's new ETag. GET the live other's key with `$select=content`.

**Then:** the old live ETag yields `200` with a new ETag, authored content, `kind=type_schema`, and `lifecycle_status=deleted` with mandatory identity, even though neither `kind` nor `lifecycle_status` was named in the selection. The tombstone's ETag yields a bodyless `304` carrying that same ETag. The other response has its own content and `lifecycle_status=active`. Exact lookup has no lifecycle filter: it returns a tombstone by key even though default discovery lists only active entities.

### TR-READ-008 — Absent and impossible keys have one exact-read error shape

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. Prepare an unregistered GTS ID in that namespace, an unregistered UUID, and the impossible key `not-a-gts-id`.

**When:** GET each by key.

**Then:** all three return `404` with `application/problem+json`, equal Problem type/status and a request-specific `instance`; none is silently treated as a successful empty representation. The absent UUID exercises reverse lookup separately from the absent GTS ID.

## Batch read

### TR-READ-101 — Mixed batch answers every key and preserves absence

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. Prepare an absent valid GTS ID and an unregistered UUID. Request [person_schema](../fixtures/reading/person_schema.json) and [person_instance](../fixtures/reading/person_instance.json); leave [other_schema](../fixtures/reading/other_schema.json) and [other_instance](../fixtures/reading/other_instance.json) unrequested.

**When:** POST `:batchGet` with four keys: absent GTS ID, [person_schema](../fixtures/reading/person_schema.json)'s ID, absent UUID, and [person_instance](../fixtures/reading/person_instance.json)'s ID.

**Then:** `200` JSON contains exactly the four requested keys, each once; neither unrequested registered key appears. Both absent results are `not_found` without `entity` or `etag`, and found results contain an `etag` and the default document-free entity metadata shape. Neither kind of missing key prevents the other keys from being answered.

### TR-READ-102 — Top-level batch projection equals an exact read

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. Request [person_schema](../fixtures/reading/person_schema.json) and [person_instance](../fixtures/reading/person_instance.json); leave [other_schema](../fixtures/reading/other_schema.json) and [other_instance](../fixtures/reading/other_instance.json) unrequested.

**When:** POST `:batchGet` with top-level `"$select": "content,provenance"` for both keys, and GET each key with the same selection.

**Then:** the batch has exactly the two requested keys, no other registered key, and each `entity` body equals its exact GET body as a JSON value. Each batch `etag` is byte-identical to that key's exact GET `ETag` under the same selection. Each entity has only the mandatory `gts_id`, `gts_uuid`, `kind`, and `lifecycle_status`, authored `content`, and the `provenance` group.

### TR-READ-103 — A batch can read a tombstone beside a live entity

**Given:** register [person_schema](../fixtures/reading/person_schema.json), [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json), and [device_schema](../fixtures/reading/device_schema.json)/[device_instance](../fixtures/reading/device_instance.json) in a fresh namespace. Delete person with a completed operation; the other and device pairs remain active. [person_instance](../fixtures/reading/person_instance.json) is absent, so it cannot block deletion.

**When:** batch read [person_schema](../fixtures/reading/person_schema.json) and [other_schema](../fixtures/reading/other_schema.json) with `"$select": "content"`.

**Then:** exactly the two requested keys are `found`, with no device key; they report `deleted` versus `active` and return their respective authored content.

### TR-READ-104 — An unknown selection is refused on both read transports

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. [person_schema](../fixtures/reading/person_schema.json) is the request target.

**When:** request `$select=contents` on exact GET and as the top-level batch body field in separate requests.

**Then:** both return `400` Problem JSON with a `$select` field violation and `INVALID_SELECT`; neither falls back to the default representation.

### TR-READ-105 — Batch-wide If-None-Match is refused

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. [person_schema](../fixtures/reading/person_schema.json) is the request target.

**When:** POST `:batchGet` with an `If-None-Match` header.

**Then:** `400` Problem JSON names `If-None-Match`. Batch conditions are supplied per item, so this header is refused rather than ignored or applied to every result.

## Consistency across read routes

### TR-READ-201 — All read routes observe the completed content revision

**Given:** register mutable, major-only [person_schema](../fixtures/reading/person_schema.json) at resource version 1, [person_instance](../fixtures/reading/person_instance.json), and the independent [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) pair. Read person through exact GET, batchGet and namespace-scoped discovery with `$select=content,origin`, asserting its initial content and version on each route. The discovery result contains exactly all four registered identities and their known content; retain person's GTS ID, UUID and `created_at`.

**When:** submit [person_schema_revised](../fixtures/reading/person_schema_revised.json) with a fresh idempotency key, changing only the authored title from `Person` to `Revised Person` under `expected_resource_version=1`, and await a successful item at resource version 2. Repeat all three reads with the same selection, using the retained UUID for exact GET and the GTS ID for batchGet. Start a fresh discovery walk after the operation completes.

**Then:** the target entity on every route returns exactly the mandatory fields, updated authored `content` and managed `origin` with `resource_version=2`. The entity bodies agree and match the independently constructed updated document. GTS ID, UUID, kind, active lifecycle and `created_at` remain unchanged; `updated_at` is valid RFC 3339, agrees across routes and is not earlier than its previous value. Discovery still contains exactly the four expected identities: only person changed, and the other schema and both Instances retain their authored content and resource versions. No route returns the version-1 person content or metadata. No sleep or read retry is needed after successful operation completion.

### TR-READ-202 — Selected null content remains present on exact read

**Given:** registered [null_schema](../fixtures/reading/null_schema.json), whose authored JSON Schema has `type: "null"`, and its conforming [null_instance](../fixtures/reading/null_instance.json), submitted with an explicitly present `"content": null`. The independent [other_schema](../fixtures/reading/other_schema.json) and [other_instance](../fixtures/reading/other_instance.json) are also registered in the same namespace.

**When:** exact-GET the null Instance first with `$select=content` and then without selection. Also GET [other_instance](../fixtures/reading/other_instance.json) with `$select=content` as a contrasting key.

**Then:** the selected null Instance has exactly the mandatory `gts_id`, `gts_uuid`, `kind=instance`, `lifecycle_status=active`, and a present `content` field whose JSON value is `null`. Its default representation has the default metadata fields and omits `content` entirely. The other Instance returns its distinct key and non-null authored content. Assert field membership separately from value so a missing field cannot pass as null.

### TR-READ-203 — Exact keys never fall back to a registered minor version

**Given:** registered [person_minor_schema](../fixtures/reading/person_minor_schema.json), naming `person.v1.0~` in the fresh test namespace with its matching authored `$id`, plus [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) and [device_schema](../fixtures/reading/device_schema.json)/[device_instance](../fixtures/reading/device_instance.json) pairs. Do not register [person_schema](../fixtures/reading/person_schema.json): the same family's major-only `person.v1~` must remain absent. Registration of all entities has completed successfully.

**When:** exact-GET both full GTS IDs with `$select=content`, then batchGet `[major-only ID, minor-bearing ID]` with the same selection.

**Then:** exact GET of the major-only ID returns `404` Problem JSON, while exact GET of the minor-bearing ID returns `200` with its exact identity and authored content. The batch returns `200` with two results matched by echoed key: `not_found` without `entity` for the major-only key, and `found` with the expected minor entity for the other key. No route synthesizes a major-only entity or treats an exact key as a discovery pattern.

## Conditional reads

### TR-READ-301 — An exact ETag survives no-op admission and changes after revision

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. GET [person_schema](../fixtures/reading/person_schema.json) and [person_instance](../fixtures/reading/person_instance.json) with `$select=content,origin`; retain their ETags and version-1 bodies.

**When:** GET the schema again with the same selection and `If-None-Match`. Re-register identical schema content with `expected_resource_version=1` and await an `unchanged` item; repeat the conditional GET. Then submit [person_schema_revised](../fixtures/reading/person_schema_revised.json) with version 1 as its precondition, await a successful version-2 item, and GET both the schema and Instance with their old ETags.

**Then:** both schema reads before the revision return bodyless `304` with the original ETag. After revision the schema returns `200`, the independently expected revised content and origin at version 2, and a different ETag; its new ETag returns bodyless `304`. The unchanged Instance returns bodyless `304` with its original ETag, because its own resource version did not move. Read immediately after each terminal operation without sleeps or retries.

### TR-READ-302 — Batch revalidation answers each key independently

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. First batchGet [person_schema](../fixtures/reading/person_schema.json) and [other_schema](../fixtures/reading/other_schema.json) with top-level `"$select": "content,origin"`; retain each result's `etag`. Revise person using [person_schema_revised](../fixtures/reading/person_schema_revised.json), await success at version 2, and prepare an unregistered key.

**When:** batchGet other with its retained `if_none_match`, person with its now-stale `if_none_match`, [other_instance](../fixtures/reading/other_instance.json) without a condition, and the absent key with any retained tag. Build a second request by copying the returned `etag` of every found or unchanged key into its next `if_none_match`.

**Then:** the first conditional batch returns HTTP `200`: other is `unchanged` with the same `etag` and no `entity`; person is `found` with its revised document and new `etag`; the unconditioned Instance is `found` with its own content and `etag`; the absent key is `not_found` without either field. The second batch returns HTTP `200` with three `unchanged` results. Match every result by echoed key after checking completeness and uniqueness, since batch order is not contractual.

### TR-READ-303 — A validator belongs to one projection

**Given:** Register [person_schema](../fixtures/reading/person_schema.json), [person_instance](../fixtures/reading/person_instance.json), [other_schema](../fixtures/reading/other_schema.json), and [other_instance](../fixtures/reading/other_instance.json) in one fresh namespace. Person and other schema documents differ.

**When:** GET [person_schema](../fixtures/reading/person_schema.json) and [person_instance](../fixtures/reading/person_instance.json) without `$select` and retain their ETags. GET each key with `$select=content` and `If-None-Match` set to its metadata ETag; repeat each with its content response's ETag. GET [other_schema](../fixtures/reading/other_schema.json) with `$select=content` as a contrasting key.

**Then:** both wider reads return `200`, their own full authored content and ETags different from their metadata ETags; their repeats return bodyless `304` carrying the corresponding content ETags. The other schema returns its own document. A metadata validator never declares a wider schema or Instance representation unchanged.

### TR-READ-304 — A base refresh invalidates its derived schema's ETag

**Given:** registered [trait_base_schema](../fixtures/reading/trait_base_schema.json), [trait_derived_schema](../fixtures/reading/trait_derived_schema.json), [trait_employee_instance](../fixtures/reading/trait_employee_instance.json), and the independent [other_schema](../fixtures/reading/other_schema.json)/[other_instance](../fixtures/reading/other_instance.json) pair. GET the derived schema and both controls with `$select=resolved_schema,origin`; retain their ETags. The derived `resolved_schema` requires `payload.employee_id`, and its own `resource_version` is 1.

**When:** submit [trait_base_schema_revised](../fixtures/reading/trait_base_schema_revised.json), changing only the base title under `expected_resource_version=1`, and await a successful item. GET the derived schema and both controls with the same selection and their old ETags; repeat the derived read with its new ETag.

**Then:** the derived old ETag returns `200`: its resolved schema contains the revised base title and still requires `payload.employee_id`, its own `resource_version` remains 1, and its ETag changes. Both independent control reads return bodyless `304` with unchanged ETags. The derived new ETag also returns a bodyless `304`. No read retry is needed after the base operation completes.
