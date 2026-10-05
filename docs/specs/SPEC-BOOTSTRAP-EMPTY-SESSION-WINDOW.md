# SPEC-BOOTSTRAP-EMPTY-SESSION-WINDOW

## 0. Status

**LOCKED** (2026-10-03). Backlog item BL-011. Context: `saltmdb-bootstrap-empty-window-2026-10-03`.

- Location/branch: main checkout `<repo>`, branch `develop`. Use the repo `.venv`, never system python.
- Test seams: `agent_sessions.get_recent_sessions_for_cwd(conn, cwd, limit=10, *, with_content=None)` (real SQLite via `init_db`, as in `tests/test_agent_sessions.py`), `session_digest_service.render_last_session_digest(conn, cwd)` and `session_digest_service.render_session_digest(conn, cwd, max_chars)` (as in `tests/test_session_digest_service.py`).
- Scope (may edit): `src/saltmdb/db/agent_sessions.py` (`get_recent_sessions_for_cwd` and its docstring only), `src/saltmdb/db/schema.py` (add exactly one index string to the performance-index list at L1185-1199), `src/saltmdb/domain/services/session_digest_service.py` (the two `get_recent_sessions_for_cwd` call sites at L37 and L246, plus the `render_last_session_digest` docstring), `tests/test_agent_sessions.py` (add tests only), `tests/test_session_digest_service.py` (add tests only).
- Does not touch: `get_last_session_for_cwd`, `record_session`, `close_session`, `reconcile_orphaned_sessions`, the hooks (`hooks/**`), the daemon and MCP layers (`src/saltmdb/daemon/**`, `src/saltmdb/mcp/**`), every other table or index in `schema.py`, `docs/architecture.md`, `INSTALL.md`, `docs/BACKLOG.md` (architect updates it).
- Developer leaves everything uncommitted, unstaged and unmerged; architect reviews and commits.
- Resource rule (owner, 2026-10-03): other agents run test suites on this 7.9 GiB host for other projects, and a parallel full run produces timing and memory-bound failures. See section 6 for the gate procedure. Do not start `./verify` or a full `pytest` without the preflight in section 6.

## 1. Why

The session-start bootstrap shows a last-session memory digest and a last-exchange handover. Both read the 10 newest `_agent_sessions` rows for the working directory (`get_recent_sessions_for_cwd` default `limit=10`, called at `session_digest_service.py:37` and `:246`) and then look for a session with content (memories for the digest, a trace for the handover) only inside that window.

Failure (owner-reported, 2026-10-03): a test project's agent started more than 10 sessions that produced nothing; the next real session received no bootstrap at all. With 10 content-free rows at the top, both functions return empty, even though an earlier session with real memories and traces exists. The failure is silent.

Earlier investigation found the same class of bug for a single concurrent empty sibling session, and the fix added a walk-back past content-free sessions. It kept the fixed 10-row window, so it only holds up to nine empty sessions. The test `test_empty_envelope_when_all_recent_sessions_are_content_free` (`tests/test_session_digest_service.py`) documents "within the lookback window" as an assumption.

Decision (owner approved, 2026-10-03): filter sessions by content in SQL, then apply the limit, so empty sessions can never push a real one out of the window. Raising the limit was rejected (it only moves the cutoff).

Grounding probes (2026-10-03, throwaway temp DB with 20,000 entities and 1,000 empty sessions, scratchpad scripts only, not committed; figures are indicative, not the real database):

- The current per-session probe `WHERE (agent_session_id = ? OR last_touched_session_id = ?) AND status != 'archived'` plans as `SCAN entities` because `last_touched_session_id` has no index (the only session index is `idx_entities_agent_session` on `agent_session_id`). 10 empty sessions took 0.1s; 1,000 took about 10s.
- A single `EXISTS` over the `OR` form took about 3.5s over 1,000 sessions; adding a partial index on `last_touched_session_id` alone made it no faster (about 11.8s) because SQLite still scans for an `OR` across two columns.
- Splitting the `OR` into one `EXISTS` per column plus the new partial index took 0.0012s over 1,000 sessions (plans: `SEARCH e USING INDEX idx_entities_agent_session`, `SEARCH e USING INDEX <new index>`). The split alone without the index took about 10s. Both parts are required.
- Traces need no schema change: `conversation_traces` has a unique index on `(agent_session_id, harness_turn_id)` that serves `agent_session_id = ?`. Its plan: `SEARCH t EXISTS USING COVERING INDEX idx_traces_session_harnessturn (agent_session_id=?)`.

## 2. `src/saltmdb/db/agent_sessions.py` — `get_recent_sessions_for_cwd`

Current (L125): `def get_recent_sessions_for_cwd(conn: sqlite3.Connection, cwd: str, limit: int = 10) -> list[dict]:`, selecting from `_agent_sessions WHERE cwd = ? ORDER BY started_at DESC LIMIT ?`.

New signature. Keep `conn`, `cwd`, `limit` positional order; add a keyword-only parameter:

```python
def get_recent_sessions_for_cwd(
    conn: sqlite3.Connection,
    cwd: str,
    limit: int = 10,
    *,
    with_content: str | None = None,
) -> list[dict]:
```

Behavior:

1. `with_content=None` (default): identical to today. Same SQL semantics, same returned keys (`session_id`, `started_at`, `agent_id`, `ended_at`, `ended_reason`), same ordering. Existing callers and tests are unaffected.
2. `with_content="memories"`: only sessions that created or touched at least one non-archived entity. A session qualifies if an entity exists with `agent_session_id = s.session_id` and `status != 'archived'`, or an entity exists with `last_touched_session_id = s.session_id` and `status != 'archived'`. No `agent_id`, `scope` or `memory_type` filter (this matches the existing digest query in `render_last_session_digest`).
3. `with_content="traces"`: only sessions with at least one row in `conversation_traces` where `agent_session_id = s.session_id`. No status filter.
4. Any other non-`None` value raises `ValueError` naming the accepted values. Do this check before touching the database.
5. The content filter is applied inside the same query, before `ORDER BY started_at DESC LIMIT ?`, so `limit` counts qualifying sessions only.

Required SQL shape (the performance result in section 1 depends on it). Use the table alias `s` for `_agent_sessions` and keep the two entity conditions as two separate `EXISTS` subqueries joined by `OR`, never as one subquery with an `OR` inside:

```sql
-- memories predicate
(EXISTS (SELECT 1 FROM entities e WHERE e.agent_session_id = s.session_id AND e.status != 'archived')
 OR EXISTS (SELECT 1 FROM entities e WHERE e.last_touched_session_id = s.session_id AND e.status != 'archived'))
-- traces predicate
EXISTS (SELECT 1 FROM conversation_traces t WHERE t.agent_session_id = s.session_id)
```

The literal `e.status != 'archived'` text must match the partial index definitions in section 3 exactly, or SQLite will not use them.

Implement the two predicates as a module-level constant named `_CONTENT_PREDICATES` (a `dict[str, str]` keyed `"memories"` and `"traces"`). The SQL text is built from this fixed dict only, never from a caller string, so no value reaches the SQL except through `?` parameters (`cwd`, `limit`). Do not add a second helper function (Coding Standards 4 and 14).

Docstring: replace the sentence that says the helper lets a caller walk backward past content-free sessions with a description of `with_content`. Keep the reference to `session_digest_service.render_last_session_digest`.

## 3. `src/saltmdb/db/schema.py` — one index

Add this string to the performance-index list, directly after the existing `idx_entities_agent_session` line (L1195):

```python
"CREATE INDEX IF NOT EXISTS idx_entities_last_touched_session ON entities(last_touched_session_id) WHERE status != 'archived'",
```

- The list runs inside `init_db` on every start with `CREATE INDEX IF NOT EXISTS`, after the `last_touched_session_id` column migration (L489), so existing databases get the index on their next start with no other migration. Do not add a `PRAGMA user_version` gate.
- Do not change any other index or table.
- Deployment note, not a spec item: a running MCP server only gets the index (and the new query) after it restarts from a checkout containing this change.

## 4. `src/saltmdb/domain/services/session_digest_service.py`

1. L37 in `render_last_session_digest`: `candidates = agent_sessions.get_recent_sessions_for_cwd(conn, normalized_cwd, with_content="memories")`.
2. L246 in `render_session_digest`: `candidates = agent_sessions.get_recent_sessions_for_cwd(conn, os.path.realpath(cwd), with_content="traces")`.
3. Update the `render_last_session_digest` docstring paragraph that describes walking back through recent sessions: state that the lookup now selects only sessions that created or touched a non-archived memory, so any number of content-free sessions (own or another agent's) cannot shadow an earlier one. Keep the reference to the earlier investigation.

Do not change the loop bodies. The `if candidate_rows:` check in `render_last_session_digest` and the `trace is not None` check in `_render_handover` stay as defensive checks. Do not change `HANDOVER_MAX_SESSIONS`, the render functions, or the default `limit`.

## 5. Tests

All new tests use real SQLite through `init_db(tmp)`, no mocks. Reuse the existing `_mk_entity`, `_session` and `_trace` helpers where the test file already has them; in `tests/test_agent_sessions.py`, add a small local entity-insert helper mirroring `TestSessionDigestService._mk_entity` if none exists.

`tests/test_agent_sessions.py` (add only):

1. `with_content="memories"` skips twelve newer content-free sessions and returns the older session that has a non-archived entity (the owner's repro, more than 10 empty sessions).
2. `with_content="memories"` also returns a session that only touched an entity, `last_touched_session_id` set and `agent_session_id` set to a different session.
3. `with_content="memories"` ignores a session whose only entity is `archived`.
4. `with_content="traces"` skips twelve newer sessions with no trace and returns the older session that has a `conversation_traces` row.
5. `limit` counts qualifying sessions only: with five qualifying sessions interleaved with empty ones and `limit=3`, exactly three qualifying sessions come back, newest first.
6. `with_content=None` still returns empty and non-empty sessions alike (default behavior unchanged); an unknown `with_content` value raises `ValueError`.
7. Plan test: run `EXPLAIN QUERY PLAN` on the memories query built from `agent_sessions._CONTENT_PREDICATES["memories"]` inside the same query shape as the function, and assert the plan text contains both `idx_entities_agent_session` and `idx_entities_last_touched_session` and contains no `SCAN e`. If the function builds its query so the test cannot reuse it directly, expose the full SQL as a private module constant (for example `_RECENT_SESSIONS_SQL`) instead; keep it one place.

`tests/test_session_digest_service.py` (add only):

8. `render_last_session_digest`: twelve newer empty sessions registered after one older session with an entity; the digest still names the older session and its memory.
9. `render_session_digest`: twelve newer sessions with no traces and one older session with a trace; the output contains the `<saltmdb-session-handover>` block and the older session's messages.
10. The existing `test_empty_envelope_when_all_recent_sessions_are_content_free` stays unmodified and green.

## 6. Verification procedure (resource-bounded)

Memory preflight before any full run (host is shared with other projects' agents):

```
free -m | head -2        # require 'available' of at least 3000 MB
pgrep -af 'pytest|verify' # require no other pytest or verify process
```

If either check fails, wait and re-check; do not start a full run. Run the full gate once, alone, with nothing else of yours running. Do not run two gates in parallel, and do not run the focused tests while the full gate runs.

If the full gate fails on a timing or memory test (`tests/test_adapter_signal_shutdown.py` SIGINT test, `tests/test_run_retrieval_bakeoff.py::test_apply_memory_ceiling_contains_a_runaway_allocation`, or any other test you did not touch), apply the circuit breaker: re-run only that one test in isolation once, report both results, and do not loop. These two failed in earlier runs under concurrent load and passed alone (an earlier incident record); a failure of a test this change touc...

## 7. Out of scope

- Raising, removing or making the default `limit` configurable.
- `get_last_session_for_cwd` and its callers and tests.
- Any change to `render_last_session_digest` output format, `_render_handover`, `HANDOVER_MAX_SESSIONS`, or the hook scripts and their docs.
- Excluding the calling session from the lookup (a separate idea from the earlier investigation; the content filter already skips the caller's own empty row).
- Adding an index on `conversation_traces`, `_agent_sessions` or any other table.
- Running `ANALYZE`, `VACUUM`, or changing PRAGMAs.
- `docs/architecture.md` L162 already says the digest walks back past content-free sessions and stays accurate.

## 8. Acceptance

Run after implementation, in this order, at the main checkout with the repo `.venv`:

1. Baseline (already run at architect lock time on `c417b1d`, clean tree): `.venv/bin/pytest -q tests/test_session_digest_service.py tests/test_agent_sessions.py` gave `45 passed`. Re-run after implementation: expect 45 plus the new tests, all passed, zero failures.
2. `.venv/bin/ruff check src tests` and `.venv/bin/ruff format --check src tests`: exit 0.
3. `.venv/bin/mypy src`: exit 0.
4. Section 6 preflight, then `./verify` once: exit 0. The architect's last full baseline was `1972 passed, 142 subtests` at `3dcd37d`; no fresh full baseline was run for this spec because of host memory pressure, so the developer's full gate is the first run on this tree. Report the counts.
5. `git diff --stat` lists only the five files in section 0 scope, and `git status --short` shows no untracked files outside them.
6. Informational, not pass or fail: a one-off scratch script in the scratchpad directory (not committed) that builds a temp DB with about 20,000 entities and 1,000 empty sessions, then times `get_recent_sessions_for_cwd(conn, cwd, with_content="memories")`; report the elapsed time next to the section 1 figure (about 0.001s). Do not add it as a test (timing assertions are flaky on a shared host).
