# SPEC-VIEWER-HOST-VALIDATION

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none yet (architect adds the row on acceptance). Found by the consultant's S5131 survey: the viewer never validates the `Host` header, so a DNS-rebinding page can read `/api/*` as same-origin. Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Not a Sonar finding.
- Baseline: `develop` at `c332b73` or later; `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, a temp-dir cleanup race in `tests/test_relation_service.py`; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: a real `SALTMDBTCPServer(("127.0.0.1", 0), SALTMDBHandler)` served in a thread, driven with `http.client.HTTPConnection` and raw sockets (see `tests/test_viewer_server.py` line 37 for the construction; copy the idiom, do not import from it). Accepted requests use `/static/viewer.css` (no database needed); rejected requests may use `/api/...` paths because they must never reach a route.
- Scope (may edit): `src/saltmdb/viewer/routes/base.py` (one new method and one constant), the NEW file `tests/test_viewer_host_validation.py`, `docs/architecture.md` (one sentence in the viewer section), `SECURITY.md` only if it already lists viewer protections (one clause).
- Does not touch: any existing test, the route dispatchers, `send_json`, `send_html`, the CORS code, the CSP, the daemon, the static files.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The viewer binds `127.0.0.1` on a fixed port and serves memory content under `/api/*`. A web page on any origin can make the visitor's browser resolve `attacker.example` to `127.0.0.1` (DNS rebinding); the browser then treats requests to `attacker.example:<port>` as same-origin and the page can read the API responses. The CORS code does not help because a rebound request is not cross-origin. The standard defence is to reject any request whose `Host` header is not one of the viewer's own loopback names. Rejected alternatives: checking `Origin` only (rebound same-origin requests carry no cross-origin signal, and GET often has no `Origin`); a per-route check (violates the single choke point rule: an unchecked route would stay open); a token in the URL (changes the user flow, much larger change).

## 2. Decisions

- D1. One choke point: override `parse_request` in `ViewerHandlerBase`. Call `super().parse_request()`; if it returns false, return false; otherwise validate the `Host` header and, on failure, call `self.send_error(421, "Misdirected Request")` and return false. `BaseHTTPRequestHandler.handle_one_request` calls `parse_request` before dispatching any method (GET, HEAD, POST, OPTIONS, and unknown verbs), so every entry passes through it. No other code path calls a `do_*` method without it. This holds while `protocol_version` is `HTTP/1.0` (the viewer never sets it): `handle_expect_100` runs inside `super().parse_request()` only for HTTP/1.1, so also override `handle_expect_100` with the same `_host_allowed()` check first (three lines) so a later protocol bump cannot bypass the choke point.
- D2. Allowed `Host`: exactly one `Host` header (use `self.headers.get_all("Host")` and require a list of length 1; zero or several are rejected). Strip only leading and trailing space and tab, then: if the value starts with `[`, the host is everything up to and including the first `]` and the rest must be exactly `:` plus the port text; otherwise split on the LAST `:` and the host part must contain no `:`. The host (lowercased, host part only) must be exactly `localhost`, `127.0.0.1` or `[::1]` (so a trailing dot, userinfo, suffixes and prefixes are rejected). The port text must be string-equal to `str(port)` where `port = self.server.server_address[1]`: do NOT use `int()` or `isdigit()` (they accept `+8080`, `08080`, `8_080`, non-ASCII digits). A value with no port, an empty port or inner whitespace is rejected. Narrow `server_address` with `isinstance(addr, tuple) and len(addr) >= 2` before indexing and deny otherwise (mypy and D5).
- D3. The allowed hostnames live in one module constant (a tuple of the three strings), reused if the CORS code later adopts it; the CORS code is NOT changed in this spec.
- D4. The 421 response (deliberately without the dynamic `no-store`/`nosniff` headers: the body is the static fixed text) goes through `send_error`, which writes a small HTML body. That body must not echo the `Host` value (the standard `send_error` message is the fixed phrase passed in; verify no header text is reflected). The response carries no memory content.
- D5. A handler built without a server (the existing unit fixtures call route methods directly and never call `parse_request`) is unaffected. If `self.server` has no `server_address`, validation denies.
- D6. No change to bind address, CORS, CSP, or any route.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/viewer/routes/base.py` | add the allowed-hostnames constant, a private `_host_allowed()` and `parse_request` per D1 and D2 |
| `tests/test_viewer_host_validation.py` | new (section 4) |
| `docs/architecture.md` | one sentence: the viewer rejects requests whose Host is not loopback plus its own port (DNS-rebinding defence) |

## 4. Tests

(Consultant amendments folded in: string port equality, valid-first duplicate Host, mutant-killing cases below.)

Written failing first, literal expected values. Real server on port 0; the test reads the port from `server.server_address[1]`.

- Accepted (200 on `/static/viewer.css`, GET and HEAD): `Host: 127.0.0.1:<port>`, `localhost:<port>`, `LOCALHOST:<port>`, `[::1]:<port>`.
- Rejected with 421 for GET, HEAD, POST and OPTIONS (one test per verb, each on an `/api/` path or `/`): `Host: attacker.example:<port>`, `127.0.0.1` (no port), `127.0.0.1:<port+1>`, `localhost.attacker.example:<port>`, `127.0.0.1:abc`, `` (empty), a missing Host header (raw HTTP/1.0 request with no headers), and two `Host` headers (raw socket).
- More rejected cases: `localhost:+<port>`, `localhost:0<port>`, a non-ASCII-digit port (for example the Arabic-Indic digits of the port), `localhost.:<port>`, `user@localhost:<port>`, `evil-localhost:<port>`, `127.0.0.1.attacker.example:<port>`, `::1:<port>`, `[::1]` (no port), `localhost:` (empty port), `localhost:<port> x` (inner whitespace). Accepted with outer whitespace: `Host:  localhost:<port> `.
- Two `Host` headers over a raw socket, BOTH orders: valid first then `attacker.example:<port>`, and the reverse; both get 421.
- The server runs on port 0 while `SALTMDB_VIEWER_PORT` defaults to 8080, so a mutant that compares against the configured port instead of `server_address[1]` fails (the tester names this killer).
- Unit test of D5/`_host_allowed()`: a handler whose `server` lacks `server_address` denies; a stub with a non-tuple `server_address` denies.
- A rejected POST must not reach `do_POST`'s `send_json` (patch it) and a rejected GET must not reach `get_db_connection` or `send_static_asset`.
- The body-not-echoing test uses GET (HEAD has no body). A raw HTTP/0.9 request (`GET /api/stats\r\n\r\n`) runs no route (assert on the mocks, not on a status code).
- A rejected request never reaches a route: patch `SALTMDBHandler.get_db_connection` (and `send_static_asset`) to a mock that fails the test if called, and assert it was not called for a rejected `/api/stats` and `/static/viewer.js`.
- The 421 body does not contain the sent Host value (use a distinctive host such as `rebind-probe.example`).
- An unknown verb (`PATCH`) with a bad Host gets 421, not 501 (the choke point runs first).

## 5. Documentation

The architecture sentence above; update only an exact quotation that becomes wrong.

## 6. Verification procedure

`./verify`; the new test file alone; `ruff check --select ARG,F,RET` on `base.py` against the baseline; complexipy on `base.py` (no function above 15, no existing function's score raised). Manual evidence without a daemon: run the handler in a scratch `SALTMDBTCPServer` on an ephemeral port and `curl -s -o /dev/null -w '%{http_code}'` with `-H 'Host: attacker.example:<port>'` (expect 421) and with the default Host (expect 200 on `/static/viewer.css`).

## 7. Out of scope

Breakage to document (architecture sentence): an SSH or reverse tunnel that changes the local port (for example `ssh -L 9000:localhost:8080`) or a proxy that rewrites `Host` is now rejected; use the same port number in the tunnel. If a non-loopback bind is ever wired up (`SALTMDB_VIEWER_HOST` exists but is not consumed), the allowlist must be extended deliberately in the same change. Pre-existing and separate: with `SALTMDB_VIEWER_PORT=0` the daemon stores 0 as the viewer port. Server proof for the daemon service channel, viewer port squatting, reading the file before `send_response` in `send_static_asset`, CORS tightening.

## 8. Acceptance

(Mutation sample also includes: parse the port with `int()`; accept a missing Host; first-value-only `headers.get`; lowercase the whole value; skip `handle_expect_100`.)

1. `./verify` exits 0. 2. `git status` lists only the files of section 3 (new test untracked). 3. The curl evidence of section 6. 4. Mutation sample on a scratch copy (tester names the killers): drop the port comparison; accept a missing Host; remove the lowercase; validate in only `do_GET`; echo the Host in the body. Each fails a test of the new file. 5. Architect after CI: Sonar gate stays OK and no new issue.
