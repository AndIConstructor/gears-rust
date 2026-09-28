"""Schema compatibility and the safety of live dependent entities."""

from copy import deepcopy

import pytest

from .helpers import (
    assert_exact_entity,
    assert_json,
    instance_entity,
    outcome,
    register_and_assert,
    schema_entity,
    schema_with_id,
)


@pytest.mark.scenario("TR-REG-501")
async def test_optional_property_at_an_open_level_is_incompatible(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """Adding a typed optional property narrows values at an open level."""
    schema = registration_fixture("person_schema")
    await given_registered(schema)
    original = await assert_exact_entity(
        registry_http, registry_api_path, schema, schema_entity(schema, 1)
    )

    open_addition = deepcopy(schema)
    open_addition["content"]["properties"]["payload"]["properties"] = {
        "employee_id": {"type": "string"}
    }
    open_addition["expected_resource_version"] = 1
    await register_and_assert(
        registry_http,
        registry_api_path,
        [open_addition],
        outcome(schema, "failed", None, "incompatible_with_baseline"),
    )
    after_refusal = await assert_exact_entity(
        registry_http,
        registry_api_path,
        schema,
        schema_entity(schema, 1),
        etag=original.headers["etag"],
    )
    assert_json(after_refusal.json(), original.json())


@pytest.mark.scenario("TR-REG-502")
async def test_direct_instance_prevents_abstract_transition(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """A live direct Instance prevents its concrete schema becoming abstract."""
    schema = registration_fixture("person_schema")
    schema["content"]["x-gts-abstract"] = False
    instance = registration_fixture("person_instance")
    await given_registered(schema, instance)
    schema_before = await assert_exact_entity(
        registry_http, registry_api_path, schema, schema_entity(schema, 1)
    )
    instance_before = await assert_exact_entity(
        registry_http, registry_api_path, instance, instance_entity(instance, 1)
    )

    abstract = deepcopy(schema)
    abstract["content"]["x-gts-abstract"] = True
    abstract["expected_resource_version"] = 1
    await register_and_assert(
        registry_http,
        registry_api_path,
        [abstract],
        outcome(schema, "failed", None, "dependent_invalid"),
    )
    schema_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        schema,
        schema_entity(schema, 1),
        etag=schema_before.headers["etag"],
    )
    instance_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        instance,
        instance_entity(instance, 1),
        etag=instance_before.headers["etag"],
    )
    assert_json(schema_after.json(), schema_before.json())
    assert_json(instance_after.json(), instance_before.json())


@pytest.mark.scenario("TR-REG-503")
async def test_derived_schema_prevents_final_transition(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """A live derived Type Schema prevents its base becoming final."""
    base = registration_fixture("person_schema")
    base["content"]["x-gts-final"] = False
    derived = registration_fixture("derived_employee_schema")
    await given_registered(base, derived)
    # The base replaces its `$ref` without `$id`, `$schema` or the `x-gts-final`
    # annotation, which governs derivation rather than validation.
    resolved_derived = deepcopy(derived["content"])
    resolved_derived["allOf"][0] = {
        key: value
        for key, value in base["content"].items()
        if key not in {"$id", "$schema", "x-gts-final"}
    }
    base_before = await assert_exact_entity(
        registry_http, registry_api_path, base, schema_entity(base, 1)
    )
    derived_before = await assert_exact_entity(
        registry_http,
        registry_api_path,
        derived,
        schema_entity(derived, 1, resolved_schema=resolved_derived),
    )

    final = deepcopy(base)
    final["content"]["x-gts-final"] = True
    final["expected_resource_version"] = 1
    await register_and_assert(
        registry_http,
        registry_api_path,
        [final],
        outcome(base, "failed", None, "dependent_invalid"),
    )
    base_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        base,
        schema_entity(base, 1),
        etag=base_before.headers["etag"],
    )
    derived_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        derived,
        schema_entity(derived, 1, resolved_schema=resolved_derived),
        etag=derived_before.headers["etag"],
    )
    assert_json(base_after.json(), base_before.json())
    assert_json(derived_after.json(), derived_before.json())


@pytest.mark.scenario("TR-REG-504")
async def test_breaking_contract_is_published_under_a_new_major(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """A new major accepts breaking constraints while v1 remains readable."""
    v1 = registration_fixture("person_schema")
    instance = registration_fixture("person_instance")
    await given_registered(v1, instance)
    v1_before = await assert_exact_entity(
        registry_http, registry_api_path, v1, schema_entity(v1, 1)
    )
    instance_before = await assert_exact_entity(
        registry_http, registry_api_path, instance, instance_entity(instance, 1)
    )

    v2 = schema_with_id(v1, v1["gts_id"].replace(".person.v1~", ".person.v2~"))
    # A v1 value with a string `name` no longer conforms: a breaking change.
    v2["content"]["properties"]["name"]["type"] = "integer"
    await register_and_assert(
        registry_http, registry_api_path, [v2], outcome(v2, "succeeded", 1)
    )
    v2_read = await assert_exact_entity(
        registry_http, registry_api_path, v2, schema_entity(v2, 1)
    )
    assert v2_read.json()["gts_uuid"] != v1_before.json()["gts_uuid"]
    v1_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        v1,
        schema_entity(v1, 1),
        etag=v1_before.headers["etag"],
    )
    instance_after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        instance,
        instance_entity(instance, 1),
        etag=instance_before.headers["etag"],
    )
    assert_json(v1_after.json(), v1_before.json())
    assert_json(instance_after.json(), instance_before.json())
