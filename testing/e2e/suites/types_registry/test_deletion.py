"""Deletion scenarios: tombstones, dependant-first ordering and refusals."""

import pytest

from .helpers import (
    RECEIPT,
    assert_operation,
    completed,
    delete_batch_and_poll,
    delete_one_and_poll,
    outcome,
    read_entity,
    read_tombstone,
)


@pytest.mark.smoke
@pytest.mark.scenario("TR-DEL-001")
async def test_delete_one_entity_leaves_a_readable_tombstone(
    registry_http, registry_api_path, deletion_fixture, given_registered
):
    """A deleted entity keeps its whole body and reads back as a tombstone."""
    schema = deletion_fixture("person_schema")
    await given_registered(schema)
    before = await read_entity(registry_http, registry_api_path, schema["gts_id"])
    assert before["lifecycle_status"] == "active", before
    assert before["origin"]["resource_version"] == 1, before

    operation = await delete_one_and_poll(
        registry_http, registry_api_path, schema["gts_id"], 1, RECEIPT
    )
    assert_operation(
        operation,
        completed("deletion", outcome(schema, "succeeded", 2)),
        ordered=True,
    )
    await read_tombstone(registry_http, registry_api_path, before, operation)


@pytest.mark.scenario("TR-DEL-002")
async def test_batch_deletion_orders_dependants_before_their_target(
    registry_http, registry_api_path, deletion_fixture, given_registered
):
    """A batch naming a schema before its own Instance still deletes both."""
    schema = deletion_fixture("person_schema")
    instance = deletion_fixture("person_instance")
    await given_registered(schema, instance)

    operation = await delete_batch_and_poll(
        registry_http,
        registry_api_path,
        [
            {"key": schema["gts_id"], "expected_resource_version": 1},
            {"key": instance["gts_id"], "expected_resource_version": 1},
        ],
        RECEIPT,
    )
    assert_operation(
        operation,
        completed(
            "deletion", outcome(schema, "succeeded", 2), outcome(instance, "succeeded", 2)
        ),
        ordered=True,
    )
    for candidate in (schema, instance):
        body = await read_entity(registry_http, registry_api_path, candidate["gts_id"])
        assert body["lifecycle_status"] == "deleted", body


@pytest.mark.scenario("TR-DEL-003")
async def test_batch_deletion_reports_outcomes_in_request_order(
    registry_http, registry_api_path, deletion_fixture, given_registered
):
    """Outcomes arrive in request order, which is what UUID callers match on."""
    person = deletion_fixture("person_schema")
    other = deletion_fixture("other_schema")
    await given_registered(person, other)
    assert other["gts_id"] < person["gts_id"]

    operation = await delete_batch_and_poll(
        registry_http,
        registry_api_path,
        [
            {"key": person["gts_id"], "expected_resource_version": 1},
            {"key": other["gts_id"], "expected_resource_version": 1},
        ],
        RECEIPT,
    )
    assert_operation(
        operation,
        completed(
            "deletion", outcome(person, "succeeded", 2), outcome(other, "succeeded", 2)
        ),
        ordered=True,
    )


@pytest.mark.scenario("TR-DEL-101")
async def test_a_stale_expected_version_is_a_terminal_item_not_a_412(
    registry_http, registry_api_path, deletion_fixture, given_registered
):
    """A failed precondition is reported on the operation, not as HTTP 412."""
    schema = deletion_fixture("person_schema")
    await given_registered(schema)

    operation = await delete_one_and_poll(
        registry_http, registry_api_path, schema["gts_id"], 7, RECEIPT
    )
    assert_operation(
        operation,
        completed("deletion", outcome(schema, "failed", None, "precondition_failed")),
        ordered=True,
    )

    after = await read_entity(registry_http, registry_api_path, schema["gts_id"])
    assert after["lifecycle_status"] == "active", after
    assert after["origin"]["resource_version"] == 1, after


@pytest.mark.scenario("TR-DEL-102")
async def test_a_live_dependant_outside_the_batch_blocks_the_deletion(
    registry_http, registry_api_path, deletion_fixture, given_registered
):
    """Ordering resolves in-batch dependants only; external ones refuse."""
    schema = deletion_fixture("person_schema")
    instance = deletion_fixture("person_instance")
    await given_registered(schema, instance)

    operation = await delete_one_and_poll(
        registry_http, registry_api_path, schema["gts_id"], 1, RECEIPT
    )
    # The refusal says how many dependants block it, never which ones: the
    # caller may not be entitled to read them.
    assert_operation(
        operation,
        completed(
            "deletion",
            outcome(
                schema,
                "failed",
                None,
                "has_registered_dependents",
                message=f"'{schema['gts_id']}' has 1 live direct registered "
                "dependants; delete or revise them first",
            ),
        ),
        ordered=True,
        exact_messages=True,
    )

    after = await read_entity(registry_http, registry_api_path, schema["gts_id"])
    assert after["lifecycle_status"] == "active", after
    assert after["origin"]["resource_version"] == 1, after
