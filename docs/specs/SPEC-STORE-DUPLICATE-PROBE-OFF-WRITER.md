# SPEC-STORE-DUPLICATE-PROBE-OFF-WRITER

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-023. Shared `context_id`: `store-dedup-off-writer-2026-10-11`.
- Baseline: `c88d316` on `develop`; `./verify` exit 0, 2192 passed, 1 skipped, 172 subtests.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `memory_service.store_memory` and `memory_service.apply_duplicate_probe` in direct mode
  (no coordinator); `dispatch_tool("store_memory", ...)` with a real `DbWriteCoordinator` on a temp
  database, as `tests/test_telemetry_service.py` (class `TestDispatchToolTelemetryWiring`, L100; coordinator at L110) and
  `tests/test_entity_id_prefix_resolution.py` (L230) set one up.
- Scope (may edit):
  - `src/saltmdb/domain/services/memory_service/write.py` (L573-600 signature, L820-865, L880-899)
  - `src/saltmdb/domain/services/memory_service/__init__.py` (one import and one `__all__` entry)
  - `src/saltmdb/daemon/dispatch.py` (`_dispatch_tool_inner`, L741-748, and `_dispatch_store_memory`,
    L244-278)
  - `tests/test_store_duplicate_probe_off_writer.py` (new)
  - `docs/architecture.md` (one sentence)
- Does not touch: `duplicates.py`, `db_write_coordinator.py`, `connection.py`,
  `relation_service.py` (`consolidate_memories` has the same probe and stays as is, section 7), the MCP
  adapter, `mcp/tools.py`, the viewer.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

In the daemon, `store_memory` is a mutating tool, so `dispatch_tool` submits the whole
`memory_service.store_memory` call as one coordinator job (`dispatch.py` L741-746).
`DbWriteCoordinator._execute_job` runs that job inside `write_transaction_retrying`, which opens
`BEGIN IMMEDIATE`; the inner `write_transaction_retrying` call in `store_memory` (`write.py` L841) is a
pass-through because the connection is the coordinator's (`connection.py` L162-166). So the new row
commits only when the whole closure returns, and the near-duplicate probe
(`check_duplicate_memories`, `write.py` L851-866: FTS, up to about 30 cross-encoder pairs, and a first
reranker load that takes seconds) runs inside the open transaction. It holds the writer queue and the
SQLite write lock: every other write (trace capture, `log_event`) waits for it.

The probe is advisory (its result only fills `duplicate_candidates` and a `NEAR_DUPLICATE` warning;
exact-hash duplicates are rejected earlier), is read-only, and opens its own connection when
`db_connection` is None (`duplicates.py` L45-49, L213-215). BL-018 (7af83c6) made it cheaper but did
not move it.

Decision: run the probe after the job returns, on the RPC handler thread, so the row is committed first
and the writer is free.

Rejected: (B) submitting the probe as a background coordinator job (it still runs on the writer thread
and cannot return its result in the response); (C) measuring only (the first-store reranker load is
unbounded by BL-018); splitting `store_memory` into two functions with a `(envelope, request)` tuple
return (about twenty early returns would change; the keyword-only sink below changes none).

## 2. Decisions

- D1. `store_memory` gains a keyword-only parameter `probe_sink: list | None = None`. When it is
  `None` the behaviour is exactly today's (probe inline, same response).
- D2. When `probe_sink` is a list and the store created a new memory (`not was_existing`), the function
  skips the probe, leaves `data.duplicate_candidates` as `[]`, adds no `NEAR_DUPLICATE` warning, and
  appends exactly one dict to `probe_sink` with the keys `title`, `content` (the redacted content, not
  the caller's), `agent_id`, `tags`, `context_id`, `exclude_ids` (a one-item list holding the new id)
  and `db_path`, where `db_path` is `db_path or get_db_path()` resolved at append time (a direct-mode
  caller may pass a `db_connection` and no `db_path`, and `None` would make the later probe open the
  default database). When no probe is due (early returns, `was_existing`), the list stays empty.
- D3. A new public function `apply_duplicate_probe(envelope, request) -> None` runs
  `check_duplicate_memories(title=..., content=..., agent_id=..., tags=..., context_id=...,
  exclude_ids=..., db_connection=None, db_path=...)` and, when it returns `potential_duplicates`, sets
  `envelope["data"]["duplicate_candidates"]` and appends the `NEAR_DUPLICATE` warning at the end of
  `envelope["warnings"]`. A result without candidates, or one carrying an `error` key, changes nothing.
  The warning body is built by one shared helper, `_near_duplicate_warning(candidates)`, used by both
  the inline path and `apply_duplicate_probe` (no copy).
- D4. In the daemon, `_dispatch_tool_inner` handles `store_memory` separately: it creates
  `sink = []`, submits `lambda _conn: (sink.clear(), fn(**{**kwargs, "coordinator": coordinator,
  "probe_sink": sink}))[1]` to the coordinator (the job body may re-run on lock contention, so it
  clears the sink first), and after the job returns calls `apply_duplicate_probe(result, sink[0])` on
  the calling thread when `sink` is non-empty, `result` is a dict and `result.get("status") == "ok"`
  (the sink is filled before the rest of the response is assembled, so a later exception yields a
  rejected envelope with a non-empty sink). Any exception from
  `apply_duplicate_probe` is caught and logged with `logger.warning(..., exc_info=True)`; it never
  turns a committed store into an error response. `probe_sink` is injected only by this code; a client
  value for it is overwritten.
- D5. The telemetry timer in `dispatch_tool` still wraps `_dispatch_tool_inner`, so the recorded
  latency includes the probe.
- D6. The response a daemon client gets from `store_memory` is identical in shape, content and
  `warnings` order to the direct-mode response for the same inputs.

## 3. Changes per file

### 3.1 `write.py`

- Add the helper `_near_duplicate_warning(duplicate_candidates: list[dict]) -> dict` holding the
  warning construction now inline at L884-899 (the `warning("NEAR_DUPLICATE", ..., {...})` call); the
  inline path calls it.
- Add `probe_sink: list | None = None` after `detail_memory_ids` in the keyword-only section.
- At L851-866: if `probe_sink is not None`, append the D2 dict instead of calling
  `check_duplicate_memories`; otherwise unchanged. Everything else in the function stays.
- Add `apply_duplicate_probe` after `store_memory`. Import `check_duplicate_memories` where the inline
  path already imports it (`from .duplicates import check_duplicate_memories`, L853), inside the
  function.

### 3.2 `memory_service/__init__.py`

Import `apply_duplicate_probe` beside `store_memory` (L49) and add it to `__all__`.

### 3.3 `dispatch.py`

- `_dispatch_store_memory` (L244-278): add `probe_sink=kw.get("probe_sink")` to the call.
- `_dispatch_tool_inner` (L741-748): add the `store_memory` branch of D4 before the generic mutating
  branch; keep `log_event` and `manage_relation` as they are.

### 3.4 `docs/architecture.md`

One sentence in the `store_memory` / Quality Gate description: in the daemon the near-duplicate probe
runs after the store's transaction commits, on the request thread, not on the writer thread.

## 4. Tests

Written failing first, in `tests/test_store_duplicate_probe_off_writer.py`. Patch `reranker_service.get_model` and `memory_service._embed_pool.submit` as
`tests/test_cross_owner_dedup.py` (L222-226) does, so no model loads, but with a fake whose `rerank`
returns a score at or above `DEDUP_CROSS_ENCODER_THRESHOLD` (4.0, `config.py` L135):
`fake_model.rerank.side_effect = lambda _q, texts: [5.0] * len(texts)`. (The score-0.0 fake used by that
file finds no candidates, so tests 1, 3, 4 and 8 would pass or fail vacuously.) Every test passes its
own `db_path` (a temp file) or sets `SALTMDB_DB_PATH`; none may open the default database.

1. Direct mode, `probe_sink=None`: storing two near-identical memories returns the same
   `duplicate_candidates` and `NEAR_DUPLICATE` warning as today (compare with the shape asserted in
   `tests/test_store_duplicate_policy_phase5.py` L77-85).
2. Direct mode, `probe_sink=[]`, second near-identical store: `data.duplicate_candidates == []`, no
   `NEAR_DUPLICATE` warning, and the sink holds one dict with exactly the D2 keys, `content` equal to
   the redacted content and `exclude_ids == [the new id]`. A first store with an empty database leaves
   the sink holding one dict too; a rejected store (exact-hash duplicate) leaves it empty.
3. `apply_duplicate_probe(envelope, sink[0])` on the test 2 envelope produces `duplicate_candidates`
   and a `NEAR_DUPLICATE` warning equal to test 1's, with the warning last in `warnings`.
4. Daemon path through `dispatch_tool("store_memory", ...)` with a real coordinator: the second
   response matches the direct-mode response for the same inputs (D6), compared by the warning codes in
   order, the `duplicate_candidates` mapped to their titles, the `NEAR_DUPLICATE` detail's `guidance`,
   and the `data` keys and `effective` (ids and messages differ between two stores and are not
   compared), and the probe ran off the writer
   thread: patch `saltmdb.domain.services.memory_service.duplicates.check_duplicate_memories` (both
   call sites import it inside the function, so the patch is seen) with a wrapper that records
   `threading.get_ident()` and asserts it differs from `coordinator.writer_thread_id`
   (`db_write_coordinator.py` L77-78).
5. The new row is committed before the probe runs: inside the wrapper of test 4, read the new id over a
   fresh `sqlite3` connection opened with the repository's read-connection helper and assert it exists.
6. The writer is free while the probe runs: the wrapper of test 4 sets a `probe_entered` Event, then
   blocks on a `release` Event (timeout 20 s). The store call runs on a second thread. The test thread
   waits for `probe_entered` (timeout 20 s), runs `dispatch_tool("log_event", ...)`, asserts it returned
   while `release` is still unset, then sets `release`, joins the store thread and checks the store
   result (captured in a list).
7. A probe that raises inside `apply_duplicate_probe` (wrapper raises `RuntimeError`) leaves the
   `dispatch_tool` response as the successful store envelope with `duplicate_candidates == []`, and a
   `WARNING` record is logged (`assertLogs("saltmdb.daemon.dispatch", "WARNING")`).
   An envelope that is not `status == "ok"` (a rejected store, with a non-empty sink forced by the
   test) is left alone and the probe is not called.
8. A client-supplied `probe_sink` kwarg in the `dispatch_tool` call is overwritten (the response
   carries no `probe_sink` effect: duplicates are still reported).

## 5. Documentation

Section 3.4 only.

## 6. Verification procedure

`./verify` alone on the host.

## 7. Out of scope

`consolidate_memories`' probe (`relation_service.py` L1580): it sits deliberately between a snapshot
and the commit's recheck and is not a simple move. The existing wart that an exception after the write
inside the closure returns `INTERNAL_ERROR` although the transaction commits. `trigger_librarian`
placement. Any change to `check_duplicate_memories` itself.

## 8. Acceptance

1. `./verify` exits 0 with the new tests included.
2. `rg -n "probe_sink" src` lists only `write.py` and `dispatch.py`.
3. On an isolated daemon (temp home, a copy of a database in a gitignored scratch directory on the real
   disk, a free viewer port): the dedup probe loads its cross-encoder on the first store whose FTS
   pre-filter finds candidates, with or without `SALTMDB_RERANKER_MODEL`, so only the FIRST such store
   after daemon start is slow enough to overlap. Issue that store from one thread and a `log_event`
   from a second thread through its own client connection, log timings in the throwaway script, and
   show the `log_event` finishes before the store returns (corroborate with the `Process memory
   (reranker model loaded)` line's timestamp in `daemon.log`). Then store a second near-identical
   memory and show its `duplicate_candidates` and warning. If the first store was too fast to
   overlap, report that and rely on test 6.
4. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

- Step 5/10 re-grep: `check_duplicate_memories` and `NEAR_DUPLICATE` appear in `relation_service.py`
  (consolidate, out of scope), `orchestrator.py` (not the store path; confirm unchanged), `duplicates.py`,
  `write.py`, `__init__.py`, and the tests `test_consolidation_is_core_inheritance.py`,
  `test_bitemporal_relations.py`, `test_cross_owner_dedup.py`, `test_advanced_quality_features.py`,
  `test_store_duplicate_policy_phase5.py`; all call the public function in direct mode, which is
  unchanged (D1).
- Step 9: `coordinator.writer_thread_id` exists (L77-78). The job body can re-run on lock contention
  (`write_transaction_retrying`, `connection.py` L170-190), which is why D4 clears the sink first.
- Consultant review (m_1303): F1 reranker fake score, F2 db_path resolution and temp-db rule, F3
  comparison basis in test 4, F4 `status == "ok"` guard, F5 `probe_entered` Event, F6 and F7
  confirmed safe, 8.3 re-aimed at the first store after start.
- Re-grep at lock: `probe_sink` appears in D1, D2, D4, sections 3.1 and 3.3, tests 2, 4, 8 and
  acceptance 8.2, all describing the same keyword-only list parameter; the `db_path` wording in D2
  matches test 2's expectation; no section still says the second store is the one to overlap.
