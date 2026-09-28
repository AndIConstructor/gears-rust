"""Registration dependency ordering and independent item outcomes."""

from copy import deepcopy

import pytest

from .helpers import (
    assert_absent,
    assert_exact_entity,
    assert_json,
    instance_entity,
    outcome,
    read_created,
    register_and_assert,
    schema_entity,
)


@pytest.mark.scenario("TR-REG-401")
async def test_derived_schema_precedes_its_base_in_the_request(
    registry_http, registry_api_path, registration_fixture
):
    """A derived schema succeeds before its base in one request."""
    base = registration_fixture("person_schema")
    derived = registration_fixture("derived_employee_schema")
    operation = await register_and_assert(
        registry_http,
        registry_api_path,
        [derived, base],
        outcome(derived, "succeeded", 1),
        outcome(base, "succeeded", 1),
    )
    await read_created(registry_http, registry_api_path, schema_entity(base, 1), operation)
    # The base replaces its `$ref` in `allOf`, without its own `$id` and `$schema`.
    resolved = deepcopy(derived["content"])
    resolved["allOf"][0] = {
        key: value for key, value in base["content"].items() if key not in {"$id", "$schema"}
    }
    await read_created(
        registry_http,
        registry_api_path,
        schema_entity(derived, 1, resolved_schema=resolved),
        operation,
    )


@pytest.mark.scenario("TR-REG-402")
async def test_referrer_precedes_its_target_in_the_request(
    registry_http, registry_api_path, registration_fixture
):
    """An authored reference resolves when its target appears later in the batch."""
    target = registration_fixture("person_schema")
    referrer = registration_fixture("person_referrer_schema")
    operation = await register_and_assert(
        registry_http,
        registry_api_path,
        [referrer, target],
        outcome(referrer, "succeeded", 1),
        outcome(target, "succeeded", 1),
    )
    await read_created(registry_http, registry_api_path, schema_entity(target, 1), operation)
    # The target replaces the `$ref`, without its own `$id` and `$schema`.
    resolved = deepcopy(referrer["content"])
    resolved["properties"]["payload"]["properties"]["person"] = {
        key: value for key, value in target["content"].items() if key not in {"$id", "$schema"}
    }
    await read_created(
        registry_http,
        registry_api_path,
        schema_entity(referrer, 1, resolved_schema=resolved),
        operation,
    )


@pytest.mark.scenario("TR-REG-403")
async def test_missing_conforming_schema_can_be_registered_before_retry(
    registry_http, registry_api_path, registration_fixture
):
    """An Instance succeeds under a new key after its missing schema arrives."""
    schema = registration_fixture("person_schema")
    instance = registration_fixture("person_instance")
    missing = await register_and_assert(
        registry_http,
        registry_api_path,
        [instance],
        outcome(
            instance,
            "failed",
            None,
            "dependency_not_found",
            dependency_id=schema["gts_id"],
            dependency_kind="conforming_type",
        ),
    )
    await assert_absent(registry_http, registry_api_path, instance)
    await register_and_assert(
        registry_http, registry_api_path, [schema], outcome(schema, "succeeded", 1)
    )
    admitted = await register_and_assert(
        registry_http, registry_api_path, [instance], outcome(instance, "succeeded", 1)
    )
    await read_created(
        registry_http,
        registry_api_path,
        instance_entity(instance, 1),
        admitted,
    )
    original_response = await registry_http.get(
        f"{registry_api_path}/operations/{missing['operation_id']}"
    )
    assert original_response.status_code == 200, original_response.text
    assert original_response.headers["content-type"].startswith("application/json")
    assert_json(original_response.json(), missing)


@pytest.mark.scenario("TR-REG-404")
async def test_invalid_instance_does_not_block_valid_neighbour(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """One invalid Instance does not prevent a valid neighbour from committing."""
    schema = registration_fixture("person_schema")
    template = registration_fixture("person_instance")
    await given_registered(schema)
    valid = deepcopy(template)
    valid["gts_id"] = valid["gts_id"].replace(".alice.v1", ".bob.v1")
    valid["content"]["name"] = "Bob"
    invalid = deepcopy(template)
    invalid["gts_id"] = invalid["gts_id"].replace(".alice.v1", ".carol.v1")
    del invalid["content"]["name"]

    operation = await register_and_assert(
        registry_http,
        registry_api_path,
        [valid, invalid],
        outcome(valid, "succeeded", 1),
        outcome(invalid, "failed", None, "invalid_value"),
    )
    await read_created(
        registry_http,
        registry_api_path,
        instance_entity(valid, 1),
        operation,
    )
    await assert_absent(registry_http, registry_api_path, invalid)


@pytest.mark.scenario("TR-REG-405")
async def test_failed_base_revision_blocks_its_new_referrer(
    registry_http, registry_api_path, registration_fixture, given_registered
):
    """A new referrer cannot fall back to a failed in-batch base revision."""
    base = registration_fixture("person_schema")
    referrer = registration_fixture("person_referrer_schema")
    await given_registered(base)
    before = await assert_exact_entity(
        registry_http, registry_api_path, base, schema_entity(base, 1)
    )
    revision = deepcopy(base)
    del revision["content"]["properties"]["name"]
    revision["content"]["required"].remove("name")
    revision["expected_resource_version"] = 1

    await register_and_assert(
        registry_http,
        registry_api_path,
        [referrer, revision],
        outcome(referrer, "failed", None, "blocked_by_dependency"),
        outcome(base, "failed", None, "incompatible_with_baseline"),
    )
    after = await assert_exact_entity(
        registry_http,
        registry_api_path,
        base,
        schema_entity(base, 1),
        etag=before.headers["etag"],
    )
    assert_json(after.json(), before.json())
    await assert_absent(registry_http, registry_api_path, referrer)


@pytest.mark.scenario("TR-REG-406")
async def test_reference_cycle_fails_beside_an_independent_success(
    registry_http, registry_api_path, registration_fixture
):
    """A mutual-reference cycle fails without suppressing independent success."""
    a = registration_fixture("cycle_a_schema")
    b = registration_fixture("cycle_b_schema")
    c = registration_fixture("person_schema")
    operation = await register_and_assert(
        registry_http,
        registry_api_path,
        [c, b, a],
        outcome(c, "succeeded", 1),
        outcome(b, "failed", None, "invalid_schema"),
        outcome(a, "failed", None, "invalid_schema"),
    )
    await read_created(registry_http, registry_api_path, schema_entity(c, 1), operation)
    await assert_absent(registry_http, registry_api_path, a)
    await assert_absent(registry_http, registry_api_path, b)
