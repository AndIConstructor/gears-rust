"""Dry-run registration as an HTTP prediction of a later commit."""

import uuid

import pytest

from .helpers import (
    assert_absent,
    assert_json,
    instance_entity,
    outcome,
    read_created,
    register_and_assert,
    schema_entity,
)


@pytest.mark.scenario("TR-REG-701")
async def test_dry_run_predicts_creation_then_new_key_commits_it(
    registry_http, registry_api_path, registration_fixture
):
    """Dry-run predicts both creations without reserving either identifier."""
    schema = registration_fixture("person_schema")
    instance = registration_fixture("person_instance")
    dry_key = str(uuid.uuid4())
    commit_key = str(uuid.uuid4())

    predicted = await register_and_assert(
        registry_http,
        registry_api_path,
        [instance, schema],
        outcome(instance, "succeeded", None),
        outcome(schema, "succeeded", None),
        dry_run=True,
        idempotency_key=dry_key,
    )
    await assert_absent(registry_http, registry_api_path, schema)
    await assert_absent(registry_http, registry_api_path, instance)

    committed = await register_and_assert(
        registry_http,
        registry_api_path,
        [instance, schema],
        outcome(instance, "succeeded", 1),
        outcome(schema, "succeeded", 1),
        idempotency_key=commit_key,
    )
    assert committed["operation_id"] != predicted["operation_id"]
    await read_created(registry_http, registry_api_path, schema_entity(schema, 1), committed)
    await read_created(
        registry_http, registry_api_path, instance_entity(instance, 1), committed
    )


@pytest.mark.scenario("TR-REG-702")
async def test_mixed_dry_run_predicts_committed_item_verdicts(
    registry_http, registry_api_path, registration_fixture
):
    """Dry-run and commit agree on each item verdict in a partial batch."""
    independent = registration_fixture("person_schema")
    broken = registration_fixture("missing_ref_schema")
    blocked = registration_fixture("blocked_instance")
    candidates = [blocked, broken, independent]
    missing_id = broken["content"]["allOf"][0]["$ref"].removeprefix("gts://")
    dry_key = str(uuid.uuid4())
    commit_key = str(uuid.uuid4())

    predicted = await register_and_assert(
        registry_http,
        registry_api_path,
        candidates,
        outcome(blocked, "failed", None, "blocked_by_dependency"),
        outcome(
            broken,
            "failed",
            None,
            "dependency_not_found",
            dependency_id=missing_id,
            dependency_kind="ref",
        ),
        outcome(independent, "succeeded", None),
        dry_run=True,
        idempotency_key=dry_key,
    )
    for document in candidates:
        await assert_absent(registry_http, registry_api_path, document)

    committed = await register_and_assert(
        registry_http,
        registry_api_path,
        candidates,
        outcome(blocked, "failed", None, "blocked_by_dependency"),
        outcome(
            broken,
            "failed",
            None,
            "dependency_not_found",
            dependency_id=missing_id,
            dependency_kind="ref",
        ),
        outcome(independent, "succeeded", 1),
        idempotency_key=commit_key,
    )
    assert committed["operation_id"] != predicted["operation_id"]

    def verdicts(operation):
        return {
            item["gts_id"]: {
                "status": item["status"],
                "reason": None if item["error"] is None else item["error"]["reason"],
            }
            for item in operation["items"]
        }

    assert_json(verdicts(predicted), verdicts(committed))
    for document in (broken, blocked):
        await assert_absent(registry_http, registry_api_path, document)
    await read_created(
        registry_http, registry_api_path, schema_entity(independent, 1), committed
    )
