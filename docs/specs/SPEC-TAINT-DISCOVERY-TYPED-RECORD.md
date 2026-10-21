# SPEC-TAINT-DISCOVERY-TYPED-RECORD

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none. Sonar `pythonsecurity:S5144` ("Server-Side Request Forgery via file content") at `src/saltmdb/daemon/client.py` `_authenticated_ping_ok` (issue lines 423-425), `_send_call_request` (587-589) and `_open_session_attempt` (788-791) on `develop` `a361f97`. Source: the discovery JSON file; sink: `socket.create_connection(("127.0.0.1", port))`. After SPEC-TAINT-DISCOVERY-VALIDATION the two S5145 findings cleared but these three stayed: `discovery.read` returns the SAME parsed `json.load` object after a separate boolean check `_valid`, which the taint engine does not treat as a sanitizer (a boolean guard in another function did not satisfy it; typed int arguments did, for S5145). Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decisions: real hardening, no suppression, no Accept/Safe where code can fix; a finding that stays flagged after a correct fix is reported to the owner.
- Baseline: `develop` at `782b410` or later; `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_relation_service.py` temp-dir race; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `discovery.read(key)` with a temporary `HOME` (the idiom of `tests/test_discovery_validation.py`), and `client.reachable_daemon_info`.
- Scope (may edit): `src/saltmdb/daemon/discovery.py` (`read`, one small private helper and one new key constant), `tests/test_discovery_validation.py` (the two deliberate assertion changes of D3 and new tests only).
- Does not touch: `client.py`, `viewer/server.py`, the write path, `_valid`'s rules, `_owned_file`, the hooks' own discovery reader (`hooks/saltmdb-stop-trace-capture.py`), any other test.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`discovery.read` is the single trusted source of the daemon port and token. It validates the parsed JSON but returns the raw parsed object; callers then pass `info["service_port"]` to `socket.create_connection`. Decision (consultant m_2720): return a NEW record rebuilt from the validated fields with explicit conversions (`int(...)`, `str(...)`), dropping every unknown key, so callers can only ever see validated keys and types. This is real hardening regardless of the Sonar outcome (unknown keys no longer flow to callers, the boundary is explicit in the code). Rejected: taking the port from the election/probe channel (the service port is OS-assigned per start so it cannot be derived from the database path, the probe port is freely bindable by any local user while the daemon is down so trusting a port learned there would hand the auth token to a squatter, and a socket read is itself a taint source); changing the callers (their `info[...]` accesses stay valid because the returned dict keeps the same keys).

## 2. Decisions

- D1. `read(key)` keeps its signature `dict[str, Any] | None`, its docstring semantics and every existing rejection (missing/unowned/world- or group-writable file, bad JSON, non-UTF-8, `_valid` false give `None`). When the record is valid it returns a NEW dict built only from these keys: `service_port` as `int(...)`, `auth_token` as `str(...)`, and, only when present in the file, `daemon_pid` as `int(...)` and `db_path` as `str(...)`, and `started_at` only when present AND a `str` that `datetime.fromisoformat` parses, rebuilt as `datetime.fromisoformat(value).isoformat()` (omitted on `ValueError`/`TypeError`; `service.py` prints `started_at` in the daemon status line, so dropping it would silently print `None`; the rebuild is a real normaliser that strips control or escape text before it reaches a terminal; `write()` produces the value with `datetime.now(UTC).isoformat()`, which round-trips). Add a module constant `_STARTED_AT`. Every other key of the file (including `schema_version`, which no caller reads) is dropped. Use the existing module key constants (`_SERVICE_PORT`, `_AUTH_FIELD`, `_DAEMON_PID`, `_DB_PATH`); the construction lives in one small private helper (`_typed_record(data) -> dict[str, Any]`) so `read` stays simple. The conversion and the use of the converted values happen on the VALUE that is returned (no boolean guard between validation and the returned values beyond `_valid`, which stays as is).
- D2. Complexity and literals: keep `read` at its current cognitive complexity or lower; the helper at or below 5; no string literal repeated 3+ times (use the existing constants). Report complexipy scores before/after. The helper may reach about 4-6 with the `started_at` branch: if it exceeds 6, split the `started_at` normaliser into its own tiny function.
- D3. Two existing assertions of `tests/test_discovery_validation.py` change deliberately, and only these two: (a) `test_read_accepts_valid_record` currently expects `read(...) == data` with an extra key (`"extra": []`); it now expects the record WITHOUT the extra key; (b) `test_read_returns_loaded_dict_identity` asserts `read(...) is sentinel`, which is the behaviour being removed; replace it with a test that the returned dict is NOT the object `json.load` returned (patch `json.load` as the old test does) and equals the typed fields. Every other test of the file stays byte-identical.
- D4. New tests (literal expected values; the key names `schema_version`, `started_at` and `extra` are module constants of the test file because they repeat 3+ times): unknown keys (`schema_version`, `extra`) are dropped; present optional keys are kept with their types; absent optional keys stay absent (no `None` placeholders); `started_at` is kept and equal for a `write()`-shaped value, dropped for `"not-a-date"` and for a non-`str`, and a value containing a control character before a valid date is normalised or dropped (state which; no raw control text survives); the returned dict is a new object each call; mutating the returned dict does not affect a second `read`. The type tests cannot go through a real file (`json.load` never yields subclasses, and `_valid` accepts an `int` subclass): patch `json.load` (the idiom of the replaced identity test) to return values of `class _S(str)` and `class _I(int)` and assert `type(record["auth_token"]) is str`, `type(record["service_port"]) is int` and `type(record["daemon_pid"]) is int`; without the `_I` case the "drop int()" mutant is equivalent. One end-to-end case: a real file with extra keys plus a patched `client._authenticated_ping_ok` returning true; `client.reachable_daemon_info(db_path)` returns a record without the extra keys (this is the `reachable_daemon_info` seam).
- D5. Callers: `client.py`, `service.py` and `viewer/server.py` are NOT edited. Read-only check by the developer: every `info[...]`/`info.get(...)` access in `client.py`, `service.py` and `viewer/server.py` whose `info` comes from `discovery.read` (directly or through `reachable_daemon_info` / `ensure_daemon_running`) uses only the five kept keys; accesses on the identify-probe dict (`info = _identify_probe(key)`, for example `client.py` about lines 239 and 452-458 reading `db_path` and `state`) are NOT discovery-derived and are exempt, but list them in the report as such (`service_port`, `auth_token`, `daemon_pid`, `db_path`, `started_at`; list them in the report with line numbers; the consultant found `service.py:388` reading `started_at`); if any caller reads another key, stop and report `BLOCKED`.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/daemon/discovery.py` | `_typed_record` helper; `read` returns it (D1) |
| `tests/test_discovery_validation.py` | the two changed assertions (D3) and the new tests (D4) |

## 4. Tests

D3 and D4. Mutation sample by the tester (scratch copy, import-pinned): return the raw `data` again; keep unknown keys; drop the `int()`/`str()` conversions; always include `daemon_pid`/`db_path` with a default; return a shared module-level dict; drop `db_path` when present. Each fails a test.

## 5. Documentation

None expected; if `docs/architecture.md` quotes the discovery record contents, update only an exact quotation.

## 6. Verification procedure

`./verify`; `tests/test_discovery_validation.py` alone; `ruff check --select ARG,F,RET` on both files against the baseline; complexipy on `discovery.py`.

## 7. Out of scope

Pre-agreed follow-up slice, triggered ONLY if the scan still flags the three lines after this change (the engine may taint the returned dict as a whole, because it also carries the `str` token): apply `int(info["service_port"])` at the three sinks in `client.py` (the shape that cleared S5145). It is NOT part of this slice. A server-proves-itself challenge for stale-port squatters (backlog), the hooks' own discovery reader, Windows ownership checks. Architect, after CI and the Sonar scan: S5144 at the three lines closed or still open; if still open, report to the owner with the evidence (the explicit conversions are the engine's documented sanitizing shape; the remaining flow is port from an owner-only file verified by uid and mode).

## 8. Acceptance

1. `./verify` exits 0. 2. `git status` lists only the two files of section 3. 3. The caller key list of D5. 4. Mutation sample of section 4. 5. Architect: Sonar gate OK after CI, S5144 status reported.

Amendment 1 (2026-10-21, developer BLOCKED m_2854, verified by the architect): the D5 audit covers only discovery-derived `info` values; the identify-probe dict (which carries `state`) is a different source and is exempt (inventory it in the report).
