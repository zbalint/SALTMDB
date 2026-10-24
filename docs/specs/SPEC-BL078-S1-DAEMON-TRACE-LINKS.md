# SPEC-BL078-S1-DAEMON-TRACE-LINKS

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-078, slice S1 of S1 to S6 (daemon links a memory write to its session's open trace at write time). Owner decision (2026-10-24): the three `capture_trace_*` tools leave the agent MCP surface; this slice only adds the daemon-side linking, so the existing tool path keeps working and nothing is removed yet. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `90df320` or later; `./verify` exits 0 at `90df320` (3638 passed, 10 skipped, 391 subtests passed; the BL-083 teardown flake can fail two tests in a run, rerun once). Targeted baseline: `.venv/bin/python -m pytest tests/test_trace_service.py tests/test_trace_mcp_tools.py tests/test_db_write_coordinator.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `trace_service` functions on a temp database; `dispatch.dispatch_tool(tool, kwargs, coordinator)` with a started real `DbWriteCoordinator` (the pattern of `tests/test_consolidation_batch_v2.py` and `tests/test_batch_services_v2.py`).
- Scope (may edit): `src/saltmdb/domain/services/trace_service.py`, `src/saltmdb/daemon/dispatch.py` (the mutating branch of `_dispatch_tool_inner`, lines ~985 to 1010, plus two small module-level private helpers, `_written_entity_ids` and `_link_trace_writes`), `tests/test_trace_service.py` (new tests), `tests/test_daemon_trace_links.py` (new).
- Does not touch: `src/saltmdb/mcp/tools.py` (the adapter's pending-write queue and the three `capture_trace_*` tools stay as they are), `daemon/protocol.py`, `daemon/server.py`, `daemon/client.py`, the schema, any hook or config file, any other service.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Today a memory write is attached to its conversation trace in two steps that both cross the MCP boundary: the adapter queues `(tool, entity_id)` in a process-local deque after each successful write, and a harness `mcp_tool` hook later calls `capture_trace_memory_link`, which drains the deque and posts one RPC per entity. Once the capture tools leave the agent surface (BL-078) the hook cannot carry the adapter's queue, so the daemon, which already sees every write together with the writer's `agent_session_id`, links the write itself. This slice adds that, additively and idempotently, so the old path can keep running until S6 removes it.

Decisions already taken (do not reopen): link AT WRITE TIME from the daemon, not by reconstructing writes from a time window at completion (a window cannot tell operations apart, loses a second update of the same entity inside one turn because the content hash is read once, leaks `update_memory_metadata` writes, and loses writes after the turn's `Stop`); the old and the new path may both link the same write because `INSERT ... ON CONFLICT(trace_id, entity_id, content_hash) DO NOTHING` makes the second a no-op.

Behaviour that changes on purpose: a write made when its session has no pending trace is not linked at all (the adapter queue today attaches it to the next turn's link call). Also: (2) the daemon has no capture flag, so if capture is later disabled at the adapter, a pending trace younger than the abandonment timeout (one hour) still receives links from this path, whereas today `_queue_trace_write` checks `is_trace_capture_enabled()`; (3) `capture_trace_memory_link` may report `linked: false` for a write the daemon already linked. Capture is opt-in, so with no trace row nothing changes.

## 2. Decisions

- D1. New `trace_service.link_session_writes(conn, agent_session_id, agent_id, tool_name, entity_ids) -> int`: on the caller's connection (it opens none and starts no transaction), find the session's newest pending trace (`WHERE agent_session_id = ? AND agent_id = ? AND status = 'pending' ORDER BY created_at DESC, rowid DESC LIMIT 1`; the lookup uses the `agent_session_id` prefix of `idx_traces_session_harnessturn`), and insert one `trace_memory_links` row per entity id with the same rules as `capture_trace_memory_link` (same content hash read at that moment, same operation classification, same `ON CONFLICT DO NOTHING`). `tool_name` is typed `LinkOperation`; any other value is a programming error and raises `ValueError` (the dispatch caller's catch of D5 logs it). Returns the number of rows inserted. No pending trace, an empty id list, or an unknown entity id inserts nothing for that item and is not an error.
- D2. The insert logic is shared, not copied: extract the per-entity body of `capture_trace_memory_link` (the entity read, the `store_memory_new`/`store_memory_update` classification and the `INSERT`) into one private function `_link_entity(c, trace_id, entity_id, tool_name)` that returns `"unknown_entity"` or `(operation, inserted)`; both `capture_trace_memory_link` (which keeps its trace lookup and its `UNKNOWN_TRACE`/`UNKNOWN_ENTITY_ID`/`INVALID_WRITE_OPERATION` envelopes) and `link_session_writes` call it. `capture_trace_memory_link`'s signature, results and error codes are unchanged.
- D3. Daemon hook point: in `_dispatch_tool_inner`, for the four tools `store_memory`, `revise_memory`, `supersede_memory` and `consolidate_memories`, after the service call returns a result whose `status` is `ok`, and only when `kwargs` carries a non-empty `agent_session_id` and a non-empty `agent_id`, call `link_session_writes` INSIDE THE SAME coordinator job as the write (so the write and its link commit or roll back together; for `store_memory` that is the existing `store_job`, for the other three the existing lambda, which becomes a small `def`; that closure is SHARED by every mutating tool, including `log_event`, `manage_relation` and `archive_memory` with their coordinator kwarg, so it must read `kwargs` after the coordinator re-binding exactly as the lambda does today, and `_link_trace_writes` returns immediately for every tool outside the four), wrapped in a `savepoint` so a failing link rolls back only itself. All of it lives in ONE module-level helper, `_link_trace_writes(conn, tool, kwargs, result) -> None`, which holds the guard (the four tool names, `status == "ok"`, non-empty `agent_session_id` and `agent_id`), the savepoint and the try/except; each job calls it as a single statement right after `fn(...)` returns, so `_dispatch_tool_inner` gains no branch (it is at McCabe 7 and about 12 cognitive today, and an inline version would pass Sonar's 15 and ruff's 12). The call form is `trace_service.link_session_writes(...)` (the module attribute, never `from ... import`), so tests can patch it.
- D4. The written entity ids come from the RAW service result that the daemon sees, not from the adapter's public remap: `store_memory` reads `data["id"]`; `revise_memory` and `supersede_memory` read `data["new_id"]`; `consolidate_memories` reads `data["items"][i]["memory_id"]` for every item. One small private function (`_written_entity_ids(tool, result) -> list[str]`) in `dispatch.py` holds these rules; a missing or non-string value yields no id. `consolidate_memories` is read on the `items` path only (the shape `_consolidation_batch_result` returns); the other two daemon paths of `_dispatch_consolidate_memories` (`consolidations`, and the singular `parent_ids` form) are not reachable from the v2 MCP tool and yield no ids: do not extend the parser to them.
- D5. A failing link never fails or rolls back the write: the exception is caught around the savepoint, logged at WARNING with `exc_info` (tool, session id, entity ids), the write result is returned unchanged, and no partial link row remains (the savepoint rolls back the rows already inserted for the same write). This is deliberate degraded behaviour for a best-effort provenance record, like the telemetry submission in the same file; say so in a comment. One documented exception: if SQLite cannot roll back to the savepoint, `savepoint()` falls back to a full `ROLLBACK` (`db/connection.py` 155 to 160) and the job fails as any failed write does; do not write a test for it.
- D6. The daemon-side linking has no feature flag: the existence of a pending trace is the gate (traces only exist when capture is enabled at the adapter).
- D7. `agent_id` must match the trace's `agent_id` (owner isolation, as today's `capture_trace_memory_link`).
- D8. No change to the result envelope of any write tool (no new key, no new warning).

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/domain/services/trace_service.py` | D1, D2: the shared insert function; `link_session_writes`; `capture_trace_memory_link` calls the shared function. |
| `src/saltmdb/daemon/dispatch.py` | D3 to D5: `_written_entity_ids` and `_link_trace_writes`; one statement calling the latter inside each of the existing coordinator jobs of the four tools. |
| `tests/test_trace_service.py` | Section 4, service level. |
| `tests/test_daemon_trace_links.py` | Section 4, dispatch level with a real coordinator. |

## 4. Tests (written first, red on the old code)

Service level (`tests/test_trace_service.py`, temp database, existing fixtures):
1. `link_session_writes` links a new `store_memory_new` and, after an update of the same entity, a `store_memory_update` with the new hash (two rows, two hashes); `revise_memory`, `supersede_memory` and `consolidate_memories` each link with their own operation literal.
2. No pending trace, a completed trace only, an `agent_id` mismatch, and an unknown entity id: nothing inserted, no exception. An unsupported `tool_name` raises `ValueError`.
3. Two pending traces for one session (an unfinished older turn and the current one): the newest one gets the link.
4. Idempotence: calling twice, and calling after `capture_trace_memory_link` for the same write, inserts one row.

Dispatch level (`tests/test_daemon_trace_links.py`, real started `DbWriteCoordinator`, `dispatch.dispatch_tool`): expected values are literals taken from a run, ids excepted.
5. A `store_memory` call with `agent_session_id` and `agent_id` for a session that has a pending trace: the write result is unchanged (compare with a run on the unchanged code for the same input), and `get_trace`-level data (or a direct `trace_memory_links` read through the service) shows exactly one link with the entity id from `data["id"]`.
6. The same for `revise_memory` and `supersede_memory` (link on `data["new_id"]`, not the predecessor) and for a two-item `consolidate_memories` (two links).
7. A write without `agent_session_id`, with an empty one, or for a session without a pending trace: no link, result unchanged. For the first two, patch `trace_service.link_session_writes` (module attribute) with a spy and assert it was NOT called (an empty id matches no trace anyway, so only the spy kills a mutant that drops the guard).
8. Atomicity and degradation: patch `trace_service._link_entity` (the module attribute) so that it raises on the SECOND entity of a two-item `consolidate_memories`; assert that the write committed, that NO link row exists (the first insert was rolled back by the savepoint), that a WARNING was logged (`assertLogs` on the `dispatch` logger) and that the result equals the unchanged-code result. A raise on the first entity would prove nothing about the savepoint. Also: a write the service rejects (`status` not `ok`) creates no link and the same spy is not called (a rejected result carries no id, so only the spy kills a mutant that skips the status check).
9. Both paths on one write: perform the dispatch write, then call the existing `capture_trace_memory_link` service for it: one link row, no error.

Which of these an existing test already pins is named by the developer; the new tests are red on the old code unless a test is a contract test (item 9 and the unchanged-result part of 5 and 7 pass on the old code, name them).

## 5. Out of scope

The hello hints, the `trace_start`/`trace_complete` RPC methods and the session resolution (S2); any hook, config or harness change (S3, S4); removing the tools or the adapter queue (S6); `update_memory_metadata`, `archive_memory`, `manage_relation`, `set_core_memory`, `review_core_memory` (not linked today either).

## 6. Acceptance

1. `./verify` exits 0.
2. The section 4 tests exist, are red on the unchanged code where stated, and pass after the change (the developer reports both runs; the architect reruns on the final tree).
3. `git status --short` lists only the four files of section 0.
4. Mutation check on a scratch copy, killer named for each: read `data["id"]` for `revise_memory`; link before the write succeeded (status not checked); skip the savepoint; let a link failure propagate; drop the `agent_id` match; pick the oldest pending trace; classify `store_memory` as always new; skip the empty-`agent_session_id` guard; link outside the coordinator job. A surviving mutant is reported.
5. Design read (architect and consultant): the shared insert function has one responsibility; no copy of the classification logic; `dispatch.py` gains two small helpers (about McCabe 5 each, 2 and 4 parameters) and `_dispatch_tool_inner` gains no branch (the architect measures it with ruff's C901 and a hand count before and after).

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| Raw result keys: `store_memory` `data.id`; `revise_memory`/`supersede_memory` `data.new_id`; `consolidate_memories` `data.items[].memory_id` | `lifecycle.py` ~817 (`envelope_ok({"old_id", "new_id", ...})`); `relation_service._consolidation_batch_result` 2859 to 2873; probe of `store_memory` on a temp DB | read; `store_memory` probed |
| Every dispatcher forwards `agent_session_id` to the service (store 305, replacement 589, consolidate `items` 606); the adapter injects `agent_session_id` and `agent_id` for the four tools (`mcp/tools.py` 317 to 336) | consultant read | read; the `agent_id` injection set not re-read |
| `_execute_job` runs `job.fn` inside `write_transaction_retrying` on the coordinator connection, so a link inside the job shares the write's transaction; a lock retry re-runs the whole job and the link is idempotent | `db_write_coordinator.py` ~204 to 232 | consultant read |
| Several pending traces per session exist when a turn's Stop never ran (sweep only for ended sessions or older than one hour); `created_at` is same-format UTC ISO text so order is chronological | `trace_service.py` 41 to 86 | consultant read |
| The adapter remaps raw keys AFTER the daemon (`id`→`memory_id`, `new_id`→`successor_memory_id`) | `mcp/tools.py` ~1440 to 1460, 1368 to 1386 | read (consultant and architect) |
| The adapter injects `agent_session_id` for `store_memory`, `revise_memory`, `supersede_memory`, `consolidate_memories`, `log_event`, `update_memory_metadata` and the capture tools | `mcp/tools.py` 323 to 336 | consultant read |
| A coordinator job runs on one writer thread in one transaction; `submit` inside a transaction runs inline | `daemon/db_write_coordinator.py` 83 to 105 | read |
| `trace_memory_links` is unique on `(trace_id, entity_id, content_hash)` and `write_operation` is limited to five literals | `db/schema.py` 1062 to 1075 | read |
| The pending-trace lookup can use `idx_traces_session_harnessturn(agent_session_id, harness_turn_id)` | `db/schema.py` 1052 to 1055 | read; plan not probed (EXPLAIN QUERY PLAN to be shown by the developer) |
| No test reaches the daemon's four mutating tools through `dispatch_tool` with a pending trace today | `rg dispatch_tool tests` (10 files) | partly verified: the developer greps those files for `agent_session_id` and `conversation_traces` and reports any hit |
