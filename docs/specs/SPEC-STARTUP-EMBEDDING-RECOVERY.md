# SPEC-STARTUP-EMBEDDING-RECOVERY

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-042. Shared `context_id`: `daemon-startup-recovery-2026-10-11`.
- Baseline: `2b5fed8` on `develop`; `./verify` exit 0, 2202 passed, 1 skipped, 177 subtests passed.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `embedding_service.embedding_recovery_gap_count(conn)` and
  `embedding_service.reconcile_all_embedding_jobs(coordinator, ...)` against a temporary database
  (`init_db`, then raw-SQL `INSERT INTO entities` plus `enqueue_embedding_jobs_for_entity` and
  `enqueue_retrieval_embedding_job_for_entity`, the pattern of `tests/test_embedding_job_reconciliation.py`
  L18-24 and `tests/test_init_db_backfill_plan.py`); a real daemon subprocess on a temporary
  database, in the shape of `tests/test_daemon_server.py::TestDaemonSignalShutdown` (L738-797).
- Scope (may edit):
  - `src/saltmdb/domain/services/embedding_service.py` (new functions after L262)
  - `src/saltmdb/daemon/server.py` (`_DaemonState.__init__` L71, `_handle_run_backfill` L594-621,
    the startup block L933-1025, the ready log L1072-1088, `_shutdown_sequence` L1189)
  - `src/saltmdb/db/schema.py` (one entry in the `idx_entities_*` index list, after L1229)
  - `tests/test_startup_embedding_recovery.py` (new)
  - `docs/architecture.md` (one bullet after L122)
  - `docs/BACKLOG.md` (one new row, BL-042)
- Does not touch: the rest of `db/schema.py`, `db_write_coordinator.py`, `agent_sessions.py`,
  `EmbedJobScheduler`, `reconcile_embedding_jobs` and `reconcile_retrieval_embedding_jobs` bodies,
  `enqueue_*` functions, the hooks, `MIGRATION.md` (BL-034's partial indexes did not touch it either),
  any existing test file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

After the owner updated SALTMDB and restarted the WSL VM, the SessionStart hook again lost the
bootstrap digest: the daemon only became ready after 26.0 s against a 13 s hook budget. The earlier
fix (BL-034) worked for what it targeted: `init_db_s` fell from 16-32 s to 1.7 s. The cost moved: the
same start logged `recovery_s=24.021`, where every earlier start logged 0.3-0.7 s. That phase
(`server.py` L947-1010) pages through every non-archived entity (2198 at the time) and calls
`reconcile_embedding_jobs` and `reconcile_retrieval_embedding_jobs` before the listeners start, so
readiness waits for it. The database is only ever soft-deleted, so the entity count, and with it this
phase, grows without bound.

The scan is a self-healing pass: it fills missing embedding jobs and cancels obsolete ones. Every live
write path enqueues or cancels jobs in the same coordinator transaction as the content write
(consultant review, files `write.py`, `lifecycle.py`, `relation_service.py`), so in steady state it
finds nothing. The gaps it can fill are legacy rows and migrations that change a source column without
enqueueing.

Decisions taken (owner agreed 2026-10-11 that recovery must not gate readiness and must not grow
with the database):

1. The embedding reconcile leaves the startup path and runs on a background thread after the daemon
   is ready.
2. On every start a cheap canary counts entities with a job gap. The full scan runs only when the
   canary is nonzero.
3. The canary is answered from a new partial covering index on `entities`, so it never walks
   `full_content` (which sits before `content_hash` and `retrieval_text_hash` in the record, so
   reading those columns from the table can mean walking overflow pages). This follows BL-034, which
   added partial indexes for the `init_db` scans and asserted their use through query plans.

Rejected alternatives:

- Background-only, full scan on every start: unblocks readiness but still reads every entity's
  `full_content` on every start and competes with live writes, linear in database size.
- SQL pre-filter of the scan itself: the entity-kind hash is `sha256(title\0content)` computed in
  Python (`embedding_service.py` L32-34), so SQL cannot filter that kind exactly.
- A once-per-generation marker: needs a new table (there is no meta table) or a share of
  `PRAGMA user_version`, which schema migrations already gate on (`schema.py` L1266, L1300, L1322).
  The canary plus the manual `run_backfill` covers the same cases with an index as the only schema
  change; the one case it cannot see is named in D3.
- Making the full scan itself faster: not done without measurement (Coding Standards rule 12).
  Section 6 measures the canary.

## 2. Decisions

- **D1.** After the "SALTMDB daemon ready" log line, a daemon thread named
  `saltmdb-embedding-recovery` runs the embedding recovery. Startup never waits for it.
- **D2.** `reconcile_orphaned_sessions` (one `UPDATE`) and `EmbedJobScheduler.start()` stay in the
  synchronous startup block, in their current order, before the listeners. The scheduler must keep
  starting before the recovery thread so it picks up jobs as they are enqueued.
- **D3.** The recovery thread first runs the canary `embedding_recovery_gap_count(conn)`: the number of
  distinct active entities (`status != 'archived'`) that have any of these gaps:
  a. `content_hash IS NOT NULL AND content_hash != ''` and no `embedding_jobs` row with `job_kind='chunk'` and
     `source_hash = content_hash`.
  b. `content_hash IS NOT NULL AND content_hash != ''` and no `embedding_jobs` row with
     `job_kind='entity'` (any hash).
  c. `retrieval_text_hash IS NOT NULL AND retrieval_text_hash != ''` and no
     `retrieval_embedding_jobs` row for `source_hash = retrieval_text_hash`.
  Gap a matches exactly the rows `reconcile_embedding_jobs` would insert (`INSERT OR IGNORE` on
  `(entity_id, job_kind, source_hash)`, any state). Gap b is hash-agnostic. Gap c is a superset of what
  `reconcile_retrieval_embedding_jobs` inserts (see the next paragraph). A title-only change to the
  entity-kind hash with no write path involved is invisible to the canary; only a migration can cause
  that, and such a migration must enqueue its own jobs or tell the operator to run the manual backfill.
  The query must not select `title`, `full_content` or `retrieval_text`, and every column it reads
  from `entities` (`id`, `status`, `content_hash`, `retrieval_text_hash`) must be in the index of D11.
  Gap c drops the `retrieval_text IS NOT NULL` condition that `reconcile_retrieval_embedding_jobs`
  applies (`retrieval_text` is not in the index). A row with a hash but no text, if one exists, would
  be counted and never closed; D4's recheck makes that visible, and the warning repeats on every start
  until the row is fixed.
- **D4.** Canary result 0: log `Embedding recovery: no job gaps found; full scan skipped` and stop;
  this path logs no `finished` line.
  Canary result N > 0: log `Embedding recovery: N entities with job gaps; running full scan`, run
  both reconcile loops, then log the existing line `Reconciled durable embedding jobs for %d active
  entities (%d retrieval-text rows).`, then run the canary again: a nonzero result logs
  `Embedding recovery: N entities still have job gaps after the full scan` at WARNING, then
  `Embedding recovery finished in %.3f s`. A stop request that ends the scan early skips the recheck.
- **D5.** The two paging loops that exist twice today (`server.py` L949-991 and L601-618) become one
  function, `reconcile_all_embedding_jobs`, used by both the recovery thread and
  `_handle_run_backfill`. This also makes the manual backfill cover retrieval-text jobs, which it does
  not today.
- **D6.** The recovery thread holds an in-flight lease (`_DaemonState._acquire_inflight` /
  `_release_inflight`, `server.py` L244-275) for its whole run, so the 30 s grace timer cannot shut the
  daemon down in the middle of a long scan. The lease is acquired on the main thread before the thread
  starts. If `_acquire_inflight()` returns False (daemon already draining) the thread is not started.
- **D7.** Shutdown: `_DaemonState` gets `embedding_recovery_stop: threading.Event`. `_shutdown_sequence`
  sets it next to `embedding_scheduler.stop()`. The paging loop checks it before each page and returns.
  A `CoordinatorUsageError` whose message is `DAEMON_SHUTTING_DOWN` ends the thread with a debug log,
  not an error.
- **D8.** Any other exception in the recovery thread is logged with `logger.exception` and ends the
  thread. It never exits the daemon. (Today the same failure exits the process with status 1, `server.py`
  L1017-1025; that handler keeps covering the coordinator start and the synchronous steps only.)
- **D9.** The `Daemon startup phases` log line keeps its exact format (a regex in
  `tests/test_daemon_server.py` L808 parses it). `recovery_s` now covers only D2's synchronous steps.
- **D10.** The canary runs as a `background` coordinator job labelled `embedding_recovery_canary`.
  `# shortcut: the canary holds the writer lane for the length of the count; move it to a read
  connection if the snapshot measurement in section 6 shows it taking more than 1 s.`
- **D11.** `init_db` creates `idx_entities_embedding_recovery` ON `entities(id, content_hash,
  retrieval_text_hash, status) WHERE status != 'archived'`, with `CREATE INDEX IF NOT EXISTS`, in the
  `idx_entities_*` list (`schema.py` L1217-1232). `status` is a trailing column on purpose: without
  it SQLite 3.53.1 plans `SCAN e USING INDEX` (a table lookup per row) instead of a covering scan, even
  though the partial predicate implies the `status` filter (probe at lock time, scratch database). The first start after the upgrade builds it once;
  that start's `init_db_s` includes reading `content_hash` and `retrieval_text_hash` of every active
  row, which sit after `full_content` in the record and may cost as much as the old scan on a cold
  cache (not measured), so that one start can still miss the hook budget; section 6 measures it. The canary's `EXPLAIN QUERY PLAN` must show
  `USING COVERING INDEX idx_entities_embedding_recovery` for every `entities` scan.

## 3. Changes per file

### `src/saltmdb/domain/services/embedding_service.py`

Add after `reconcile_retrieval_embedding_jobs` (L262), no change to existing functions:

- `embedding_recovery_gap_count(conn: sqlite3.Connection) -> int`: the SQL lives in a module constant
  `EMBEDDING_RECOVERY_GAP_SQL` (test 5 explains it); one SQL statement, three
  `SELECT e.id FROM entities e WHERE e.status != 'archived' AND ... NOT EXISTS (...)` branches joined
  with `UNION`, wrapped in `SELECT COUNT(*)`, per D3. The SQL is a literal constant with no parameters: the
  predicate must read exactly `e.status != 'archived'` (a bound parameter would stop SQLite using the
  partial index), and the table aliases must be `e` for `entities`, `j` for `embedding_jobs` and `r`
  for `retrieval_embedding_jobs` (test 5 matches on them).
- `reconcile_all_embedding_jobs(coordinator, *, stop: threading.Event | None = None, page_size: int =
  100) -> tuple[int, int]`: the two loops moved out of `server.py` unchanged in behaviour (same
  `coordinator.submit(label, fn, priority="background")`, same cursor by last id, labels
  `reconcile_embedding_jobs` and `reconcile_retrieval_embedding_jobs`), plus: before each page
  `if stop is not None and stop.is_set(): return (entities, retrieval_rows)` with the counts so far.
  `coordinator` is duck-typed (an object with `.submit`); the module must not import from
  `saltmdb.daemon`.

### `src/saltmdb/daemon/server.py`

- `_DaemonState.__init__` (L71): add `self.embedding_recovery_stop = threading.Event()`.
- `_handle_run_backfill` (L594-621): replace the loop with
  `entities, retrieval = reconcile_all_embedding_jobs(self.coordinator)`; response text
  `f"{entities} entities queued for durable embedding ({retrieval} retrieval-text rows)."` The
  `if self.coordinator is None` check stays. No test or caller parses this string
  (`rg "queued for durable embedding"` finds only this line).
- Startup block (L933-1025): delete both paging loops and the combined `Reconciled durable embedding
  jobs` log from the `try`; keep coordinator start, `enable_daemon_connection_boundary()`, the orphaned
  session reconcile, scheduler start and `t_recovery`. Drop the two `reconcile_*` names from the
  import there.
- New module-level function `_run_embedding_recovery(state: _DaemonState) -> None` implementing D3,
  D4, D6 (release in `finally`), D7, D8. Started with
  `threading.Thread(target=..., args=(state,), daemon=True, name="saltmdb-embedding-recovery")` right
  after the `Process memory (daemon ready)` log (L1088), guarded by D6's lease. Before
  `state.start_initial_grace_period()` the lease is already held, so the initial grace timer cannot
  fire while the thread runs.
- `_shutdown_sequence` (L1189): add `state.embedding_recovery_stop.set()` before
  `embedding_scheduler.stop()`.

### `src/saltmdb/db/schema.py`

Add the D11 `CREATE INDEX IF NOT EXISTS` string to the list at L1217-1232, after
`idx_entities_retrieval_text_hash` (L1229). Nothing else in the file changes.

### `tests/test_startup_embedding_recovery.py` (new)

See section 4.

### `docs/architecture.md`

One bullet after L122: at startup the daemon is ready before the embedding job recovery runs; a canary
count of entities with a job gap decides whether the full scan runs; the manual
`--backfill-chunk-embeddings` always runs the full scan and now also covers retrieval-text jobs; a
migration that changes an embedding source column must enqueue its own jobs.

### `docs/BACKLOG.md`

Add `BL-042 | perf | Startup embedding recovery blocked daemon readiness and grew with the database |
implemented | <one sentence, no memory content>` in the existing table format, after the last row.

## 4. Tests

Written failing first, in `tests/test_startup_embedding_recovery.py`. Expected values are literals.

Seeding for tests 1-6 and 9: `init_db` on a temporary path, then raw-SQL `INSERT INTO entities` with
literal ids, titles, contents and hashes, then `enqueue_embedding_jobs_for_entity(..., force=True)` (and
`enqueue_retrieval_embedding_job_for_entity` for the entity with `retrieval_text` and
`retrieval_text_hash`). Direct-mode `memory_service.store_memory` is not required.

1. Clean database: three entities (one with retrieval text), jobs enqueued, nothing deleted:
   `embedding_recovery_gap_count == 0`.
2. Delete all `embedding_jobs` rows of one of the three entities: count `== 1` (distinct entities, not
   gap kinds).
3. Delete only the `retrieval_embedding_jobs` row of the entity that has retrieval text: count `== 1`.
4. An archived entity with no jobs is not counted. An entity with `content_hash` NULL, and one with
   `content_hash = ''`, are not counted for gaps a and b; the same for `retrieval_text_hash` NULL and
   `''` against gap c.
5. Query plan, in the style of `tests/test_init_db_backfill_plan.py`: `EXPLAIN QUERY PLAN` of
   `EMBEDDING_RECOVERY_GAP_SQL`. At lock time the plan lines are three
   `SCAN e USING COVERING INDEX idx_entities_embedding_recovery`, three `SEARCH j|r USING COVERING
   INDEX sqlite_autoindex_...` and one `SCAN (subquery-6)`. Assert: every plan line that starts with
   `SCAN e` ends with `USING COVERING INDEX idx_entities_embedding_recovery`; at least one `SEARCH`
   line mentions `embedding_jobs` and one `retrieval_embedding_jobs`; no line starts with `SCAN j` or
   `SCAN r`. Do not assert the subquery number. Also assert `init_db` on a fresh database creates
   `idx_entities_embedding_recovery`.
6. `reconcile_all_embedding_jobs` with a stand-in coordinator that runs each submitted function on a
   real connection: five entities, `page_size=2`, no `stop`: returns `(5, 0)`. With a `stop` event set
   from inside the first submitted page: returns `(2, 0)` and submits no second page, which means the
   retrieval loop also checks `stop` before its first page.
7. Daemon subprocess started as `TestDaemonSignalShutdown` does (`--foreground`,
   `SALTMDB_VIEWER_ENABLED=false`, `SALTMDB_DB_PATH` on a temporary database), seeded with three
   entities whose `embedding_jobs` rows were deleted before the daemon starts. A reader thread collects
   stdout lines. Wait up to 30 s for `Embedding recovery finished`; only then send SIGTERM, `wait(15)`,
   and assert exit code 0. Assert the order `SALTMDB daemon ready` before
   `Reconciled durable embedding jobs for 3 active entities`, the line
   `Embedding recovery: 3 entities with job gaps; running full scan`, and no `still have job gaps`
   warning. If SIGTERM lands while the embedding scheduler is mid-inference and the daemon does not
   exit within the 15 s wait, report it as a finding; do not raise the limit silently.
8. Same subprocess shape on a clean seeded database (jobs intact): wait for
   `Embedding recovery: no job gaps found; full scan skipped`, then SIGTERM; assert exit code 0 and no
   `Reconciled durable embedding jobs` line.
9. After `reconcile_all_embedding_jobs` on the database of test 2 (gap closed), the canary returns 0.

## 5. Documentation

Section 3 covers `docs/architecture.md` and `docs/BACKLOG.md`. No `MIGRATION.md` entry (an index
only, like BL-034's). No tool description changes.

## 6. Verification procedure

The default gate (`./verify`) plus one measurement, reported not gated:

- `saltmdb-cli snapshot` writes a database copy through the daemon (BL-027). On that copy, time the
  canary statement (read-only, repo venv, quiet host, run twice: once after
  `sync; echo 3 | sudo tee /proc/sys/vm/drop_caches` if the owner allows it, otherwise once warm) and
  report both numbers. If the cold time exceeds 1 s, report it as a finding for the D10 shortcut.
  Also time `init_db` on a second copy of the snapshot once (the one-time D11 index build) and report
  it. Dropping the page cache needs `sudo`; the developer asks the architect instead of running it.
  The developer does not open the live `saltmdb.db`.

## 7. Out of scope

- Making the full scan faster, batching its statements, or avoiding its content read.
- A generation marker or any schema change beyond the D11 index.
- `consolidate_memories` possibly not enqueuing a retrieval-text job for a consolidated entity
  (`relation_service.py` L1770-1808): unverified; file a separate backlog item if confirmed.
- Reading the canary through a read connection (D10 names the trigger).
- The SessionStart hook, `~/.agents`, systemd service installation.
- Why a manual `saltmdb-cli bootstrap-digest` returned an empty digest after this restart.

## 8. Acceptance

1. `uv run pytest tests/test_startup_embedding_recovery.py -q`: red before the change, green after
   (all nine tests).
2. `./verify`: exit 0, no new failures, every existing test unchanged.
3. `rg -n "reconcile_embedding_jobs|reconcile_retrieval_embedding_jobs" src/saltmdb/daemon/server.py`:
   no match (both are reached only through `reconcile_all_embedding_jobs`).
4. `rg -n "reconcile_orphaned_sessions|_ThreadingRpcServer\(\(\"127" src/saltmdb/daemon/server.py`:
   the orphaned-session call appears on an earlier line than the listener construction.
5. `rg -n "saltmdb-embedding-recovery|SALTMDB daemon ready" src/saltmdb/daemon/server.py`: both
   match, and the `SALTMDB daemon ready` line number is the smaller one.
6. `git diff --stat` and `git status --short` list only the files in section 0 scope (the new test file
   is untracked, so only `git status` shows it).
7. The section 6 measurement is reported.

## 9. Pre-lock gate notes

Checks recorded below. Acceptance 4 already holds on today's tree (a regression guard); acceptance 1,
3 and 5 require the new code and are checked after implementation; `BL-034`'s file list
(`git show 6d39e10 --stat`) has no `MIGRATION.md`, which backs section 5. Wording re-grepped after the
last review-driven change (gap c, aliases, D4 zero-gap path, test 6 and 7 notes).

Consultant review round 2: no blockers. Fixes: literal `'archived'` and the `e`/`j`/`r` aliases in
section 3, gap-a/gap-c wording in D3, repeated warning noted in D3, zero-gap path pinned in D4, first
start cost stated in D11, retrieval loop stop check pinned in test 6, test 7 residual risk noted.

Lock-time probe (scratch database, repo venv, SQLite 3.53.1): the canary SQL with the first D11
index draft (no `status` column) planned `SCAN e USING INDEX`, not covering; adding `status` as the
last column gave `SCAN e USING COVERING INDEX` for all three branches and index searches on both job
tables. D11 and test 5 were written from that output.

Consultant review round 1 (pre-lock): B1 (test 6 stop point) fixed; B2 (test 7 exit path) fixed by
prescribing the `TestDaemonSignalShutdown` shape; W1 (canary scans `entities`) fixed by D11 plus the
plan assertion in test 5; W2 (empty hashes) fixed in D3, with a recheck in D4 and test 9; W3
(gap b holds) recorded; wrong claim about test seams fixed in section 0; `_DaemonState.__init__` anchor
fixed to L71; acceptance 5 and 6 reworked. Existing tests touched but compatible: `_DaemonState(...)`
constructions in `tests/test_daemon_server.py` (L51, L583, L849), `tests/test_agent_id_rename.py`
(L200), `tests/test_viewer_operations.py` (L35), `tests/test_viewer_rework_contracts.py` (L157), and the
direct `reconcile_*` calls in `tests/test_embedding_jobs.py` (L62),
`tests/test_embedding_job_reconciliation.py` (L27), `tests/test_retrieval_text.py` (L255).
