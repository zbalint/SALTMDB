# SPEC-DAEMON-STARTUP-PHASES

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-021. Shared `context_id`: `perf-startup-phases-2026-10-10`.
- Baseline: the commit that adds this spec on `develop`, plus the `./verify` counts the developer
  records before editing.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the daemon's log output, observed through the existing real-subprocess test in
  `tests/test_daemon_server.py` (`TestDaemonSignalShutdown`, which starts `python -m saltmdb.daemon.server --foreground`
  with stdout piped).
- Scope (may edit): `src/saltmdb/daemon/server.py` (`main()` only), `tests/test_daemon_server.py`.
- Does not touch: everything else, including `client.py`, `config.py`, the DB layer, docs.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

After the owner's WSL2 restart on 2026-10-10 the daemon needed about 30 s from spawn to "ready"
(`~/.saltmdb/daemon.log`, 06:47:45 to 06:48:15). `startup_lag_s` (spawn to start of `main()`) was only
0.2 to 6.9 s, so more than 20 s went into `main()` itself, and nothing says which step. The candidates in
`main()` are: the election and probe-port binds, `init_db()` on the 117 MB database, the paged
`reconcile_embedding_jobs` and `reconcile_retrieval_embedding_jobs` loops (about 2,100 active entities),
orphaned-session cleanup, the embedding scheduler start, and the service and viewer listener binds. Measure
before optimising: log the elapsed time of each phase in one line when the daemon is ready.

## 2. Decisions

- D1. Use `time.monotonic()`. Take a timestamp at five points in `main()`:
  `t0` right after the line `Daemon process starting: ...` is logged; `t_bound` right after the probe-port
  bind succeeds (after `probe_sock.listen(...)` in the `try`); `t_db` right after `init_db()` and
  `conn.close()`; `t_recovery` at the end of the startup `try` block that holds the embedding recovery,
  i.e. after `state.embedding_scheduler.start()`; `t_ready` immediately after the existing
  `SALTMDB daemon ready: ...` log call.
- D2. Directly after the `t_ready` assignment, emit one INFO line:
  `Daemon startup phases: election_s=%.3f init_db_s=%.3f recovery_s=%.3f listeners_s=%.3f total_s=%.3f`
  where election = `t_bound - t0`, init_db = `t_db - t_bound`, recovery = `t_recovery - t_db`,
  listeners = `t_ready - t_recovery`, total = `t_ready - t0`.
- D3. No behaviour change, no new helper function, no config constant, no new log level. If startup exits
  early, no phases line is logged.

## 3. Changes per file

### 3.1 `src/saltmdb/daemon/server.py`

Add the five `time.monotonic()` assignments and the one `logger.info` call from D1 and D2, inside
`main()`. `time` is already imported (used by `main()` for `startup_lag_s`). Do not alter any existing line.

## 4. Tests

`tests/test_daemon_server.py`: in `test_sigterm_triggers_clean_shutdown_without_deadlock`, after the existing
exit-code assertions, read the remaining output from `proc.stdout` and assert that it matches the regex
`Daemon startup phases: election_s=(\d+\.\d{3}) init_db_s=(\d+\.\d{3}) recovery_s=(\d+\.\d{3}) listeners_s=(\d+\.\d{3}) total_s=(\d+\.\d{3})`,
that all five values are non-negative, and that `total_s` is at least the sum of the other four minus
0.01 (rounding slack). If reading stdout after the existing `finally` handling is awkward, add a second test
with the same subprocess shape instead; either is acceptable. The test is written first and fails against
the baseline because the line does not exist.

## 5. Out of scope

Optimising any phase, adapter-side timing, changing log destinations, `startup_lag_s` itself.

## 6. Acceptance

1. Before editing: `./verify`; record counts and any pre-existing failure.
2. Write the test; `.venv/bin/python -m pytest tests/test_daemon_server.py -q` fails on exactly the new
   assertion.
3. After implementing: the same command exits 0.
4. Isolated daemon run (temp HOME and DB, never the working daemon). Output goes to a file because the
   timeout kills the pipeline:

   ```sh
   T=$(mktemp -d); HOME=$T SALTMDB_DB_PATH=$T/x.db SALTMDB_VIEWER_ENABLED=false timeout 25 .venv/bin/python -m saltmdb.daemon.server --foreground > $T/out.log 2>&1; rg 'daemon ready|startup phases' $T/out.log; rm -rf $T
   ```

   Expected: two lines, `SALTMDB daemon ready: ...` then `Daemon startup phases: ...`. Report the values.
5. `./verify` exits 0 (do not start a second full run in parallel).
6. `git status --short` lists only the two scope files.

## 7. Pre-lock gate notes

- The log text `Daemon startup phases` and the line `SALTMDB daemon ready` appear in no existing test
  or source other than `server.py` (`rg -n 'daemon ready|Reconciled durable' tests src`).
- The isolated run in 6.4 was dry-run on the unchanged code: the `ready` line appears with `--foreground` and
  a file redirect (a pipe into `rg` lost the output when `timeout` terminated the pipeline); the phases
  line is the post-implementation check.
- The step that the 30 s may also include time before `main()` (imports) is outside this spec; `startup_lag_s`
  already covers that part.
