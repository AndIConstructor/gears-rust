<!-- Updated: 2026-09-28 by Constructor Tech -->

# Types Registry e2e suite

The suite covers two APIs:

| Path | Covers |
|---|---|
| `test_registration_*.py`, `test_deletion.py` | the asynchronous admission API (`202` + `GET /operations/{id}`), written as numbered scenarios |
| `test_reading.py`, `test_discovery.py` | exact reads, `:batchGet` and discovery pages over that API, also numbered scenarios |
| [`legacy/`](legacy/README.md) | the original synchronous `v1` API (`200` + a `results` array) |

Async scenarios pair tests, `scenarios/` documentation and `fixtures/` data: [registration](scenarios/registration.md) (`TR-REG-*`), [deletion](scenarios/deletion.md) (`TR-DEL-*`), [exact/batch reads](scenarios/reading.md) (`TR-READ-*`) and [discovery](scenarios/discovery.md) (`TR-DISC-*`). The default Types Registry e2e config enables compatibility force and allows vendor `acme` only under `gts.acme.e2e.*`. TR-REG-902 uses the force-disabled profile; the default profile skips it.

Each section of [registration.md](scenarios/registration.md) has one test file:

| Scenario section | Test file |
|---|---|
| Successful registration | `test_registration_success.py` |
| Partial success and refusals | `test_registration_refusals.py` |
| Request identity and idempotency | `test_registration_idempotency.py` |
| Content revisions and optimistic preconditions | `test_registration_revisions.py` |
| Dependency graph and partial admission | `test_registration_graph.py` |
| Compatibility and dependent safety | `test_registration_compatibility.py` |
| Minor versions and version-family shape | `test_registration_versions.py` |
| Dry-run admission | `test_registration_dry_run.py` |
| Managed schema dialect and unstable major-zero profile | `test_registration_schema_profile.py` |
| Deployment policy and compatibility waiver | `test_registration_deployment_policy.py` |

`conftest.py` provides HTTP/fixture setup, `given_registered` and scenario-ID binding; `helpers.py` provides submit-and-poll and read helpers, expected-body builders and comparators. Pytest collects both sets.

## Test scope

E2E tests serve as executable documentation of the registry's client contract. Write an e2e scenario when it:

- demonstrates a client workflow, such as discovery followed by reading returned references, or reading again after a completed mutation;
- verifies an HTTP contract through the running server, such as conditional reads with ETag and a bodyless `304`;
- explains a rule that affects client behavior, such as tombstones remaining readable or a dependency refresh changing a derived schema's validator.

Keep representative success and failure cases. Limited overlap with Rust tests and repeated endpoint calls are intentional exceptions to the [general E2E guide](../../../../docs/toolkit_unified_system/13_e2e_testing.md): they make complete client workflows readable and verifiable.

Leave exhaustive parameter combinations, malformed inputs, numeric boundaries, and token encoding to Rust unit and integration tests. SQL behavior, query counts, snapshot consistency and races also belong there, where they can be observed and controlled directly. Backend-specific guarantees require tests on those databases; the local HTTP launcher uses SQLite.

## Execution rules

- `registry_api` defaults to `v2` today. Set `TYPES_REGISTRY_API_VERSION` explicitly when running against another version; there is no fallback.
- Each test gets a fresh `cf.e2e.r<uuid>.` namespace. Fixture loaders rewrite IDs and `$ref` targets, not schema constraints or values. TR-REG-901 and TR-REG-904 change the vendor and package after loading to exercise the configured policy region.
- Submissions use a fresh `Idempotency-Key` by default. Replay and conflict
  scenarios intentionally reuse the key they are testing.
- Registration outcomes match by GTS ID. Deletion outcomes preserve request order and use `assert_operation(..., ordered=True)`.
- Read responses are compared whole. `assert_exact` compares `{"status", "etag", "body"}` as one JSON value; a `304` has no `body`. An expected `"<etag>"` stands for a validated ETag that is none of the known ones, so a changed ETag is part of the expectation. `assert_batch` matches results by echoed key, because batch order is not contractual. `walk` follows every cursor and `assert_pages` compares the whole walk.
- Only unpredictable values are masked, after validation: operation IDs, receipt status, timestamps, new ETags, cursors, implementation versions, trace IDs and non-contractual messages.
- Tests contain complete expected bodies; scenario docs state intent.

## Run

From the repository root, with the existing Python environment:

```sh
# Whole suite.
.venv/bin/python tools/scripts/run_e2e.py --suite types-registry

# Through make.
make e2e-local SUITE=types-registry

# Scenario tests only.
.venv/bin/python tools/scripts/run_e2e.py --suite types-registry -- -m scenario

# Disabled-force refusal (TR-REG-902).
.venv/bin/python tools/scripts/run_e2e.py --suite types-registry --profile force-disabled -- -k test_disabled_force_rejects_commit_and_dry_run

# Legacy tests only.
.venv/bin/python tools/scripts/run_e2e.py --suite types-registry -- -m "not scenario"

# Collection only; verifies scenario-ID bindings.
.venv/bin/python -m pytest testing/e2e/suites/types_registry --collect-only
```

Select subsets by marker: arguments after `--` are appended after the suite path. Scenario tests carry the `scenario` marker; legacy tests do not.

The local launcher serves authenticated v2 directly; no edge gateway is required.
