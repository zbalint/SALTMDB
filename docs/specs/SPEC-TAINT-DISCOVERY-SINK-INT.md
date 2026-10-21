# SPEC-TAINT-DISCOVERY-SINK-INT

## 0. Status

LOCKED 2026-10-21 (the pre-agreed follow-up named in section 7 of SPEC-TAINT-DISCOVERY-TYPED-RECORD, whose consultant review m_2793 proposed it).

- Backlog item: none. Sonar `pythonsecurity:S5144` ("Server-Side Request Forgery via file content") stayed OPEN at `src/saltmdb/daemon/client.py` `_authenticated_ping_ok` (about lines 423-425), `_send_call_request` (587-589) and `_open_session_attempt` (788-791) on `develop` `6d676fd`, after `discovery.read` began returning a rebuilt typed record. The engine may taint the returned dict as a whole (it also carries the `str` token), so the port still looks tainted at the sink. The conversion that cleared S5145 was a typed `int` argument at the point of use. Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decisions: real hardening, no suppression, no Accept/Safe where code can fix; a finding that stays flagged after a correct fix is reported to the owner.
- Baseline: `develop` at `6d676fd` or later; `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_daemon_server.py::TestDaemonSignalShutdown`; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the three functions with `client.socket.create_connection` patched (the idiom of `tests/test_daemon_client.py`, for example the patch at about line 192), and `SessionConnection`-level tests where `_open_session_attempt` is reached.
- Scope (may edit): `src/saltmdb/daemon/client.py` (the three connect calls only) and a NEW test file `tests/test_daemon_sink_port.py`.
- Does not touch: `discovery.py`, any existing test, any other line of `client.py`, the hooks.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`discovery.read` already validates and rebuilds the record, so the port is an `int` in 1..65535 by construction. Applying `int(...)` again at the three `socket.create_connection` calls is redundant at runtime and costs nothing, but it puts the typed conversion at the exact point where the value reaches the network call, which is the shape the engine accepted for S5145 and is immune to how it models the returned dict. Rejected: taking the port from the election or probe channel (any local user can bind the probe port while the daemon is down, so a port learned there would hand the auth token to a squatter); suppressing the finding.

## 2. Decisions

- D1. In `client.py`, the three calls `socket.create_connection(("127.0.0.1", info["service_port"]), ...)` become `socket.create_connection(("127.0.0.1", int(info["service_port"])), ...)`. Nothing else in the three functions changes: not the timeouts, not the `try`/`except` structure, not the token handling. `int(...)` of an already-validated `int` cannot raise; do not add any new handling.
- D2. Complexity and literals: no function's cognitive complexity changes (no new branch); two `client.py` functions sit exactly at 15 today, so report complexipy before/after for the three functions and stop if any score rises. No suppression, no `NOSONAR`.
- D3. New tests (literal expected values), in `tests/test_daemon_sink_port.py`, with `client.socket.create_connection` patched to a recording mock and the rest of each function mocked just enough to run (copy the closest existing idiom): for each of the three functions, pass an `info` whose `service_port` is an `int` SUBCLASS (`class _Port(int)`), and assert the first positional argument of the recorded call is exactly `("127.0.0.1", 4000)` AND `type(call.args[0][1]) is int` (a plain `int`, not the subclass). This fails without D1 because the subclass object would be passed through unchanged.
- D4. Mutation sample (tester, scratch copy, import-pinned): remove the `int(...)` at each of the three sites (each must fail its own test); change the host literal; pass the port as `str(...)`.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/daemon/client.py` | `int(...)` around `info["service_port"]` at the three connect calls (D1) |
| `tests/test_daemon_sink_port.py` | new (D3) |

## 4. Tests

D3; the existing `tests/test_daemon_client.py` cases must stay green unchanged.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`; the new test file alone; `ruff check --select ARG,F,RET` on both files (no unused arguments in the new test); complexipy on `client.py`.

## 7. Out of scope

Everything else in `client.py`; a server-proves-itself challenge for stale-port squatters (backlog). Architect, after CI and the Sonar scan: S5144 at the three lines closed or still open; if still open after this, report to the owner with the evidence (the port is an owner-only-file `int` converted at the sink; the residual flow is a loopback connection to a port read from a 0600 file verified by uid and mode).

## 8. Acceptance

1. `./verify` exits 0. 2. `git status` lists only the two files of section 3. 3. Complexity before/after of the three functions. 4. Mutation sample of D4. 5. Architect: Sonar gate OK after CI, S5144 status reported.
