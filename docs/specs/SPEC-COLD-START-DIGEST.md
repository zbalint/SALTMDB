# SPEC-COLD-START-DIGEST

## 0. Status

LOCKED 2026-10-11.

- Backlog: BL-034 (new, added by this spec's commit). Shared `context_id`:
  `session-start-digest-cold-start-2026-10-11`.
- Baseline: `develop` at `a1acf7b`, `./verify` exit 0 (run it before editing and record the counts).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `saltmdb.db.schema.init_db` (a temp DB, statement trace through
  `conn.set_trace_callback` or `EXPLAIN QUERY PLAN`), and the hook module's `bootstrap_text` /
  `main` with `run_cli` and the clock monkeypatched.
- Scope (may edit): `src/saltmdb/db/schema.py` (only the three backfill blocks named in section 3.1
  plus the three new `CREATE INDEX` statements before them), `hooks/saltmdb-session-start-bootstrap.py`,
  `hooks/tests/test_session_start_bootstrap.py`, `tests/test_db_schema*.py` (add one new test file
  `tests/test_init_db_backfill_plan.py` instead if no suitable file exists), `docs/BACKLOG.md`
  (add BL-034 as implemented, plus BL-035 from section 7).
- Does not touch: `daemon/`, `cli.py`, every other hook, `config.py`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Some sessions never receive the bootstrap digest. The session-start hook runs under a 15 s harness
limit; on a daemon cold start `saltmdb-cli bootstrap-digest` waits for the daemon, and the daemon
log shows `init_db_s` of 15.7, 23.8 and 31.7 s on cold starts (warm: 0.8 s).

Root cause, measured 2026-10-11 on a copy of a 119 MB database (BL-027 snapshot) stored on the
real ext4 disk, with the file evicted from the page cache (`posix_fadvise DONTNEED`):

- `init_db` took 4.1-4.9 s cold against 0.03-0.33 s warm. 3.4 s of it was SQL, in two statements
  that run on **every** start and full-scan the `entities` table (the table holds the 119 MB of
  `full_content`): the `embedding_status = 'archived'` backfill (2.6 s) and the `content_hash`
  backfill (0.8 s). The `context_id = project_id` backfill is a third scan of the same kind.
  Each matches zero rows in steady state; they scan because no index covers their predicate.
- Cold disk throughput on this host measured 14 MB/s (4 KiB direct reads) to 53 MB/s (sequential).
  After a WSL VM restart the disk is slower and busier, which scales the same scans to the
  observed 15-32 s. The database grew from 75 MB to 120 MB since mid-September, so the cost grows
  with it.
- Not the cause: cold import of `sqlite_vec`/`numpy` is 0.8 s; the vec0 `CREATE ... IF NOT EXISTS`,
  the FTS checks and the migrations gated on `PRAGMA user_version` are cheap.

The spawn storm (about 20 daemons in 1 s, 2026-10-09) is already fixed by BL-019 (`4bec86b`,
committed 2026-10-10, live since `3d7f0ea`); the daemon log shows one start per cold start since.
Nothing to do for it here.

Even with `init_db` fixed, a cold start still costs the pre-log interpreter/import time
(`startup_lag_s` 5-8 s) plus about 1-2 s, so the hook must not depend on the daemon being fast.
Today it runs three sequential CLI calls with a 10 s timeout each, under a 15 s limit, and prints
nothing unless the whole script finishes, so one slow call loses the sections that did succeed.

Rejected alternatives:

- Making the systemd service a requirement: the owner excluded it.
- Gating the backfills behind `PRAGMA user_version`: it drops the self-healing the backfills give
  after an old release writes a row, and needs a version bump. Partial indexes keep the semantics.
- Raising the harness hook timeout alone: not enough (cold starts of 16-34 s), and it is outside
  this repo.
- A new daemon-side warm-up or persistent-daemon default: out of scope, unrelated to the measured
  cause.

## 2. Decisions

- D1. Three partial indexes make the three steady-state backfills index scans over zero rows.
- D2. The three backfill statements keep their semantics and use `INDEXED BY`, because the planner
  otherwise prefers `idx_entities_status_updated` / `idx_entities_context` (measured).
- D3. The hook has one overall deadline of 13.0 s (monotonic) measured from process start. Each CLI
  call gets `min(10, remaining)` as its subprocess timeout; a call is skipped when `remaining < 1.0`.
- D4. The hook prints every section it gathered, even when a later call timed out or failed.
- D5. When the bootstrap digest is missing (timeout or failure) the hook adds a short
  `<saltmdb-digest-pending>` block telling the agent to run the resolved CLI command
  `bootstrap-digest` itself with the Bash tool, so the continuity is recovered within the session
  instead of lost.
- D6. `corpus-health` (the overdue-core nudge) is the lowest priority: it runs last and is skipped
  without a message when the budget is short.

## 3. Changes per file

### 3.1 `src/saltmdb/db/schema.py`

Add, immediately before the `embedding_status` backfill (the block starting
`# Backfill embedding_status = 'archived' ...`), three statements. Use exactly these names and
predicates (SQLite matches a partial index only when the query's WHERE implies the index's):

```python
conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_entities_archived_embedding_pending ON entities(id) "
    "WHERE status = 'archived' AND embedding_status IS NOT 'archived'"
)
conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_entities_hash_missing ON entities(id) "
    "WHERE content_hash IS NULL OR content_hash = ''"
)
conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_entities_context_backfill ON entities(id) "
    "WHERE context_id IS NULL AND project_id IS NOT NULL"
)
```

The `project_id` column is added by the earlier `_add_column_if_missing` loop, so the third index
is valid at that point; place the statements after that loop and after the existing guards. Keep
the surrounding `try/except sqlite3.OperationalError` style of each block (the first and third are
inside one already; put the index statements inside the same `try`, the second inside its own).

Rewrite the three queries, same meaning:

- `UPDATE entities INDEXED BY idx_entities_archived_embedding_pending SET embedding_status =
  'archived' WHERE status = 'archived' AND embedding_status IS NOT 'archived'`
  (`IS NOT` is the NULL-safe form of `!= ... OR IS NULL`; same rows).
- `SELECT id, full_content FROM entities INDEXED BY idx_entities_hash_missing WHERE content_hash
  IS NULL OR content_hash = ''`
- `UPDATE entities INDEXED BY idx_entities_context_backfill SET context_id = project_id WHERE
  context_id IS NULL AND project_id IS NOT NULL`

Do not change any other statement. The `idx_relations_unique_edge` drop/create and the
`relations` dedupe `DELETE` are measured at 0.05-0.6 s cold and stay as they are (section 7).

### 3.2 `hooks/saltmdb-session-start-bootstrap.py`

- Record `START = time.monotonic()` at import; `BUDGET_S = 13.0`; helper
  `remaining() -> float` returns `BUDGET_S - (time.monotonic() - START)`.
- `run_cli(*args)`: compute `limit = min(10, remaining())`; return `None` without spawning when
  `limit < 1.0`; pass `timeout=limit`. Behaviour otherwise unchanged (None on OSError, timeout,
  non-zero exit).
- `bootstrap_text`: keep the order bootstrap-digest, session-digest, corpus-health. If the
  bootstrap digest came back empty/None **and a CLI was resolved**, append a section
  `<saltmdb-digest-pending>` ... `</saltmdb-digest-pending>` with two or three plain lines: the
  digest could not be fetched in time (the SALTMDB daemon is probably still starting); run
  `<CLI> bootstrap-digest` with Bash now and read the result before continuing. `<CLI>` is the
  resolved `CLI` path. If no CLI was resolved, emit nothing extra (unchanged).
- Collected sections are always returned (they already are; keep it so when `run_cli` returns
  None mid-way, and do not raise).
- `main` is unchanged apart from using the new `bootstrap_text`.

## 4. Tests

Written failing first.

- `tests/test_init_db_backfill_plan.py` (or an existing schema test file): build a temp DB with
  `init_db`; insert 3 entities (one archived with `embedding_status='pending'`, one with
  `content_hash=NULL`, one with `project_id='p'` and `context_id` NULL) using raw SQL on a test
  connection to the temp file; run `init_db` again; assert the three rows were healed
  (`embedding_status='archived'`, a non-empty `content_hash`, `context_id='p'`). Then assert, with
  `EXPLAIN QUERY PLAN` of each of the three statements as written in section 3.1, that the plan
  text contains the matching index name and no `SCAN entities` without `USING INDEX`. Do not run
  the statements for the plan check against the live DB.
- Hook tests (extend `hooks/tests/test_session_start_bootstrap.py`, same monkeypatch style):
  1. `run_cli` times out on `bootstrap-digest` (returns None) but `session-digest` returns text:
     output contains the session digest and the `<saltmdb-digest-pending>` block naming the
     resolved CLI path and `bootstrap-digest`.
  2. Clock patched so `remaining()` is 0.5 on the third call: `corpus-health` is not invoked and
     the output still contains the first two sections.
  3. `subprocess.run` patched to record `timeout`: first call `10`, and a call made when
     `remaining()` is 4.0 receives `4.0`.
  4. `source == "compact"` still skips `session-digest` (existing test stays green).
  5. No CLI resolved: no pending block, empty output (existing behaviour).

## 5. Documentation

`hooks/README.md`: in the session-start hook entry, one sentence stating the 13 s overall budget
and the pending notice. `docs/BACKLOG.md`: add BL-034 (this work, status implemented) and BL-035
(section 7 follow-up, open).

## 6. Verification procedure

Default `./verify`. Do not start parallel full runs.

## 7. Out of scope

- BL-035 (open): `startup_lag_s` of 5-8 s before the daemon's first log line (interpreter and
  imports on a cold disk) is unmeasured in detail; the `idx_relations_unique_edge` drop/create and
  the `relations` dedupe scan on every start (0.05-0.6 s cold, grows with `relations`); BL-032
  model warm-up. Record, do not implement.
- Changing `DAEMON_SHUTDOWN_GRACE_PERIOD_S`, the systemd service, or any other hook.
- The unexplained 2026-10-09 18:11 UTC burst with no daemon start.

## 8. Acceptance

1. `git status --short` before editing is clean; `./verify` exit 0, counts recorded.
2. New tests fail before the change and pass after (record the red run).
3. `./verify` exit 0 after the change, counts recorded.
4. Cold measurement, on a copy, never the live database. Take a copy with
   `.venv/bin/saltmdb-cli snapshot` (or copy `~/.saltmdb/backups/*.db`) into `scratch-cold/` inside
   the repo (gitignored; it must be on the real disk, not `/tmp`, which is tmpfs here), run
   `init_db` once to create the indexes, evict the file with
   `os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)`, then time `init_db` in a fresh
   `.venv/bin/python -I` process. Expected: under 1.5 s, and the three backfill statements each
   under 0.1 s. Report the before number (about 4 s) from the same copy before the code change.
5. `git diff --stat` lists only the files in section 0 scope; `scratch-cold/` is not tracked.

## 9. Pre-lock gate notes

- Scope list vs acceptance: every file in section 0 appears in a section 3, 4 or 5 item;
  `tests/test_init_db_backfill_plan.py` is named once as the fallback for the schema test file.
- Index names and predicates are written in one place (3.1) and referenced by name in section 4.
- Code claims checked 2026-10-11: backfills at `schema.py` (embedding_status, content_hash,
  context_id blocks) scan without an index on the real 119 MB copy; the planner picked
  `idx_entities_status_updated` / `idx_entities_context` without `INDEXED BY` and the partial
  indexes with it (EXPLAIN QUERY PLAN run on the copy); cold run of the prototype: 0.039 s,
  0.010 s, 0.001 s against 2.6 s, 0.8 s, 0.7 s.
- Hook claim: `run_cli` uses `timeout=10` and `bootstrap_text` makes three sequential calls
  (`hooks/saltmdb-session-start-bootstrap.py`); harness limit 15 s is from the ~/.agents hook
  config as reported by the owner's side, not verified here.
- No consultant pass (consultant peer detached); locked on the gate alone.

## Amendment 1 (2026-10-11)

Section 0 "Scope (may edit)" omitted `hooks/README.md`, which section 5 requires editing. The
scope now includes `hooks/README.md` (the single sentence in section 5 only). Nothing else changes.

## Amendment 2 (2026-10-11)

Evidence (developer, same cold copy): after the index change `init_db` is still 2.4-3.0 s cold. The
profiler attributes 1.24 s to the `relations` dedupe `DELETE` and 0.83 s to the
`idx_relations_unique_edge` drop/create, both run on every start. Section 7 listed them as out of
scope on a measurement (0.05-0.6 s) that did not hold; the target in section 8.4 cannot be met
without them. They move into scope; the BL-035 follow-up keeps only the interpreter/import lag and
BL-032.

Changes (supersede the sections named):

- Section 0 scope: `src/saltmdb/db/schema.py` also covers the `relations` dedupe block and the
  `idx_relations_unique_edge` block (the two consecutive `try` blocks around them).
- Section 3.1 addition (D7): before those two blocks read
  `SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_relations_unique_edge'`.
  If a row exists and its `sql`, with all whitespace runs collapsed to one space, equals
  `CREATE UNIQUE INDEX idx_relations_unique_edge ON relations(source_id, target_id, predicate) WHERE valid_to IS NULL`,
  skip BOTH the dedupe `DELETE` and the drop/create (the partial unique index proves there are no
  duplicate active edges). Otherwise run both blocks unchanged. Keep the `try/except
  sqlite3.OperationalError` posture and the explanatory comments (add one line naming this guard).
- Section 4 addition: a test that (a) on a fresh `init_db` DB the index exists with the exact sql
  above, (b) a second `init_db` run issues no `DELETE FROM relations` and no `DROP INDEX` (statement
  trace via `set_trace_callback`), (c) a DB where the index is missing or has the old non-partial
  definition is migrated to the exact definition, with duplicate active edges collapsed first.
- Section 7: delete the first bullet's mention of the `relations` dedupe and the unique-index
  rebuild.
- Section 8.4 replaced: cold `init_db` (database, WAL and SHM evicted, fresh `python -I` process)
  under 1.5 s on the same copy, the three entity backfills each at most 0.25 s, and the relations
  guard at most 0.1 s. Report the numbers of three consecutive evicted runs. If `init_db` is still
  over 1.5 s with these met, report the profile instead of widening scope; the architect decides.
- Section 8.3 baseline: a `./verify` that hit the tool time limit before the pytest summary is
  recorded as "no exit-0 baseline"; the final `./verify` exit 0 with counts is the gate.
