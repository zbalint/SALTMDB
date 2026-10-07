# SPEC-DAEMON-BACKGROUND-SERVICE

## 0. Status

**LOCKED** (2026-10-07, pre-lock gate run against develop at the baseline commit named below; gate notes in section 9). Backlog item BL-013. Context: `daemon-background-service-2026-10-05`. Two consultant reviews are incorporated (3 should-fix and 7 nits, then 2 blockers, 1 should-fix and 8 nits against the committed BL-012 code).

- Location/branch: main checkout `<repo>`, branch `develop`. Use the repo `.venv`, never system python. Baseline: develop at `c8b94c9` (BL-012 implemented in `8180ade`); `.venv/bin/pytest -q tests/test_daemon_client.py` gave `38 passed, 3 subtests passed`; the full `./verify` last ran on BL-012's final tree (`2004 passed, 145 subtests`).
- Test seams: `saltmdb.daemon.service` public functions (`render_unit`, `unit_path`, `install_service`, `uninstall_service`, `start_daemon`, `stop_daemon`, `daemon_status`), each returning a `ServiceResult`; the injectable command runner; the module-level `_read_proc_cmdline(pid)`; patched `saltmdb.daemon.service.time`; and `saltmdb.cli.build_parser` for the argument surface. Existing real-daemon subprocess pattern: the `--foreground` test in `tests/test_daemon_server.py`.
- Scope (may edit exactly these files): `src/saltmdb/daemon/service.py` (new), `src/saltmdb/daemon/client.py` (`_spawn_daemon_process` signature and its `args` list only), `src/saltmdb/config.py` (two new constants beside the daemon constants), `src/saltmdb/cli.py` (the new `daemon` subcommand group and the parser description), `tests/test_daemon_service.py` (new), `tests/test_daemon_client.py` (add tests only), `tests/test_cli.py` (add tests only), `INSTALL.md`, `docs/architecture.md` (the daemon lifecycle paragraphs only).
- Does not touch: `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/discovery.py`, `ensure_daemon_running`, `reachable_daemon_info`, `probe_owner`, `_spawn_if_due`, `begin_lazy_start`, `SessionConnection`, `src/saltmdb/mcp/**`, `hooks/**`, the schema, `docs/BACKLOG.md` (architect updates it), other specs.
- Developer leaves everything uncommitted, unstaged and unmerged; architect reviews and commits.
- Resource rule: other agents run test suites on this host. Run the targeted tests below first; run `./verify` once at the end.

## 1. Why

A daemon that is already running removes the cold-start wait for the first agent session (BL-012). Today the daemon only exists as a child spawned by the first MCP adapter, and it exits 30 s after its last session leaves. `python -m saltmdb.daemon.server --foreground` already runs with no idle timer until SIGINT or SIGTERM (`_DaemonState.foreground`, `daemon/server.py`), but there is no supported way to start it detached, stop it, check it, or run it under systemd.

This is also the first step toward running the daemon on another host (backlog BL-015): an adapter cannot spawn a daemon there, so something other than the adapter must start and keep it alive. This spec stays local-only (loopback, one machine, systemd user unit); BL-015 is not designed here. Two cheap choices keep it open: the service functions return structured results and the CLI layer prints them, and the local-only kill logic sits in one helper.

Facts the design rests on (read in `daemon/server.py` at the baseline): on SIGTERM the daemon sets `_draining` first, so `ping` fails with `DAEMON_SHUTTING_DOWN` within milliseconds (`handle_request`); the real exit comes later (`_shutdown_sequence`: service server close, probe socket close, coordinator drain of up to 5 s plus a goodbye lease of up to 10 s, `discovery.remove`, `guard.close`, `os._exit(0)`). The identify response never reports draining (`identify_response`), so during that drain the probe still says `ready`. An election loser exits 0 at the guard bind, before it opens the database. The discovery file has no foreground flag, so nothing but the process itself says whether a daemon has an idle timer.

Owner decisions (2026-10-05): scope is a start/stop/status command plus a service installer plus docs; platform is Linux and WSL2 with systemd only. The architect decided the rest (section 2).

Rejected: a new daemon flag for "persistent but log-redirected" (the detached spawn already redirects stdout and stderr to `daemon.log`, so `--foreground` on that spawn is enough); a system-wide unit (needs root; the daemon is per-user because its state lives under `~/.saltmdb`); calling `loginctl enable-linger` automatically (changes login-manager state outside this project); reusing `_spawn_if_due` in the CLI (its 15 s throttle is a module global built for adapters); detecting process exit with `os.kill(pid, 0)` (a zombie still answers it, and in the real-daemon test the daemon is a never-waited child); using `reachable_daemon_info` returning `None` as proof that a stopped daemon has exited (it goes `None` at the start of the drain); making `daemon start` call `systemctl` when a unit exists (documented instead, D9).

## 2. Decisions

- **D1. CLI group and result type.** New CLI group `saltmdb-cli daemon <start|stop|status|install-service|uninstall-service>`. The `saltmdb-cli` parser description changes from "Read-only SALTMDB CLI." to "SALTMDB CLI (read-only data commands plus daemon lifecycle)." Every public function of `service.py` returns `ServiceResult`, a `NamedTuple(exit_code: int, message: str)`, and never prints; the `cli.py` command functions print `message` to stdout and return `exit_code`. Each of `start_daemon`, `stop_daemon` and `daemon_status` takes `db_path: str | None = None` (`None` means `discovery.resolve_canonical_db_path()`, otherwise the given path is canonicalized the same way). CLI contract, also stated in `INSTALL.md`: exit 0 success, 1 failure or refusal, 3 for `status` when no daemon is running or one is starting or stopping.
- **D2. Platform guard.** Every function of the group returns `ServiceResult(1, "daemon commands support Linux and WSL2 only")` when `sys.platform` does not start with `"linux"`, before any other work. (`install_service` additionally needs `shutil.which("systemctl")`, D7.)
- **D3. start.** Resolve the canonical DB path. If `client.reachable_daemon_info(db_path)` returns info, return `ServiceResult(0, "already running: pid=P port=N (a daemon spawned by an agent exits 30 s after its last session; run stop, then start, for a persistent one)")` without spawning. Otherwise run this wait loop with `client.probe_owner` and `client._spawn_daemon_process(db_path, persistent=True)`, up to `DAEMON_START_WAIT_S`:
  1. Every `DAEMON_DISCOVERY_RETRY_DELAY_S`, check `client.reachable_daemon_info`; on info return `ServiceResult(0, "started: pid=P port=N")` if this call spawned a daemon, else the `already running` message above.
  2. Probe at most once per `DAEMON_OWNER_PROBE_INTERVAL_S`. An answer (`initializing` or `ready`) resets the miss counter; `None` increments it.
  3. When the miss counter reaches `DAEMON_OWNER_PROBE_MISSES` and this call has not spawned within the last `DAEMON_SPAWN_MIN_INTERVAL_S`, spawn once (persistent) and record the time. An owner that is alive but not yet reachable (initializing, or a daemon draining after a SIGTERM that has not exited) is therefore waited for, never spawned against; if it exits, three misses later this call spawns.
  4. On timeout return `ServiceResult(1, ...)` naming `daemon.log` and its last 20 lines from `client._read_daemon_log_tail(db_path)`.
  `start` does not turn an adapter-spawned daemon into a persistent one (the message says so).
- **D4. persistent spawn.** `_spawn_daemon_process(db_path: str, *, persistent: bool = False)`: when `persistent` is true, `args` is `[sys.executable, "-m", "saltmdb.daemon.server", "--foreground"]`; otherwise unchanged. The existing Popen stdout and stderr redirection to `daemon.log` stays, and the function already sets `SALTMDB_DB_PATH` from its `db_path` argument. Existing callers pass nothing and behave exactly as before.
- **D5. stop.** In order: (1) if the unit file exists, run `systemctl --user is-active --quiet saltmdb-daemon.service` through the injectable runner: return code 0 returns `ServiceResult(1, "managed by systemd: use systemctl --user stop saltmdb-daemon")` (the unit uses `Restart=always`, so a SIGTERM would only restart it); return code 3 (inactive, or activating) or 4 (the unit file exists but systemd does not know the unit, for example before a `daemon-reload`; read with `systemctl --user is-active --quiet no-such-unit.service` on the dev host) continues; any other code returns `ServiceResult(1, "cannot determine whether the systemd unit is active (systemctl exit N); stop it with systemctl --user stop saltmdb-daemon or fix the user session")` and sends no signal. (2) `client.reachable_daemon_info(db_path)`; if `None`: when `client.probe_owner(db_path)` reports an owner return `ServiceResult(1, "the daemon is starting or stopping; try again shortly")` (identify carries no pid and no draining flag), otherwise `ServiceResult(0, "not running")`. (3) `pid = info["daemon_pid"]`; call `_signal_local_daemon(pid)`, the one helper that holds all local-only kill logic: it returns an error message (and sends nothing) unless `pid` is a positive integer and `_read_proc_cmdline(pid)` contains `saltmdb.daemon.server` (guards against a reused pid or a different pid namespace), otherwise it sends `os.kill(pid, signal.SIGTERM)` (never `pkill` or a name pattern) and returns `None`. A refusal returns `ServiceResult(1, message)`. (4) Wait for the process to exit, not for the daemon to stop answering: poll every `DAEMON_DISCOVERY_RETRY_DELAY_S` until `_read_proc_cmdline(pid)` no longer contains `saltmdb.daemon.server` (a missing process or a zombie reads as empty), up to `DAEMON_STOP_WAIT_S`; then return `ServiceResult(0, "stopped")`. On timeout return `ServiceResult(1, "daemon pid P did not exit within N s; it may still be draining, see daemon.log")`. Never send SIGKILL. Known limit: a SIGTERM that lands in the few milliseconds between the discovery write and the daemon's signal handlers takes the default action (no drain, stale discovery file); the file is harmless because the ping fails.
- **D6. status.** Reachable: `ServiceResult(0, "running pid=P port=N started_at=T db=PATH")`. Not reachable but `probe_owner` reports an owner: `ServiceResult(3, "starting or stopping")`. Otherwise `ServiceResult(3, "not running")`. It never includes the auth token or any other discovery-file field. It cannot tell a persistent daemon from an adapter-spawned one (D3 note).
- **D7. unit file.** User unit at `$XDG_CONFIG_HOME/systemd/user/saltmdb-daemon.service` (default `~/.config/systemd/user/`). `render_unit(python: str, db_path: str) -> str` returns exactly the text in section 3.1. `python` is `sys.executable`, `db_path` is the canonical DB path. Either value containing whitespace (including a newline or carriage return), `%`, `$`, `"` or `\` raises `ValueError` naming the value (systemd quoting rules are not worth reimplementing). `RestartSec=30` is a starting value chosen without measurement (each loser retry pays an interpreter start and the imports; BL-012 added a `startup_lag_s` field to the daemon's `Daemon process starting` log line that settles the cost); revisit it with that data.
- **D8. install-service.** `shutil.which("systemctl")` required, else `ServiceResult(1, ...)` with a clear message. Steps, each as an argument-list `subprocess.run` through the injectable runner, `check=False`, never `shell=True`: write the unit (create parent dirs, mode 0644), `systemctl --user daemon-reload`, `systemctl --user enable --now saltmdb-daemon.service` (or `enable` only with `--no-start`). `--dry-run` returns the unit and the planned commands in the message and changes nothing. Idempotent: re-running with identical content reports `unchanged`, with different content `updated`. Any nonzero systemctl exit includes its stderr and the hint `On WSL2, enable systemd in /etc/wsl.conf ([boot] systemd=true) and restart WSL` and returns exit code 1. On success the message ends with these hints; none of the commands they name is ever run: `To start at boot without a login session: loginctl enable-linger $USER`; and `enable --now returning 0 does not mean the unit's daemon took over: run saltmdb-cli daemon status; if a daemon spawned by an agent holds the election, the unit's daemon exits as a loser and systemd retries every 30 s`.
- **D9. uninstall-service.** `systemctl --user disable --now saltmdb-daemon.service`, remove the unit file if present, `systemctl --user daemon-reload`. If the unit file does not exist, return `ServiceResult(0, "not installed")` without calling systemctl.
- **D10. clients unchanged, known races documented.** Adapters and hooks still spawn a grace-timer daemon when none is reachable. If the managed or persistent daemon is up they just connect (same election port, same discovery file). Two consequences are documented in `INSTALL.md`, not fixed here: (a) while agent sessions keep cycling, an adapter-spawned daemon can win the election again each time the previous one exits, so a freshly installed unit may not take over until the agents are idle for 30 s; (b) `daemon start` while the unit is installed and enabled leaves the unit retry-looping every 30 s with a failed start in the journal each time; use `systemctl --user start saltmdb-daemon` then.
- **D11. constants.** `config.py`: `DAEMON_START_WAIT_S = 120.0` (observed cold starts reach 86 s) and `DAEMON_STOP_WAIT_S = 30.0` (drain 5 s plus goodbye-lease drain 10 s, `config.py` L668 and L675, with margin). D3 and D5 reuse the existing `DAEMON_DISCOVERY_RETRY_DELAY_S`, `DAEMON_OWNER_PROBE_INTERVAL_S`, `DAEMON_OWNER_PROBE_MISSES` and `DAEMON_SPAWN_MIN_INTERVAL_S`.
- **D12. module access.** `service.py` does `from saltmdb.daemon import client` and `import time` and calls `client.reachable_daemon_info`, `client.probe_owner`, `client._spawn_daemon_process`, `client._read_daemon_log_tail` and `time.monotonic`/`time.sleep` as module attributes, never `from ... import name`, so tests patch them in one place. The two private `client` uses carry one `# shortcut:` comment: private imports, make them public if a third caller appears. `_read_proc_cmdline(pid: int) -> str` is a module-level function in `service.py`: it reads `/proc/<pid>/cmdline`, replaces NUL bytes with spaces, and returns `""` on `OSError`.

## 3. Changes per file

### 3.1 `src/saltmdb/daemon/service.py` (new)

Standard library plus `saltmdb.daemon.client`, `saltmdb.daemon.discovery` and `saltmdb.config`. Functions per D1 to D9 and D12. `render_unit` returns exactly (python and db_path substituted, trailing newline):

```
[Unit]
Description=SALTMDB daemon (persistent)
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=SALTMDB_DB_PATH=<db_path>
ExecStart=<python> -m saltmdb.daemon.server --foreground
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

The runner parameter defaults to `subprocess.run`; with `time` and `_read_proc_cmdline` it is the only seam the tests replace.

### 3.2 `src/saltmdb/daemon/client.py`

Only `_spawn_daemon_process` and its `args = [sys.executable, "-m", "saltmdb.daemon.server"]` line: add the keyword-only `persistent` parameter and the conditional `--foreground` per D4. No other line changes; `_spawn_daemon_subprocess` and the win32 intermediary stay as they are.

### 3.3 `src/saltmdb/config.py`

Add the two constants of D11 after the BL-012 startup constants (after `DAEMON_LAZY_HELLO_BUDGET_S`), each with a one-line comment.

### 3.4 `src/saltmdb/cli.py`

Add the `daemon` subparser group (D1) in `build_parser` before `return p`, each subcommand with `set_defaults(func=...)`; the command functions call `service.py`, print `result.message` and return `result.exit_code`. `install-service` takes `--dry-run` and `--no-start`. `main` stays unchanged (its `sys.exit(args.func(args) or 0)` already turns a returned int into the exit code).

### 3.5 Docs

`INSTALL.md`: a section "Running the daemon persistently" covering the by-hand foreground and detached start, `install-service`, the WSL2 prerequisite, the linger hint, the CLI contract of D1 (exit codes 0, 1 and 3 and the one-line status format), how to stop a managed daemon (`systemctl --user stop saltmdb-daemon`; `daemon stop` refuses while the unit is active; while the unit is only retrying (`activating (auto-restart)`) `daemon stop` proceeds and stops the daemon that holds the election, after which the unit takes over), that adapters still start their own daemon when none is running, the two races of D10, that `daemon status` cannot tell a persistent daemon from an adapter-spawned one, that the unit bakes in the DB path of the shell that ran `install-service` while a CLI run from a shell with a different `SALTMDB_DB_PATH` inspects a different daemon, and that a systemd-started daemon does not inherit the user's shell environment while an adapter-spawned one copies it: settings such as viewer variables must be added with `systemctl --user edit saltmdb-daemon`. `docs/architecture.md`: one paragraph near the grace-timer paragraph and a clause near the `daemon.log` sentence stating that a persistent daemon has no idle timer and logs to `daemon.log` when started detached or to the journal when run by systemd.

## 4. Tests (failing first; expected values are literals)

`tests/test_daemon_service.py` (fake runner returning an object with `returncode` and `stderr`; `XDG_CONFIG_HOME` set to a temp dir wherever a unit path matters; `saltmdb.daemon.service.time` patched with a fake clock so no test really sleeps; `client.reachable_daemon_info`, `client.probe_owner`, `client._spawn_daemon_process`, `service._read_proc_cmdline` and `os.kill` patched unless a test says it runs a real daemon):
1. `render_unit("/home/u/.venv/bin/python", "/home/u/.saltmdb/saltmdb.db")` equals the exact literal text of section 3.1.
2. `render_unit` raises `ValueError` for a python path with a space, a db path containing `%`, and a value containing a newline.
3. `install_service`: writes the unit at `<tmp>/systemd/user/saltmdb-daemon.service`, and the fake runner receives exactly `["systemctl","--user","daemon-reload"]` then `["systemctl","--user","enable","--now","saltmdb-daemon.service"]`; with `no_start=True` the second command is `["systemctl","--user","enable","saltmdb-daemon.service"]`. Its message contains `loginctl enable-linger` and `daemon status`.
4. A second `install_service` with the same inputs reports `unchanged`; with a different `db_path` reports `updated` and rewrites the file.
5. `install_service(dry_run=True)` writes no file and runs no command, and its message contains the unit text.
6. A fake runner returning exit 1 with stderr text: `install_service` returns exit code 1 and its message contains `systemd=true`.
7. `install_service` returns exit code 1 when `shutil.which("systemctl")` is patched to `None`; with `sys.platform` patched to `"darwin"` each of `install_service`, `uninstall_service`, `start_daemon`, `stop_daemon` and `daemon_status` returns exit code 1 with `Linux and WSL2 only` in the message and runs no command and no signal.
8. `uninstall_service` with no unit file returns exit code 0, message `not installed`, runs no command; with a file it runs `["systemctl","--user","disable","--now","saltmdb-daemon.service"]`, removes the file, then daemon-reload.
9. `stop_daemon` with the unit file present: `is-active` returning 0 gives exit code 1, `managed by systemd` and no signal; returning 3 or 4 proceeds to the signal; returning 1 gives exit code 1, `cannot determine` and no signal.
10. `stop_daemon` returns exit code 1 and sends no signal when `_read_proc_cmdline` does not contain `saltmdb.daemon.server`, and when the pid is not a positive integer.
11. `stop_daemon` waits for the process: `reachable_daemon_info` returns info once and then `None` (the drain), `_read_proc_cmdline` keeps containing `saltmdb.daemon.server` for 5 simulated seconds and then returns `""`; the result is exit code 0 `stopped` only after those 5 simulated seconds, and `os.kill` was called exactly once with `signal.SIGTERM`.
12. `stop_daemon` timeout: `_read_proc_cmdline` never stops matching: after 30 simulated seconds the result is exit code 1 naming the pid and `daemon.log`; `os.kill` was called exactly once and never with `SIGKILL`.
13. `daemon_status`: reachable info gives exit code 0 and a message with `pid=` and `port=` and no token; `reachable_daemon_info` `None` with `probe_owner` `"initializing"` or `"ready"` gives exit code 3 and `starting or stopping`; both `None` gives exit code 3 and `not running`. `stop_daemon` in the owner case returns exit code 1 with `starting or stopping`.
14. `start_daemon` with reachable info returns exit code 0, `already running`, the `stop, then start` hint, and spawns nothing.
15. `start_daemon` spawn rule: with `probe_owner` always `None` and `reachable_daemon_info` becoming info after 10 simulated seconds, `_spawn_daemon_process` is called exactly once, with `persistent=True`, after at least `DAEMON_OWNER_PROBE_MISSES` probes, the result is exit code 0 `started`, and `probe_owner` is called at most 11 times; with `probe_owner` `"initializing"` for 20 simulated seconds and then info, `_spawn_daemon_process` is called zero times and the result is exit code 0.
16. `start_daemon` timeout: nothing ever reachable and no owner for more than `DAEMON_START_WAIT_S` simulated seconds: the result is exit code 1 naming `daemon.log`, `_read_daemon_log_tail` was called once, and every two consecutive spawns are at least `DAEMON_SPAWN_MIN_INTERVAL_S` simulated seconds apart.
17. Real daemon (same pattern as the `--foreground` subprocess test in `tests/test_daemon_server.py`, temp DB path passed explicitly as `db_path` to every call): set `SALTMDB_VIEWER_ENABLED=false` in the test process environment (a real daemon on the dev machine holds the default viewer port; the spawn copies `os.environ`). `start_daemon` returns exit code 0 and a second `start_daemon` returns `already running`; `daemon_status` returns exit code 0 and its message has no token; `stop_daemon` returns exit code 0 `stopped`; `daemon_status` then returns exit code 3 and `stop_daemon` returns exit code 0 `not running`. A persistent daemon has no idle timer, so the test's `finally` reads the pid from the discovery file of the temp DB path (`discovery.read(discovery.daemon_key(path))`), sends SIGTERM if the process still matches, waits until `_read_proc_cmdline(pid)` is empty (never `os.kill(pid, 0)`: the daemon is a never-waited child and stays a zombie), then calls `discovery.remove(key)` because the discovery file lives in the real `~/.saltmdb`.

`tests/test_daemon_client.py` (add only): `_spawn_daemon_process(db, persistent=True)` passes argv `[sys.executable, "-m", "saltmdb.daemon.server", "--foreground"]` to the patched `subprocess.Popen`; without the keyword the argv is unchanged (existing tests keep passing).

`tests/test_cli.py` (add only): `build_parser().parse_args(["daemon", "install-service", "--dry-run", "--no-start"])` carries `dry_run` and `no_start` true and a `func`; each of `daemon start`, `stop`, `status`, `uninstall-service` parses; the parser description is `SALTMDB CLI (read-only data commands plus daemon lifecycle).`; a `daemon status` command function prints the result message and returns its exit code (service function patched to return `ServiceResult(3, "not running")`).

## 5. Documentation

Section 3.5. No `MIGRATION.md` row: no schema, version or MCP tool parameter changes.

## 6. Verification procedure

Targeted first: `.venv/bin/pytest -q tests/test_daemon_service.py tests/test_daemon_client.py tests/test_cli.py`. Then `./verify` once. Test 17 starts a real daemon; run it alone first and make sure no daemon for the test DB path is left running and no `MagicMock` or temp-path artifact is left (`git status --short --ignored | rg -i magicmock` prints nothing).

## 7. Out of scope

- macOS launchd, Windows services, system-wide units, container images.
- Running `loginctl enable-linger` or editing `/etc/wsl.conf`.
- Changing `daemon/server.py` (so no draining flag in `identify`, no foreground flag in the discovery file), `ensure_daemon_running`, the adapter startup path (BL-012), or the viewer.
- Environment passthrough options for the unit (use `systemctl --user edit saltmdb-daemon`).
- Converting a running idle-timer daemon to persistent; making a unit take over from an adapter-spawned daemon sooner (D10); choosing `RestartSec` from data (D7).
- Remote daemon operation (BL-015).

## 8. Acceptance

Run in order at the main checkout with the repo `.venv`:

1. Baseline at lock time (recorded in section 0): `.venv/bin/pytest -q tests/test_daemon_client.py` = 38 passed, 3 subtests passed. After implementation: the same file all passed plus the new test(s).
2. `.venv/bin/pytest -q tests/test_daemon_service.py tests/test_cli.py` all passed (seventeen tests in the first file, section 4, plus the new CLI tests).
3. `.venv/bin/ruff check src tests`, `.venv/bin/ruff format --check src tests`, `.venv/bin/mypy src`: exit 0.
4. `./verify`: exit 0, report counts.
5. `.venv/bin/saltmdb-cli daemon install-service --dry-run` prints the unit from 3.1 with the real interpreter and db path, runs no systemctl command, and creates no file.
6. `git diff --stat` and `git status --short` list only files in section 0 scope; after `./verify`, `git status --short --ignored | rg -i magicmock` prints nothing and no daemon process other than the installed one is left.
7. Informational: `systemctl --user` end-to-end install is a manual check by the owner, not run by the developer.

## 9. Pre-lock gate notes

Done before locking: baseline run (`tests/test_daemon_client.py` 38 passed, 3 subtests); callers of `_spawn_daemon_process` after BL-012 listed (`_spawn_daemon_subprocess` twice, the win32 intermediary `main` in `client.py`, and its tests in `tests/test_daemon_client.py`; none passes anything new, `persistent` is Linux-only); the whole tree (src and tests) grepped for the old parser description (`cli.py` only, no test hardcodes it); `cli.main` read (`sys.exit(args.func(args) or 0)` turns a returned int into the exit code; `SystemExit` passes the `except Exception`); discovery keys read (`daemon_pid`, `service_port`, `started_at`; no foreground flag); `_spawn_daemon_process` already sets `SALTMDB_DB_PATH` from its argument, so test 17 passes the temp path explicitly; shutdown order read in `daemon/server.py` (`handle_request` returns `DAEMON_SHUTTING_DOWN` as soon as `_draining` is set, `identify_response` has no draining state, probe socket closed after the service server, guard closed and `os._exit(0)` last), which is why D5 waits for process exit and D3 waits on a live owner; two narrow probes on the dev host: a never-waited child that has exited reads as state Z with an empty `/proc/<pid>/cmdline` while `os.kill(pid, 0)` still succeeds, and `systemctl --user is-active --quiet` on an unknown unit exits 4 (D5 treats 3 and 4 as not active); wording re-grepped for `starting or stopping`, `ServiceResult`, `_read_proc_cmdline`, `persistent=True`, `DAEMON_START_WAIT_S`, `DAEMON_STOP_WAIT_S` and the exit codes 0, 1, 3 (D1, D6 and section 3.5 agree); the test count (seventeen) matches section 8 item 2; concrete instances walked: a daemon spawned by an agent draining while `start` runs (waits, spawns after three misses, a spawn that loses the still-held election is retried no sooner than `DAEMON_SPAWN_MIN_INTERVAL_S`), a zombie daemon in test 17, `is-active` exit 3 while the unit auto-restarts. Two consultant reviews are incorporated; the second found two blockers (D5 stopped waiting too early, D3 waited on a daemon that had already left), both verified in `daemon/server.py` and fixed.

Post-implementation checks (need the new code, so not run at lock): everything in section 8 except item 1's baseline.
