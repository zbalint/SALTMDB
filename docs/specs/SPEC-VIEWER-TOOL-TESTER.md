# SPEC-VIEWER-TOOL-TESTER

## 0. Status

LOCKED 2026-10-11 after a consultant review (findings applied in sections 1-4).

- Backlog: BL-040 (new). Shared `context_id`: `viewer-tool-tester-2026-10-11`.
- Baseline: `develop` at the commit that contains this spec; `./verify` exit 0 (record counts).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the viewer HTTP handler (`saltmdb.viewer.routes.SALTMDBHandler`, driven the way the
  existing viewer route tests drive it) and a new `ToolRunner` class that takes the command used to
  start the MCP server, so tests inject a small real stdio MCP server instead of mocking.
- Scope (may edit): new `src/saltmdb/viewer/tool_runner.py`; new `src/saltmdb/viewer/routes/tools.py`
  (mixin) and its registration in `src/saltmdb/viewer/routes/__init__.py` and `base.py`'s `do_GET`;
  `src/saltmdb/viewer/templates.py`, `src/saltmdb/viewer/static/viewer.js`,
  `src/saltmdb/viewer/static/viewer.css` (one new "Tools" view); `src/saltmdb/config.py` (one flag);
  `src/saltmdb/viewer/routes/_protocol.py` (declare the two new handler methods, which
  `ViewerHandlerProtocol` needs for mypy); `src/saltmdb/viewer/server.py` (create and close the runner);
  `src/saltmdb/viewer/routes/_shared.py` only if static assets need registering; new tests under
  `tests/`; `docs/architecture.md`, `tests/manual_viewer_smoke.md`, `docs/BACKLOG.md`.
- Does not touch: `mcp/tools.py` and every tool's behaviour, `daemon/` (other than reading what
  `server.py` already passes the viewer), the database schema, hooks.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Users debugging an install cannot easily check what a SALTMDB tool actually does: which arguments
it accepts, what it returns, how long it takes, why a call is rejected. Today they need an agent
session. The owner asked for an interface where a user can test the exposed MCP tools.

Owner decisions (2026-10-11): the interface is a new tab in the existing viewer; the first slice
runs read-only tools only (write tools are listed but disabled); calls run as a fixed `viewer`
agent identity, clearly labelled.

Design decision (architect): the tab drives the real MCP adapter, not the daemon's internal
`DISPATCH_TABLE`. The adapter layer (`mcp/tools.py`) does argument normalization, identity
injection and validation that the daemon table skips, and that is where user-visible tool bugs
live; the tab must show what an agent really sees. The viewer therefore starts one MCP adapter
subprocess (`python -m saltmdb`) with `SALTMDB_AGENT_ID=viewer` and talks MCP stdio to it with the
`mcp` SDK client that is already a dependency (version 1.29 here). Feasibility checked 2026-10-11 on
an isolated temp database: initialize took 0.83 s, `list_tools` returned all 24 tools with
`inputSchema` and `description`, and a call with missing arguments returned the tool's normal
`VALIDATION_ERROR` payload. The subprocess isolates identity (the adapter keeps its identity in a
process-wide singleton, so it cannot be switched per call inside the daemon).

The viewer is read-only by contract (`do_POST` answers 405 "Viewer is read-only"; no existing
test pins it, the new tests do). Calls are therefore exposed as GET requests that can only run read tools, so that
contract stays true. Read tools can still touch bookkeeping such as `last_accessed_at`; the tab
says so.

Rejected: calling `dispatch_tool` in-process (skips the adapter layer; per-call identity would need
a contextvar rework); a POST endpoint (breaks the read-only contract and adds CSRF surface); a
scratch-database mode for writes (separate daemon, larger slice, later); a CLI command (the owner
chose the viewer).

## 2. Decisions

- D1. `GET /api/tools` returns `{"agent_id": "viewer", "tools": [{"name", "description",
  "input_schema", "access": "read"|"write", "enabled": bool}]}` for every tool the adapter lists,
  sorted by name. `access` comes from `saltmdb.daemon.protocol.READ_TOOLS` / `WRITE_TOOLS`; a listed
  tool in neither set is reported as `write` and disabled (fail closed). `enabled` is true only for
  `read` tools.
- D2. `GET /api/tools/call?name=<tool>&arguments=<url-encoded JSON object>` runs one enabled tool and
  returns `{"tool", "agent_id": "viewer", "arguments", "is_error", "duration_ms", "result_text",
  "truncated"}`. `result_text` is the adapter's text content concatenated. Status codes: 400 for a
  missing name, an `arguments` value that is not a JSON object, or one over 8192 characters; 404 for
  an unknown tool; 403 for a write or unclassified tool (nothing is sent to the adapter); 503 when
  the feature is disabled, the runner is closed, the adapter cannot be started, or another call is
  already running (`{"error": "busy"}`, answered at once, never queued); 504 on a 60 s call timeout. A tool's own
  error (`isError` true or a rejected payload) is a 200 response with `is_error: true`.
- D3. `result_text` is capped at 262144 characters; beyond that it is cut and `truncated` is true.
- D4. `ToolRunner(db_path, command=None, env=None, call_timeout_s=60.0, idle_timeout_s=300.0)` owns one
  long-lived adapter subprocess, started lazily on the first request, on one background thread
  with its own asyncio loop (the viewer's request threads are synchronous). One call at a time:
  a second concurrent call is refused at once (busy, see D2), so request threads (capped at 64) are
  never held waiting. `mcp`'s `stdio_client` and `ClientSession` are anyio context managers that
  must be entered and exited in the same task, so the runner runs ONE owner coroutine on its loop
  that holds both and serves a request queue; calls, idle close and `close()` all go through that
  task. After `idle_timeout_s` without a call the subprocess is closed; a crashed or closed
  subprocess is restarted on the next request. On a call timeout the owner task closes and the
  subprocess is restarted on the next request, so no orphaned call keeps running. `close()` sets a
  closed flag first; after it no start is allowed (a request then gets 503). The subprocess is
  started with `cwd` set to the directory of the database file (a neutral directory, so its recorded
  working directory never matches a real project and cannot shadow a project's last-session
  handover). Default command is `[sys.executable, "-m", "saltmdb"]`; default env is the
  daemon's environment plus `SALTMDB_AGENT_ID=viewer` and `SALTMDB_DB_PATH=<db_path>` and no
  other change. The runner object is created in `SALTMDBTCPServer.__init__` (so concurrent first requests cannot
  create two), the subprocess still starts lazily, the database path is read at first start from
  `self.viewer_gateway.db_path`, and `server_close` closes the runner then calls `super()`.
  Consequences to document: the adapter registers a daemon session, so the daemon stays up for
  up to `idle_timeout_s` plus its 30 s grace (330 s) after the last tool call and cannot restart on
  a redeploy until then; each adapter start adds a `viewer` row to Agent Sessions; during daemon
  shutdown the adapter may respawn a successor daemon that has no sessions and exits after its
  grace (bounded churn, accepted; `daemon/server.py` is not changed in this slice).
- D5. A new config flag `SALTMDB_VIEWER_TOOLS_ENABLED` (default true) in `config.py`, read like the
  existing viewer flags; when false both endpoints return 503 `{"error": "Tool tester disabled"}`.
- D6. The Tools view (vanilla JS, no new dependency, strict CSP unchanged): a list of tools with a
  read/write badge, the description, an arguments editor (a textarea prefilled with a skeleton built
  from `input_schema`: required properties with a placeholder per JSON type), a Run button disabled
  for write tools with the text "write tool, disabled in this version", and a result panel showing
  status (ok, tool error, HTTP error), duration, and the raw result text, pretty-printed when it
  parses as JSON. A fixed banner states: "Calls run as agent `viewer`. Read tools can update access
  bookkeeping."
- D7. The view state follows the existing hash convention (`#tools` and `#tools/<name>`).
- D8. Both endpoints are GET only and guarded against cross-site use, in this order, each failure
  a 403 `{"error": ...}` with nothing sent to the adapter: (a) the request must carry the header
  `X-SALTMDB-Tool-Tester: 1` (a cross-origin page cannot add it without a CORS preflight, and
  `do_OPTIONS` allows only `Content-Type`, so the preflight fails); (b) if `Sec-Fetch-Site` is
  present it must be `same-origin`; if `Origin` is present it must equal `http://<Host header>`;
  (c) the `Host` header must be exactly `localhost:<viewer port>` or `127.0.0.1:<viewer port>`, a
  missing Host is refused. The page's own JS sends the header on every call. Do not add the new
  paths to any CORS allow list.

## 3. Changes per file

- `src/saltmdb/viewer/tool_runner.py`: `ToolRunner` per D4. Use `mcp.ClientSession` and
  `mcp.client.stdio.stdio_client`. No new helper beyond what the class needs. A `list_tools()` and a
  `call_tool(name, arguments)` sync API for the handler; both raise a small `ToolRunnerError`
  (with a `kind`: `unavailable`, `timeout`) that the route maps to 503 and 504.
- `src/saltmdb/viewer/routes/tools.py`: `ToolsMixin` with `get_tools()` and `get_tool_call(query)`
  per D1-D3 and D8, reading the runner from `self.server`. Add the two `elif` branches in
  `base.py` `do_GET`. Register the mixin in `routes/__init__.py`.
- Where the viewer server is constructed (read how `daemon/server.py` builds `SALTMDBTCPServer` and
  injects `viewer_gateway`/`daemon_state`; do not edit `daemon/server.py` unless the runner cannot
  be attached any other way, and if it must, stop and report BLOCKED rather than widening scope):
  nothing in `daemon/` changes: `SALTMDBTCPServer.__init__` creates the runner (D4) and
  `server_close` closes it (both in `viewer/server.py`, in scope).
- `src/saltmdb/viewer/routes/_protocol.py`: add `get_tools` and `get_tool_call` to
  `ViewerHandlerProtocol`, in the file's existing style.
- `templates.py`, `viewer.js`, `viewer.css`: the Tools view per D6-D7, in the file's existing style.
- `config.py`: `get_viewer_tools_enabled()` beside the existing viewer getters.

## 4. Tests

Behaviour only, no mock echoes, written failing first. A small real MCP stdio server for tests:
a script under `tests/fixtures/` built with the `mcp` SDK's `FastMCP` exposing three tools whose
names match real ones for classification (`search_tags` read, `store_memory` write) plus
`mystery_tool` (in neither set), with one that sleeps longer than a short test timeout and one that
raises.

1. `ToolRunner` against the fixture server: `list_tools` returns the three tools with schemas;
   `call_tool` returns text and `is_error` false; a raising tool gives `is_error` true; a call over
   `call_timeout_s` raises `ToolRunnerError(kind="timeout")`; after the subprocess is killed the next
   call restarts it and succeeds; after `idle_timeout_s` the subprocess is gone and the next call
   starts a new one; after a timeout the subprocess was restarted (a second call works and the
   slow tool is no longer running); the subprocess's working directory is the directory of the
   database file (the fixture server reports its cwd); `close()` leaves no child process and later
   starts are refused.
2. Handler tests (drive `SALTMDBHandler` the way existing viewer route tests do): `GET /api/tools`
   shape and `access`/`enabled` for read, write and unclassified tools; `GET /api/tools/call` for a
   read tool returns 200 with `result_text`; a write tool and an unclassified tool return 403 and the
   fixture server records no call; unknown tool 404; bad `arguments` JSON, a JSON array, and an
   over-length value 400; a missing `X-SALTMDB-Tool-Tester` header, a foreign or missing `Host`, a mismatched `Origin` and
   a `Sec-Fetch-Site: cross-site` each 403 with the fixture server recording no call; a second
   call while one is running gets 503 busy at once; after `server_close` a request gets 503; the flag off gives 503 for both endpoints;
   a result over the cap is truncated with `truncated` true; `do_POST` still answers 405.
3. One integration test against the real adapter on an isolated temp database and temp home
   (follow the pattern of the existing daemon subprocess tests): `list_tools` returns 24 tools, every
   one is in `READ_TOOLS` or `WRITE_TOOLS`, and a real `search_tags` call returns without error. Skip
   with a clear reason only if the existing suite's pattern for such tests says so.
4. The front end has no unit tests in this repo; the nav button markup follows the existing contract test
   (`<button data-view="tools" class="nav-item">Tools</button>`); `tests/manual_viewer_smoke.md` gets
   a Tools section with the steps (list shows 24 tools; run `search_tags`; a write tool's Run is disabled; result
   panel shows duration; banner visible).

## 5. Documentation

`docs/architecture.md`: a short paragraph in the viewer section describing the Tools view, the
`viewer` identity, that it drives the real MCP adapter subprocess, read-only scope, and the two
endpoints. `tests/manual_viewer_smoke.md` per section 4. `docs/BACKLOG.md`: BL-040 (this, implemented,
awaiting review) and BL-041 (open): write tools on a scratch database, a copy-as-curl or MCP
JSON snippet, and a call history.

## 6. Verification procedure

Default `./verify`; no parallel full runs. Acceptance 4 uses an isolated daemon (temp home, a copy
of a database, a free viewer port), never the live daemon.

## 7. Out of scope

Write tools, a scratch database mode, per-call agent identity, call history, saving requests,
authentication, any change to a tool's behaviour or description, a POST endpoint.

## 8. Acceptance

1. Clean tree before editing (exclude only attributed architect files); `./verify` exit 0, counts.
2. New tests red before the production change, green after.
3. `./verify` exit 0 after, counts recorded.
4. Isolated end-to-end check: start a daemon on a database copy with the viewer on a free port in a
   temp home, then `curl` `/api/tools` (24 tools, read tools enabled, write tools disabled),
   `/api/tools/call?name=search_tags&arguments=%7B%7D` (200, `is_error` false), the same call for
   `store_memory` (403), and report the first-call and second-call `duration_ms`. Stop the daemon and
   confirm no stray adapter or daemon process remains.
5. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

- Consultant review 2026-10-11 (saltmdb-consultant, grounded in code with path:line): applied H1
  (CSRF: custom header, Sec-Fetch-Site/Origin/Host checks, no queueing), H2 (`_protocol.py` in
  scope), M1 (daemon lifetime documented), M2 (closed flag and bounded respawn accepted, no daemon
  edit), M3 (neutral cwd), M4 (restart after timeout), M5 (owner coroutine), M6 (runner created in
  `__init__`), L1 (no test pins the 405), L2 (nav markup). Not verified by the consultant and to be
  confirmed by the developer while implementing: the SDK's `stdio_client` working-directory
  behaviour (the `cwd` field exists on `StdioServerParameters` in mcp 1.29.0, checked by the
  architect) and the anyio single-task rule (read the SDK source before writing the runner).
- Scope list vs acceptance: every file in section 0 appears in section 3, 4 or 5.
- Data shapes: the two endpoint response shapes are defined once (D1, D2) and referenced by name.
- The viewer front end has no unit tests; its acceptance is the manual smoke steps and acceptance 4.
