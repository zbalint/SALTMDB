# SPEC-TEST-LIBRARIAN-DRAIN

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-083 (narrowed to its verified root cause; the wider sweep of the other `TemporaryDirectory()` users stays in BL-083). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `90df320` or later; `./verify` exits 0 at `90df320` (3638 passed, 10 skipped, 391 subtests passed; the BL-083 teardown flake can fail two tests in a run, rerun once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `tests/mcp_contract.py::cleanup` (shared by 10 test files) and the existing `tests/test_mcp_lists_events_traces_v2.py`.
- Scope (may edit or create): `tests/mcp_contract.py` (`cleanup` and one new helper), `tests/test_mcp_contract_cleanup.py` (new). Nothing under `src/`.
- Does not touch: any production module, any other test file, `pyproject.toml`, `hooks/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`./verify` fails intermittently with `OSError: [Errno 39] Directory not empty` while a test removes its temporary directory, in `tests/test_mcp_lists_events_traces_v2.py::test_event_service_tie_break_is_explicit` and `::test_event_service_different_timestamp_order` (seen in 2 of the architect's last 4 full `./verify` runs; reproduced alone at 1 of 25 and 2 of 80 runs of those tests). Both tests call `event_service.log_event(...)` directly. `log_event` ends with `trigger_librarian(db_path=...)` (`event_service.py` 78 to 80), which submits `_run_maintenance_pass_impl` to the module's single-worker pool `librarian_service._librarian_trigger_pool` (`librarian_service.py` 21, 37 to 39). That worker thread opens the test's database path and may create files in the temporary directory at the moment `TemporaryDirectory.cleanup()` is deleting it. The test's own `tearDown` calls `mcp_contract.cleanup(...)`, which closes the test's connection but does not wait for that background work.

Evidence for the fix: in a scratch copy of the tests, with a teardown that first runs `librarian_service._librarian_trigger_pool.submit(lambda: None).result(timeout=30)`, 0 of 80 runs failed against 2 of 80 for the unmodified copy. Because the pool has ONE worker, a submitted no-op completes only after every earlier job has finished: it is a barrier.

A pytest autouse fixture does not help: for a unittest `TestCase` the fixture's teardown runs after `tearDown`, which has already removed the directory. The drain must happen inside `mcp_contract.cleanup`, after the connection is closed and before the caller removes the directory.

Rejected: `ignore_cleanup_errors=True` (hides a late write instead of waiting for it; the late write could also fail a test that inspects the directory); patching `trigger_librarian` out in every test (changes what the tests exercise); a sleep (flaky by construction).

## 2. Decisions

- D1. New helper in `tests/mcp_contract.py`, `drain_librarian_pool(timeout: float = 30.0) -> None`: submits a no-op to `librarian_service._librarian_trigger_pool` and waits for it with `Future.result(timeout=timeout)`. A timeout raises (it is a test failure, not a silent skip). The attribute is read from the module at call time.
- D2. `cleanup(connection, previous_backend, previous_db_path)` keeps its signature and order and gains one call to the helper as its LAST step (after `connection.close()`), so no caller changes.
- D3. A pool that was never used is fine: the barrier submit also works then.
- D4. No `noqa`, no `time.sleep`, no change to any production module.

## 3. Tests (written first; the first is red on the old `cleanup`)

In `tests/test_mcp_contract_cleanup.py`:
1. Deterministic regression: configure the contract with a temporary database (`mcp_contract.configure`), patch `librarian_service._run_maintenance_pass_impl` (module attribute) with a function that sleeps 0.2 s and then writes a marker file into the temporary directory and sets an event; call `event_service.log_event(..., db_connection=connection)` so the pool is triggered; call `mcp_contract.cleanup(...)`; assert the event is set and the marker exists BEFORE the directory is removed (red on the old `cleanup`, which returns while the worker still sleeps), then remove the directory without error.
2. `drain_librarian_pool` with nothing queued returns promptly; with a job that never finishes (patch the maintenance function to wait on an event the test sets afterwards) and a short timeout it raises `concurrent.futures.TimeoutError`, and the test then releases the event so the pool is not left blocked.
3. The existing 20 tests of `tests/test_mcp_lists_events_traces_v2.py` still pass (contract test).

## 4. Out of scope

The other `TemporaryDirectory()` users (9 files also call `log_event` or `store_memory` directly and have not been seen to flake; BL-083 keeps the sweep); `ignore_cleanup_errors`; any production change; `trigger_community_detection`'s use of the same pool (the barrier covers it).

## 5. Acceptance

1. `./verify` exits 0.
2. Test 1 is red on the unchanged `cleanup` and green after (the developer reports both runs; the architect reruns on the final tree).
3. `for i in $(seq 1 100); do .venv/bin/python -m pytest -q -x tests/test_mcp_lists_events_traces_v2.py -k event_service -p no:cacheprovider || echo FAIL; done` prints no `FAIL` (the unmodified tree fails about 1 to 3 times in 100; the architect runs it on the final tree).
4. `git status --short` lists only the two files of section 0.
5. Mutation check on a scratch copy, killer named for each: remove the helper call from `cleanup`; drain before `connection.close()` only if that breaks test 1 (say so); replace the barrier with `time.sleep(0.05)`; drop the timeout; swallow the timeout. A surviving mutant is reported.

## 6. Claims

| Claim | Where | Verified |
|---|---|---|
| `log_event` triggers the librarian pool | `domain/services/event_service.py` 78 to 80 | read |
| The pool is single-worker and shared with community detection | `librarian_service.py` 21; docstring of `trigger_community_detection` | read |
| The two flaky tests call `event_service.log_event` and use `mcp_contract.configure/cleanup` | `tests/test_mcp_lists_events_traces_v2.py` 37 to 48, 697 | read |
| 10 test files call `mcp_contract.cleanup` | `rg` | probed |
| No `conftest.py` exists in the repo | `ls` | probed |
| Drain removes the failures in a scratch experiment | 80 runs each, 2 failures versus 0 | measured by the architect |
