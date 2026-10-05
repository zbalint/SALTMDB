# SPEC-DAEMON-COLD-START

## 0. Status

**LOCKED** (2026-10-05, pre-lock gate run against develop at the commit named below; gate notes in section 9). Backlog item BL-012. Context: `mcp-cold-start-2026-10-05`. First consultant review (3 blockers, 1 should-fix, 3 nits) and second look (1 blocker, 3 should-fix, 3 nits) are incorporated in this revision.

- Location/branch: main checkout `<repo>`, branch `develop`. Use the repo `.venv`, never system python. Baseline: the spec's commit on develop (parent: the docs commit that moved the specs; no source file changed since the baseline run). `.venv/bin/pytest -q tests/test_daemon_client.py tests/test_mcp_server.py` gave `25 passed`; the full `./verify` gave `1981 passed, 142 subtests passed`.
- Test seams: `saltmdb.daemon.client` (`ensure_daemon_running`, `reachable_daemon_info`, `probe_owner`, `adopt_current_session`, `begin_lazy_start`, `call`), `saltmdb.mcp.server.server_lifespan` (as in `tests/test_mcp_server.py`), `saltmdb.mcp.tools.RpcBackend.call`. Existing pattern for fake time and sockets: `tests/test_daemon_client.py`.
- Scope (may edit exactly these files): `src/saltmdb/daemon/client.py` (`ensure_daemon_running`, `_spawn_daemon_process`'s env only, the error classes next to `DaemonStartupError`, `call`, `get_current_session`'s docstring, and new small functions), `src/saltmdb/config.py` (six constants beside L678-683), `src/saltmdb/mcp/server.py` (`server_lifespan` only), `src/saltmdb/__main__.py` (L55-70 region only), `src/saltmdb/mcp/tools.py` (`RpcBackend.call` only), `src/saltmdb/daemon/server.py` (the probe-socket bind at L879-883 and the 'Daemon process starting' log line at L835-841 only, D11 and D12), `tests/test_daemon_client.py` (add tests only), `tests/test_mcp_server.py` (add tests; and the existing lifespan tests at L36-69 may be edited only to patch `saltmdb.daemon.client.reachable_daemon_info`, see section 4), `tests/test_rpc_backend_starting.py` (new), `docs/architecture.md` (the daemon startup paragraph only).
- Does not touch: the rest of `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/discovery.py`, `hooks/**`, `cli.py`, the schema, `docs/BACKLOG.md` (architect updates it), other specs, `INSTALL.md`.
- Developer leaves everything uncommitted, unstaged and unmerged; architect reviews and commits.
- Resource rule: other agents run test suites on this host. Run the targeted tests first; run `./verify` once at the end.

## 1. Why

Owner-reported (2026-10-05): after a restart, with the daemon not yet running, the first agent session almost always fails to connect its SALTMDB MCP server.

Cause, confirmed in code: the MCP adapter blocks on the daemon at startup in two places, each with a hard wait of about 10 s (`DAEMON_DISCOVERY_RETRY_ATTEMPTS` 40 x `DAEMON_DISCOVERY_RETRY_DELAY_S` 0.25 s, `config.py` L678-679): `__main__.py` L69 calls `ensure_daemon_running(db_path)` before `mcp.run()`, and `server_lifespan` calls `session.open()`, which itself calls `ensure_daemon_running` (`client.py` L521). `~/.saltmdb/daemon.log` shows the daemon needs 20 to 86 s to become ready right after a reboot (1 to 3 s warm). When the wait runs out `DaemonStartupError` propagates, the MCP server never answers the initialize handshake, and the harness reports a failed connection.

A longer wait alone does not work: documented harness startup limits are short (Codex `startup_timeout_sec` defaults to 10 s; Claude Code's `MCP_TIMEOUT` has no documented default), so the adapter must start serving immediately. A tool call then pays a bounded wait.

Also confirmed: every concurrent adapter respawns the daemon every 8 attempts (about every 2 s) while it initialises (`client.py` L326-333), and each loser pays a full interpreter start. The probe port answers only 8 concurrent identify connections and drops the rest unanswered (`daemon/server.py` L686-695), `identify` reports `ready` (L279-287) about a second before the discovery file is written (L995 vs L1036), and the probe socket is bound without `SO_REUSEADDR` (L879-883) while `daemon.log` shows bind failures starting seconds after a daemon shuts down.

A lazy start has its own trap, seen in `daemon.log` (2026-10-02 23:36:14 ready, 23:36:44 "Grace period elapsed with zero sessions", bind failures from 23:36:51): a daemon that finishes starting while no session has said hello exits 30 s later. So the adapter must hello by itself as soon as the daemon is ready, not wait for the first tool call.

Decision (architect, with consultant review): start the adapter without waiting; spawn the daemon in the background when no owner exists; a background thread waits for the daemon (long cap, outside the session lock) and then does the hello; a tool call that arrives first waits progress-aware under a per-tool-call deadline; past the deadline return a structured retryable result. Rejected: only raising the wait (harness gives up first); waiting in the lifespan.

## 2. Decisions

- **D1. Helpers in `client.py`.**
  - `reachable_daemon_info(db_path: str) -> dict[str, Any] | None`: canonicalizes its own input (`discovery.resolve_canonical_db_path`), reads the discovery file, returns the info when it matches the canonical path and `_authenticated_ping_ok(info)`, else `None`. `ensure_daemon_running` uses it for its fast path (behaviour unchanged). BL-013 uses it too.
  - `probe_owner(db_path: str) -> str | None`: canonicalizes, calls `_identify_probe`, returns `"initializing"` or `"ready"` when the response is a dict whose `state` is one of those and whose `db_path` equals the canonical path; `None` for anything else (no answer, over-capacity drop, other database).
- **D2. Progress-aware wait.** `ensure_daemon_running(db_path, *, cap_s: float | None = None)`. After the fast path fails:
  1. If `probe_owner(db_path)` is `None`, call `_spawn_if_due(db_path)` (D4); if an owner exists, do not spawn.
  2. Deadline: `t0 + cap_s` when `cap_s` is given, else the per-tool-call deadline of D3 if set, else `t0 + DAEMON_STARTUP_PROGRESS_CAP_S`. Always at most that deadline; the legacy window below never extends it.
  3. Loop every `DAEMON_DISCOVERY_RETRY_DELAY_S` (0.25 s): if `reachable_daemon_info` returns info, return it.
  4. Probe at most once per `DAEMON_OWNER_PROBE_INTERVAL_S` (1.0 s). A probe answer of `initializing` or `ready` means an **owner is alive**: reset the miss counter. `None` increments the miss counter; the owner counts as alive until `DAEMON_OWNER_PROBE_MISSES` (3) consecutive probes returned `None`.
  5. Owner alive: never respawn, do not advance the legacy counter; at the deadline raise `DaemonStartingError`.
  6. No owner (3 consecutive misses): behave as today: each loop iteration advances the legacy counter, respawn (through `_spawn_if_due`) every `DAEMON_RESPAWN_RETRY_INTERVAL` (8) iterations, and when the counter reaches `DAEMON_DISCOVERY_RETRY_ATTEMPTS` (40) raise `DaemonStartupError(_classify_startup_failure(...))`. At the deadline raise `DaemonStartupError` as well.
  The miss counter and legacy counter count only iterations after the first spawn attempt; an owner that appears resets both. The phase before the daemon binds its probe port (interpreter start and imports, length unmeasured, see D12) looks like 'no owner', so the legacy window can end while the daemon is still importing; the callers cope with that (D7 retries, D9 maps `DaemonStartingError` only), and `_spawn_if_due` keeps the respawns from piling up.
- **D3. Per-tool-call deadline.** A module-level `contextvars.ContextVar` `_startup_deadline` in `client.py` and a public context manager `client.startup_budget(seconds: float | None = None)`: when the var is unset it sets it to `monotonic() + (seconds if given else DAEMON_STARTUP_PROGRESS_CAP_S)`; when already set it does nothing; on exit it resets the var with the token of the frame that set it (an inner frame never resets an outer deadline). `call()` runs inside `startup_budget()`. `RpcBackend.call` wraps its whole body in `with daemon_client.startup_budget():`, so the `MID_CALL_FAILURE` read retry (a second sequential `call()`) shares the first call's budget. Every `ensure_daemon_running` reached from one tool call (`call_method` L388 and its connect-failure recursion, `ensure_fresh` L658, `open` L521 retry) uses the remaining time. The background thread of D7 passes its own `cap_s`. Known limit, accepted: FastMCP runs these synchronous tools on the event-loop thread, so while one call waits the adapter reads no other request and a SIGTERM handled through the loop is delayed by up to the cap; the cap is small enough that two stacked calls fit in one 60 s tool budget.
- **D4. Errors and constants.** `class DaemonStartingError(DaemonStartupError)` next to `DaemonStartupError`, message `the SALTMDB daemon is still starting (initializing after N s)`. `config.py`: `DAEMON_STARTUP_PROGRESS_CAP_S = 25.0` (two stacked calls fit in the 60 s tool-call budget of common harnesses, `DAEMON_RPC_CALL_TIMEOUT_S` is 60 s; observed cold starts reach 86 s so one call may not be enough), `DAEMON_LAZY_OPEN_CAP_S = 120.0` (the background thread; cold starts reach 86 s), `DAEMON_OWNER_PROBE_INTERVAL_S = 1.0`, `DAEMON_OWNER_PROBE_MISSES = 3`, `DAEMON_SPAWN_MIN_INTERVAL_S = 15.0` (minimum gap between daemon spawns by one adapter process; a spawn pays a full interpreter start and the real owner is already importing), `DAEMON_LAZY_HELLO_BUDGET_S = 5.0` (the hello inside `open()` after the thread's wait); one short comment each. `client._spawn_if_due(db_path) -> bool` is a new module function: it records a module-level monotonic `_last_spawn_at`, calls `_spawn_daemon_subprocess(db_path)` and returns `True` only when at least `DAEMON_SPAWN_MIN_INTERVAL_S` passed since the last spawn by this process, otherwise returns `False` without spawning. Every spawn in `ensure_daemon_running` and `begin_lazy_start` goes through it.
- **D5. `__main__.py`.** Delete the `ensure_daemon_running(db_path)` call (L69), the `db_path = get_db_path()` assignment (L63) that only it uses, and the `ensure_daemon_running` name from the import on L55 (keep `get_current_session`). `tools.configure_backend(tools.RpcBackend())` stays.
- **D6. Lifespan start and background hello.** In `server_lifespan`, replace the unconditional `session.open()` with: if `client.reachable_daemon_info(get_db_path())` is not `None`, `session.open()` (fast path); if that raises `DaemonStartupError`, `DaemonRpcError` or `OSError`, log a warning with the exception and use the lazy path. Otherwise the lazy path: `client.begin_lazy_start(session)`; then yield immediately. Any other exception from `open()` propagates.
- **D7. `begin_lazy_start(session)`.** In `client.py`: (1) if `probe_owner(session.db_path)` is `None`, call `_spawn_if_due(session.db_path)`, non-blocking; (2) `adopt_current_session(session)`; (3) start a daemon thread that, until `DAEMON_LAZY_OPEN_CAP_S` has passed since the thread started, calls `ensure_daemon_running(session.db_path, cap_s=remaining)` outside `session._state_lock`; a `DaemonStartupError` (including `DaemonStartingError`) is logged once at warning level and the loop retries (the daemon may still be importing, D2); on success, only if `get_current_session() is session`, it calls `session.open()` inside `with startup_budget(DAEMON_LAZY_HELLO_BUDGET_S):` (so a daemon that dies between the wait and the hello cannot keep the lock longer than that) and ends; `DaemonRpcError` and `OSError` from `open()` are logged at warning level and end the thread. A tool call that races the thread serializes on the existing lock; the second `open()` returns early. Leave a `# shortcut:` comment: a close racing the thread can reopen once (in production the process exits right after the lifespan ends, the thread is a daemon thread); add a stop flag if that ever matters. The function itself never waits.
- **D8. `adopt_current_session(session)`.** In `client.py`: sets the module-level `_current_session` to `session` without any network call; update `get_current_session`'s docstring to say it may return a session that is adopted but not yet connected (the signal path then sends goodbye on a session with no socket, which only resets fields, `client.py` L600-642). Required because `call()` only attaches the caller session id and capability when `_current_session` is set, and `open()` sets it only after a successful hello (`client.py` L578-579). The first tool call then reaches `call_method` with the session, `ensure_fresh` (L644) finds no socket and calls `open()` (existing behaviour).
- **D9. Structured retryable result.** `RpcBackend.call` runs its whole body inside `with daemon_client.startup_budget():` (D3) and catches `daemon_client.DaemonStartingError` around that whole body (including the `MID_CALL_FAILURE` read retry inside the existing `except`, `tools.py` L248-262) and returns `{"status": "DAEMON_STARTING", "tool": tool_name, "advice": "The SALTMDB daemon is still starting after a restart. Wait a few seconds and repeat the call; nothing was executed."}` (same dict shape as `DAEMON_CONNECTION_LOST_DURING_WRITE`, L262-272). Other `DaemonStartupError`s propagate as today. "Nothing was executed" is true because `ensure_daemon_running` always runs before a request is sent.
- **D10. Hooks.** No hook or CLI change. `daemon_client.call` used by `saltmdb-cli` hooks goes through the same wait, now bounded by D3's 25 s. On a cold start the session-start hook still loses its bootstrap digest, for the same reason as today: it runs three `saltmdb-cli` calls in sequence, each with a 10 s subprocess timeout inside a 15 s hook timeout (`hooks/saltmdb-session-start-bootstrap.py` L45-53, `hooks/*-example.json`), so the 10 s subprocess timeout kills the first call before the new wait could help. Not worse than today; avoided entirely by a persistent daemon (BL-013).
- **D11. Probe socket.** In `daemon/server.py`, before `probe_sock.bind(...)` (L879-883): `if sys.platform != "win32": probe_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)`. Addresses a plausible cause of the bind failures (TIME_WAIT left by closed probe connections); on Linux it does not allow two listeners. It is a mitigation: BL-014 stays open for a remaining cause (a failure window lasted about 3 minutes on 2026-10-05, longer than TIME_WAIT). No unit seam exists (the socket is built inline in `main()`), so it is verified by diff review plus the existing real-daemon tests.

- **D12. Startup lag measurement.** `_spawn_daemon_process` sets `env["SALTMDB_DAEMON_SPAWNED_AT"] = repr(time.time())` beside the existing `SALTMDB_DB_PATH` (`client.py` L193-194). The daemon's existing 'Daemon process starting' log line (`daemon/server.py` L835-841) gets one more field, `startup_lag_s=<time.time() - SALTMDB_DAEMON_SPAWNED_AT>` when the variable parses as a float, `n/a` otherwise. It settles how long interpreter start and imports take on a cold system (`daemon.log` logs that line only after the imports). Informational; no behaviour depends on it.

## 3. Changes per file

### 3.1 `src/saltmdb/daemon/client.py`
`reachable_daemon_info`, `probe_owner` (D1) above `ensure_daemon_running` (L292); the loop rewrite (D2) and the `cap_s` parameter; `_startup_deadline`, `startup_budget` and its use in `call()` (D3); `_spawn_if_due` (D4); `DaemonStartingError` (D4); `adopt_current_session` and `begin_lazy_start` next to `get_current_session` (L57). The `SessionConnection.open` (L521) and `ensure_fresh` (L658) call sites are unchanged: they inherit the new wait and the per-tool-call deadline.

### 3.2 `src/saltmdb/config.py`
The six constants of D4 and `_spawn_if_due`'s companions go after `DAEMON_RESPAWN_RETRY_INTERVAL` (L683).

### 3.3 `src/saltmdb/mcp/server.py`
`server_lifespan`, D6 only. The `finally` block is unchanged (`session.close()` is safe on a never-opened session: with `_sock` None it only resets fields, `client.py` L600-642).

### 3.4 `src/saltmdb/__main__.py`
D5 only.

### 3.5 `src/saltmdb/mcp/tools.py`
`RpcBackend.call`, D9 only.

### 3.6 `src/saltmdb/daemon/server.py`
D11 (the lines before the probe bind) and D12 (the one log line) only.

### 3.7 `docs/architecture.md`
Update the daemon startup description: the adapter starts without waiting; a background hello follows once the daemon is ready; a tool call waits progress-aware up to 25 s per tool call; `DAEMON_STARTING` means retry.

## 4. Tests (failing first; expected values are literals)

Fake time and sockets in the style of `tests/test_daemon_client.py` (patch `saltmdb.daemon.client.time`, `probe_owner`, `reachable_daemon_info` and `_spawn_daemon_subprocess`; no real sleeps, no real daemon):
1. Probe `initializing` for 20 simulated seconds, then reachable: returns the info; `_spawn_daemon_subprocess` called zero times when an owner already existed at the start, once when none did.
2. No owner at all: raises `DaemonStartupError` (not `DaemonStartingError`) after the legacy window (40 non-owner iterations after 3 misses), respawns every 8th iteration as before.
3. Owner `initializing` past the cap: raises `DaemonStartingError` at 25 simulated seconds, no respawn.
4. Holes from the review: alternating `None` and `initializing` never respawns and never advances the legacy counter; a single `None` between answers does not count as "no owner"; `ready` (discovery file not yet written, `reachable_daemon_info` still `None`) counts as owner alive, with no respawn at attempt 0.
5. Probe throttle: over 10 simulated seconds `probe_owner` is called at most 11 times.
6. Per-tool-call deadline: inside one `startup_budget()`, two sequential `ensure_daemon_running` invocations each wanting 20 simulated seconds raise `DaemonStartingError` at a total of 25 seconds, not 40; a `RpcBackend.call` whose first `call()` raises `MID_CALL_FAILURE` and whose read retry then waits shares the same 25 seconds; an inner `startup_budget()` or `call()` never resets an outer deadline; after the outermost frame exits a new `call()` gets a fresh 25 seconds.
7. `reachable_daemon_info` and `probe_owner` return `None` for a missing discovery file, another `db_path`, a failed ping, and (probe) a non-dict or foreign-database answer; they canonicalize a relative or symlinked path.
8. `tests/test_mcp_server.py`: `server_lifespan` with `reachable_daemon_info` patched to `None`: does not call `session.open` synchronously, calls `begin_lazy_start`, yields. With info returned and `session.open` raising `DaemonStartupError`: logs a warning, takes the lazy path, yields. With `session.open` raising `ValueError`: the exception propagates. The two existing lifespan tests (L36-69) are edited only to patch `reachable_daemon_info` to return a dict.
9. Background hello (the first blocker): after `begin_lazy_start(session)` with a fake daemon that becomes reachable after 5 simulated seconds, the fake receives a hello with no tool call having been made; and a tool call racing the thread does not send a second hello.
10. Attribution after a lazy start: after `begin_lazy_start(session)`, a `client.call(db_path, tool, kwargs, caller_agent_session_id=session id)` against a fake daemon sends params containing `caller_agent_session_id` and a non-empty `caller_agent_session_capability` on the first request.
11. `begin_lazy_start` with an owner already initializing does not spawn (`_spawn_daemon_subprocess` zero calls). Spawn throttle: with fake time, `_spawn_if_due` called at t=0, 5 and 14 spawns once; at t=15 spawns again; `ensure_daemon_running` with no owner for 12 simulated seconds spawns at most once.
11b. Pre-probe phase (second-look blocker): the probe answers `None` for 20 simulated seconds (daemon still importing), then `initializing` for 40 more, then the daemon is reachable; the thread of `begin_lazy_start` survives the `DaemonStartupError` of the first window, retries, and the fake receives the hello at about 60 seconds with no tool call made. A thread whose daemon never appears stops retrying at 120 simulated seconds, having logged a warning.
11c. `_spawn_daemon_process` puts `SALTMDB_DAEMON_SPAWNED_AT` (a float string) in the Popen env (patched `subprocess.Popen`), keeping `SALTMDB_DB_PATH` unchanged.
12. In the new `tests/test_rpc_backend_starting.py`: `RpcBackend.call` with `daemon_client.call` patched to raise `DaemonStartingError` returns the dict of D9 with the tool name; the same when the error is raised from the `MID_CALL_FAILURE` read retry; with a plain `DaemonStartupError` the exception propagates.
13. Acceptance commands, not pytest: `rg -n 'ensure_daemon_running' src/saltmdb/__main__.py` prints nothing; `rg -n 'SO_REUSEADDR' src/saltmdb/daemon/server.py` prints a match in the probe block; `rg -n 'startup_lag_s' src/saltmdb/daemon/server.py` prints the new log field.

## 5. Documentation

Section 3.7. No `MIGRATION.md` row (no schema, version or tool-parameter change); the new `DAEMON_STARTING` status is a new result of existing tools: the developer states in the report whether any tool description in `mcp/tools.py` enumerates possible `status` values and needs a sentence (`CONTRIBUTING.md` section 5); if so add it in `RpcBackend`'s docstring only and report it as an uncertainty.

## 6. Verification procedure

Targeted: `.venv/bin/pytest -q tests/test_daemon_client.py tests/test_mcp_server.py tests/test_rpc_backend_starting.py tests/test_daemon_server.py`. Then `./verify` once. Do not start a real daemon for the new tests.

## 7. Out of scope

- A full fix for the probe-port bind failures (BL-014 stays open; D11 is a mitigation only).
- A persistent or systemd-managed daemon (BL-013).
- Hook timeouts and scripts, `saltmdb-cli`.
- Windows behaviour; the intermediary launcher path in `_spawn_daemon_subprocess` is reused untouched.
- Making the cold start itself faster; reducing the `identify` over-capacity drop.

## 8. Acceptance

In order, at the main checkout with the repo `.venv`:

1. Baseline (recorded in section 0): `.venv/bin/pytest -q tests/test_daemon_client.py tests/test_mcp_server.py` = 25 passed. After implementation: the section 6 targeted command passes with zero failures and the new tests included.
2. `.venv/bin/ruff check src tests`, `.venv/bin/ruff format --check src tests`, `.venv/bin/mypy src`: exit 0 (no new `noqa` unless justified in the report).
3. The two `rg` commands of section 4 item 13.
4. `./verify`: exit 0, report counts.
5. `git diff --stat` and `git status --short` list only files in section 0 scope.
6. Manual, owner (not run by the developer): stop the daemon, start an agent session, and see the MCP server connect; the first tool call returns its result or `DAEMON_STARTING` and succeeds on repeat; the daemon survives past 30 s without any tool call.

## 9. Pre-lock gate notes

Done before locking: baseline run (25 passed targeted, 1981 passed full); all callers of `ensure_daemon_running` listed (`client.py` L388, L521, L658; `__main__.py` L69, removed by D5); `SessionConnection.close()` is safe on a never-opened session (read at `client.py` L600-642); `tests/test_mcp_server.py` L53 and L69 assert `open.assert_called_once()` with a mocked `SessionConnection`, which D6 changes (hence the scope clause for those two tests); existing `RpcBackend` tests live in `tests/test_trace_mcp_tools.py`, `test_phase3_mcp_surface.py` and `test_session_identity.py` (none edited); the `identify` states and the ready-before-discovery-write gap checked in `daemon/server.py` L279-287, L995, L1036; the 8-connection probe drop at L686-695; `__main__.py` L63 (`db_path`) is used only by the call D5 removes; FastMCP runs these synchronous tools on the event-loop thread (consultant read of the installed library), recorded in D3 as an accepted limit; wording re-grepped for `startup_budget`, `_spawn_if_due`, `probe_owner`, `reachable_daemon_info`, `begin_lazy_start`, `adopt_current_session`, `DAEMON_STARTUP_PROGRESS_CAP_S` (25 s everywhere), `DaemonStartingError`, `DAEMON_STARTING`; one concrete instance walked (cold boot, three adapters starting together, daemon needs 60 s, first tool call at t=5 s) by the architect and the consultant, which produced D3, D4's spawn throttle and D7's retry loop. Two consultant reviews are incorporated (3 blockers and 1 should-fix, then 1 blocker and 3 should-fix).

Post-implementation checks (need the new code, so not run at lock): everything in section 8 except item 1's baseline.

Uncertainties accepted: the length of the interpreter start and import phase is unmeasured (D12 measures it); the root cause of the longest bind-failure window is open (BL-014).
