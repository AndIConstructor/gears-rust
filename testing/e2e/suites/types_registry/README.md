<!-- Updated: 2026-09-28 by Constructor Tech -->

# Types Registry e2e suite

The suite covers two APIs:

| Path | Covers |
|---|---|
| `test_registration.py`, `test_deletion.py` | the asynchronous admission API (`202` + `GET /operations/{id}`), written as numbered scenarios |
| [`legacy/`](legacy/README.md) | the original synchronous `v1` API (`200` + a `results` array) |

Async scenarios pair tests, `scenarios/` documentation and `fixtures/` data: [registration](scenarios/registration.md) (`TR-REG-*`), [deletion](scenarios/deletion.md) (`TR-DEL-*`), and planned [exact/batch reads](scenarios/reading.md) (`TR-READ-*`) and [discovery](scenarios/discovery.md) (`TR-DISC-*`).

`conftest.py` provides HTTP/fixture setup and scenario-ID binding; `helpers.py` provides submit-and-poll helpers and comparators. Pytest collects both sets.

## Test scope

E2E tests serve as executable documentation of the registry's client contract. Write an e2e scenario when it:

- demonstrates a client workflow, such as discovery followed by reading returned references, or reading again after a completed mutation;
- verifies an HTTP contract through the running server, such as conditional reads with ETag and a bodyless `304`;
- explains a rule that affects client behavior, such as tombstones remaining readable or a dependency refresh changing a derived schema's validator.

Keep representative success and failure cases. Limited overlap with Rust tests and repeated endpoint calls are intentional exceptions to the [general E2E guide](../../../../docs/toolkit_unified_system/13_e2e_testing.md): they make complete client workflows readable and verifiable.

Leave exhaustive parameter combinations, malformed inputs, numeric boundaries, and token encoding to Rust unit and integration tests. SQL behavior, query counts, snapshot consistency and races also belong there, where they can be observed and controlled directly. Backend-specific guarantees require tests on those databases; the local HTTP launcher uses SQLite.

## Execution rules

- `registry_api` defaults to `v2` today. Set `TYPES_REGISTRY_API_VERSION` explicitly when running against another version; there is no fallback.
- Each test gets a fresh `cf.e2e.r<uuid>.` namespace. Fixture loaders rewrite IDs and `$ref` targets, not schema constraints or values.
- Every submission carries a fresh `Idempotency-Key`.
- Registration outcomes match by GTS ID. Deletion outcomes preserve request order and use `assert_operation(..., ordered=True)`.
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

# Legacy tests only.
.venv/bin/python tools/scripts/run_e2e.py --suite types-registry -- -m "not scenario"

# Collection only; verifies scenario-ID bindings.
.venv/bin/python -m pytest testing/e2e/suites/types_registry --collect-only
```

Select subsets by marker: arguments after `--` are appended after the suite path. Scenario tests carry the `scenario` marker; legacy tests do not.

The local launcher serves authenticated v2 directly; no edge gateway is required.
