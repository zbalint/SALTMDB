# SPEC-BL064-S11-PUBLISH-READY

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 11 (the last item of its order): `_publish_ready` in `daemon/server.py`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Targeted baseline: `.venv/bin/python -m pytest tests/test_daemon_server_main.py tests/test_daemon_server.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `server.main()` with its helpers patched (`tests/test_daemon_server_main.py` ~192 replaces `_publish_ready` with a plain `patch.object(server, "_publish_ready")`, so its arguments are free, and asserts only the start order `["probe", "durable"]`; the same test patches `_bind_daemon_sockets` with `return_value=(object(), object(), 1.0)`, `_start_durable_services` and `_start_service_server` with fakes: their return shapes, a 3-tuple, a 2-tuple and a `(server, port)` pair, MUST stay). `_publish_ready` itself has no direct test today.
- Scope (may edit): `src/saltmdb/daemon/server.py`, ONLY: `_publish_ready`, the two new types (defined above `_publish_ready`), and in `main()` the lines that hold the startup timing marks and the one call to `_publish_ready`; `tests/test_daemon_publish_ready.py` (new characterization tests only).
- Does not touch: `_bind_daemon_sockets`, `_start_durable_services`, `_start_service_server`, `_start_probe_thread`, `_start_viewer`, `_prepare_startup`, the shutdown sequence, `discovery.py`, the protocol, any other part of `main()`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`_publish_ready` takes ten positional parameters: the daemon state, the key and the database path, three port numbers (`service_port`, `e_port`, `p_port`) and four monotonic timestamps (`t0`, `t_bound`, `t_db`, `t_recovery`) that it subtracts pairwise for one log line. The ports belong together (they are logged together) and so do the timestamps (they are only ever used as the boundaries of the startup phases). `main()` holds the marks as four loose locals.

Rejected: changing the return shapes of the startup helpers (the `main()` test pins them); a dict (a bag); moving the embedding-recovery thread start out (a separate responsibility, but the order after the token and the discovery file is part of the contract, not touched here).

## 2. Target shape

Defined above `_publish_ready` in `server.py`:

```python
class _DaemonPorts(NamedTuple):
    service: int
    election: int
    probe: int

class _StartupTimings(NamedTuple):
    """Monotonic marks taken during daemon startup; the phases are the gaps between them."""
    start: float          # t0: before the election
    bound: float          # t_bound: sockets bound
    db_ready: float       # t_db: init_db finished
    services_started: float   # t_recovery: orphaned sessions reconciled and the embed scheduler started (the embedding RECOVERY thread starts later, inside _publish_ready)
```

(Both are small immutable all-numeric records, so both are `NamedTuple`s: no hash note is needed and no method: the five subtractions stay inline in `_publish_ready`, reading `timings.start` and so on, exactly as today. The log label `recovery_s` keeps its text.)

`_publish_ready(state, key, db_path, ports: _DaemonPorts, timings: _StartupTimings) -> None`: 5 parameters (was 10). The body is today's: `secrets.token_urlsafe(32)`, `state.auth_token = ...`, `discovery.write(key, db_path, os.getpid(), ports.service, auth_token)`, the "SALTMDB daemon ready" log line (`db`, `service_port`, `election_port`, `probe_port`), `t_ready = time.monotonic()`, the "Daemon startup phases" log line with the same format string and the same five subtractions computed inline from `timings`, the "Process memory (daemon ready)" line, the embedding-recovery thread started only if `state._acquire_inflight()`, otherwise the same debug line, and the win32 diagnostics call. In `main()`, `_publish_ready(state, key, db_path, _DaemonPorts(service_port, e_port, p_port), _StartupTimings(t0, t_bound, t_db, t_recovery))`.

## 3. Decisions

- D1. Behaviour is identical: the order of the steps (token, state, discovery file, ready log, phases log, memory log, recovery thread, win32 diagnostics), every log message text and format, the single call to `time.monotonic()`, and that nothing is advertised as ready before this function runs.
- D2. Module attributes read at call time stay: `discovery.write`, `secrets.token_urlsafe`, `time.monotonic`, `format_process_memory`, `_run_embedding_recovery`, `_log_windows_job_diagnostics`, `threading.Thread`, `sys.platform`.
- D3. No `noqa`; `NamedTuple` is not imported yet (the `typing` import has `TYPE_CHECKING`, `Any` and `cast`): add it. `dataclass` is not needed.

## 4. Tests

New `tests/test_daemon_publish_ready.py`, written first and green on the unchanged code (calling the old 10-argument form), then the same assertions through the new form:
Existing end-to-end coverage, cite it and do not duplicate it: `tests/test_daemon_server_main.py` ~60 to 110 runs the real daemon as a subprocess and checks the discovery file contents, its 0600 mode, a working auth token and the log marker order ("SALTMDB daemon ready:" before "Daemon startup phases:" before the signal and shutdown markers); `tests/test_startup_embedding_recovery.py` ~232 checks that the ready line precedes the recovery log. What is missing is the isolated unit test below. To run `_publish_ready` in isolation patch or stub: a state stub with a settable `auth_token` and `_acquire_inflight()` returning True or False (nothing else on the state is read), `server.discovery.write`, `server.secrets.token_urlsafe`, `server.time.monotonic`, `server.format_process_memory`, `server.threading.Thread`, `server.sys.platform` (keep that context minimal: it affects the whole process) and `server._log_windows_job_diagnostics`.
1. Token and discovery order: `secrets.token_urlsafe` returns a fixed token and is called once with `32`; `discovery.write` has a `side_effect` that asserts `state.auth_token == token` AT CALL TIME (the token is set before the file is written) and is called with `(key, db_path, os.getpid(), service_port, token)`.
2. The log lines, with UNEQUAL marks and three DISTINCT ports: `start=0`, `bound=1.5`, `db_ready=4.0`, `services_started=8.25` and a `time.monotonic` patched with `side_effect=[10.0]` (a second call raises `StopIteration`, so "monotonic called twice" is killed): the phases line reads `election_s=1.500 init_db_s=2.500 recovery_s=4.250 listeners_s=1.750 total_s=10.000` and the ready line carries the three distinct ports in the order `service_port`, `election_port`, `probe_port` (use `assertLogs` on the module logger).
3. The embedding-recovery thread: started exactly when `state._acquire_inflight()` returns true (assert `threading.Thread` was called with `target=server._run_embedding_recovery`, `args=(state,)`, `daemon=True`, `name="saltmdb-embedding-recovery"`, and `.start()` called), and not started (with the debug line) when it returns false.
4. The win32 diagnostics call happens only when `sys.platform == "win32"`.

## 5. Out of scope

The startup helpers' return shapes; `main()` beyond the call; the shutdown sequence.

## 6. Acceptance

1. `./verify` exits 0.
2. The section 4 items 1 to 4 pass on the unchanged code (calling the old 10-argument form) and after the change (the new form, same assertions).
3. `rg -n "def _publish_ready" -A12 src/saltmdb/daemon/server.py` shows 5 parameters (judge: the architect reads the definition).
4. `git status --short` lists only the two files of section 0.
5. Mutation check on a scratch copy, killer named for each: write the discovery file BEFORE setting `state.auth_token`; swap `ports.election` and `ports.probe` in the ready log; swap two phase boundaries and log `total_s` as `listeners_s` (killed by the unequal marks of item 2); swap two of the three ports in the ready line (distinct ports); start the recovery thread when `_acquire_inflight()` is false; skip the win32 call; call `time.monotonic()` twice (the single-element `side_effect`); a different token length (the `called once with 32` assertion). A surviving mutant is reported.
6. Design read (architect and consultant): 5 parameters, two coherent records with domain names, no method that only reorders a tuple; ruff C901 of `main()` unchanged (9 with `--ignore-noqa`).

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `_publish_ready` has 10 positional parameters, one caller (`main()`), no direct test | `daemon/server.py` ~1033, ~1168; `rg` over tests | read; scout |
| The `main()` test replaces `_publish_ready` with a plain patch and pins the helpers' return shapes | `tests/test_daemon_server_main.py` ~180 to 200 | read |
| The timing marks come from `_prepare_startup` (`t0`), `_bind_daemon_sockets` (3rd element), `_start_durable_services` (2-tuple); `t_recovery` is taken after the orphaned-session reconcile and the `EmbedJobScheduler` start, not after the recovery thread | `server.py` ~934 to 992, ~961 to 984, ~1140 to 1146 | read; consultant read |
| `_publish_ready` is at ruff C901 3 (branches 3, statements 13); `main` at 9 / 5 / 50; the change adds no branch | ruff with `--ignore-noqa` | measured by the consultant |
