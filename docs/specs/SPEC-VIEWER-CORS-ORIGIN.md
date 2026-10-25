# SPEC-VIEWER-CORS-ORIGIN

## 0. Status

LOCKED 2026-10-25 (consultant review folded in).

- Backlog item: none yet (the architect adds the row on acceptance). Found by the GitHub code-scanning alerts #5 and #6 (CodeQL `py/http-response-splitting`, medium) on `src/saltmdb/viewer/routes/base.py` lines 109 and 155. Shared `context_id`: `saltmdb-viewer-origin-2026-10`.
- Baseline: `develop` at `b0dcaed` or later. Narrow baseline run at lock time: `pytest tests/test_viewer_host_validation.py tests/test_viewer_response_headers.py tests/test_viewer_server.py tests/test_viewer_routes.py` gave 74 passed, 32 subtests passed. `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, a temp-dir cleanup race in `tests/test_relation_service.py`; rerun a failing one alone once, then record it as pre-existing).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `ViewerHandlerBase.send_json` and `do_OPTIONS` through a `SALTMDBHandler` built with a dummy request and a recording `send_header` (the idiom of `tests/test_viewer_response_headers.py` lines 16 to 25), and a real `SALTMDBTCPServer(("127.0.0.1", 0), SALTMDBHandler)` driven with a raw socket (the idiom of `tests/test_viewer_host_validation.py` lines 21 to 56). Do not invent another harness.
- Scope (may edit): `src/saltmdb/viewer/routes/base.py` (one new import, one new module constant, one new method, and the two `Origin` blocks in `send_json` and `do_OPTIONS`); the NEW file `tests/test_viewer_cors_origin.py`; `docs/architecture.md` (one sentence after the existing Host-validation sentence at line 207).
- Does not touch: any existing test, the route dispatchers, `send_html`, `send_static_asset`, `parse_request`, `_host_allowed`, `_send_dynamic_headers`, the CSP, the daemon, the static files, `docs/BACKLOG.md`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`send_json` and `do_OPTIONS` echo the request's `Origin` header into `Access-Control-Allow-Origin` when its hostname is `localhost` or `127.0.0.1`. The check parses the value with `urllib.parse.urlparse`, which silently drops ASCII tab, CR and LF before it extracts the hostname, but the header that is sent is the original string. A request with a folded header, `Origin: http://localhost/` followed by a continuation line ` X-Evil: 1`, passes the check and reaches `send_header` with an embedded CR LF, which Python 3.11's `http.server` writes into the response unchanged (probed on 3.11.15: the buffered line is `Access-Control-Allow-Origin: http://localhost/\r\n X-Evil: 1\r\n`). The continuation starts with whitespace, so it is an obsolete line fold of the same header, not a new header or a body: the practical impact is low, the viewer binds loopback and now rejects foreign `Host` values, and a browser cannot send a folded header. The invariant is still broken (control characters from the request reach a response header), the two CodeQL alerts are technically valid, and the same unchecked block is written twice.

Decision: keep the existing CORS behaviour for real browser origins and make the echo safe. A real browser `Origin` is `scheme://host[:port]` with no path, query or whitespace, so the echo is limited to exactly that canonical form on the two loopback names, and the duplicated block becomes one method. Rejected alternatives: removing the CORS echo altogether (the viewer's own page is same-origin and uses no CORS, but removing it changes which local pages may read `/api/*`, which is the owner's call and is offered separately, not part of this spec); stripping CR and LF from the value and echoing the rest (still echoes paths and queries, and hides the finding instead of narrowing the input); adding `[::1]` (the old code never allowed it, because `urlparse(...).hostname` returns `::1` without brackets, which is not in the tuple; this spec does not widen the allow list).

## 2. Decisions

- D1. One module constant in `base.py`: a compiled regular expression `_LOOPBACK_ORIGIN` equal to `https?://(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?`, used only with `fullmatch` (never `match` or `search`, so a trailing newline cannot slip past `$`-style anchoring).
- D2. One private method `ViewerHandlerBase._send_cors_origin_header(self)`, no arguments besides `self`, no return value. It reads the first `Origin` header with the same defensive lookup the old blocks used (`getattr(self, "headers", None)`, empty string when absent), and sends `Access-Control-Allow-Origin` with the unchanged value only when `_LOOPBACK_ORIGIN.fullmatch(origin)` matches. In every other case it sends nothing and does not raise. It sends no other header.
- D3. `send_json` and `do_OPTIONS` each replace their inline `Origin` block (the `headers`/`origin`/`urlparse`/hostname lines) with one call to `self._send_cors_origin_header()` at the same position, so the order of headers in each response is unchanged. `Access-Control-Allow-Methods: GET, OPTIONS` and `Access-Control-Allow-Headers: Content-Type` in `do_OPTIONS`, the status codes and the `except` clauses are not changed.
- D4. Behaviour for the canonical form is byte-identical to before: an `Origin` of `http://localhost`, `https://localhost`, `http://localhost:8765`, `http://127.0.0.1:3000` is echoed exactly. Behaviour narrows for every input that is not a canonical loopback origin, including the non-canonical values the old code echoed (a path, a query, a non-numeric or empty port, a port longer than 5 digits, a scheme other than http and https, upper-case schemes, whitespace, control characters).
- D5. `urllib.parse` stays imported (`do_GET` and other code use it); add `import re` in the standard-library import group in alphabetical position. No other import changes.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/viewer/routes/base.py` | `import re`; the `_LOOPBACK_ORIGIN` constant beside `_ALLOWED_HOSTNAMES` (line 29); the new method `_send_cors_origin_header` placed directly before `send_json` (line 99); in `send_json` (lines 104 to 109) and in `do_OPTIONS` (lines 150 to 155) replace the six inline lines (`headers = ...`, `origin = ...`, `if origin:`, `parsed_origin = ...`, `if parsed_origin.hostname in (...):`, and its `send_header` line) by the single call per D3 |
| `tests/test_viewer_cors_origin.py` | new (section 4) |
| `docs/architecture.md` | one sentence directly after line 207: the viewer echoes `Access-Control-Allow-Origin` only for a canonical `http` or `https` origin on `localhost` or `127.0.0.1` (optional numeric port), never a value containing a path, query or control character |

Every caller of the changed code: `send_json` and `do_OPTIONS` are called as methods (no other module imports them; `grep -rn "send_json\|do_OPTIONS" src` finds only calls through `self`/`handler`); no rename, no signature change, so no import or `mock.patch` target changes.

## 4. Tests

Written failing first. Literal expected values; no value is recomputed with the logic under test. File `tests/test_viewer_cors_origin.py`, `unittest` style like the neighbouring viewer tests, two classes.

Class `CorsOriginHandlerTests` (recording-handler seam). For each case build `SALTMDBHandler(DummyRequest(), ("127.0.0.1", 8080), DummyServer())`, set `handler.headers` to an `email.message.Message` with the single header `Origin` (omit the header for the empty case), record `send_header` into a dict, call `send_json({})` or `do_OPTIONS()`, and run each case for BOTH methods with `subTest`.

- C1 (contract; passes on the old code). These origins are echoed exactly: `http://localhost`, `https://localhost`, `http://localhost:8765`, `http://127.0.0.1:3000`, `https://127.0.0.1:65535`. The recorded `Access-Control-Allow-Origin` equals the input string.
- C2 (abuse and narrowing; the cases marked * fail on the old code). One test method per response method (`send_json`, `do_OPTIONS`), each looping the values below with `subTest`. The header is absent for: `http://evil.example`, `http://localhost.evil.example`, `http://localhost@evil.example`, `null`, the missing header, a handler with no `headers` attribute at all (the unit fixtures build the handler that way), `http://[::1]:8765`, `http://127a0a0a1` (pins the escaped dots of D1), `http://localhost:٨٧` (Arabic-Indic digits, U+0668 U+0667)* (pins `[0-9]` over `\d`), `HTTP://localhost`*, `http://localhost/`*, `http://localhost/path`*, `http://localhost?x=1`*, `http://localhost:`*, `http://localhost:abc`*, `http://localhost:123456`*, `ftp://localhost`*, ` http://localhost` (leading space)*, `http://localhost\n`*, `http://localhost\r\nX-Evil: 1`, `http://localhost/\r\n X-Evil: 1`*. The cases not marked pass on the old code (probed at lock time against the old check with a `Message` header: only the starred values were echoed; `http://localhost\r\nX-Evil: 1` was not, because `urlparse` joins its lines into a different hostname) and pin that the narrowing did not widen anything.
- C6 (contract; passes on the old code). The recording seam of this file stores `(name, value)` pairs in a list, not a dict, so it can see order. In `send_json` with an allowed `Origin`, `Access-Control-Allow-Origin` comes after `Referrer-Policy` (the last dynamic header); in `do_OPTIONS` it comes before `Access-Control-Allow-Methods`. This pins D3's "same position".
- C3 (contract). `do_OPTIONS` still sends `Access-Control-Allow-Methods` equal to `GET, OPTIONS` and `Access-Control-Allow-Headers` equal to `Content-Type`, with and without an allowed `Origin`; `send_json` still sends `Cache-Control` equal to `no-store`.

Class `CorsOriginServerTests` (real server on port 0, raw socket, `Host: 127.0.0.1:<port>`, `Connection: close`).

- C4 (abuse; fails on the old code). Two test methods, one per request: `GET /no-such-endpoint` (falls through to `send_json` with a 404) and `OPTIONS /`, each with the folded request header `Origin: http://localhost/` plus the continuation line ` X-Evil: 1`. Read the whole response by calling `recv` in a loop until it returns `b""` (a single `recv(65536)` can return a short read; do not copy that helper). Assert `b"x-evil"` is not in the lower-cased response bytes and `b"access-control-allow-origin"` is not in them either.
- C5 (contract). The same two requests with `Origin: http://localhost:<port>` yield a response whose headers contain exactly one `Access-Control-Allow-Origin: http://localhost:<port>` line.

## 5. Documentation

`docs/architecture.md`: the one sentence in section 3. No other document changes; the backlog row is the architect's.

## 6. Verification procedure

Run every pytest invocation under the shared lock: `flock -w 3600 /home/zbalint/.cache/saltmdb-suite.lock systemd-run --user --scope -p MemoryMax=1500M <command>`. The full gate is `./verify` run the same way. One suite run at a time.

## 7. Out of scope

- Removing the CORS echo, adding `[::1]`, changing `Access-Control-Allow-Headers` or `Allow-Methods`, adding a CORS allow list setting.
- Any change to `send_html`, `send_static_asset`, `send_error` bodies, Host validation, the CSP or the cache headers.
- The Scorecard alerts (#7, #9, #10, #11) and the Sonar S5131 disposition.
- Closing the CodeQL alerts by changing the code for the scanner (section 8, item 7).
- A Hypothesis property test of the echo (for any generated `Origin` string, the header is either absent or exactly the input and the input is a canonical loopback origin; no control character is ever emitted). It belongs to the follow-up fuzzing spec, which adds the dependency and the conventions; this spec uses example-based tests only.

## 8. Acceptance

Trust-boundary note: the viewer is a loopback HTTP server fed by any local process and any web page in the owner's browser. Attack path P1, a control character in `Origin` reaching a response header, is removed (only a fully matched canonical origin is echoed, so no CR, LF or space can be present). Abuse tests: C2 (the CR and LF cases) and C4, which fail on the old code. Attack path P2, an arbitrary web page on another local origin reading `/api/*` through the retained loopback echo, is unchanged and out of scope. Path P3, DNS rebinding, is unchanged (Host validation).

1. Failing first: after writing only the new test file, `<lock> .venv/bin/python -m pytest -q tests/test_viewer_cors_origin.py` fails. Expected: the two C2 test methods fail with 12 failing subtests each (the 12 starred values, counted by probing the old check at lock time), and the two C4 test methods fail; C1, C3, C5, C6 and the unstarred C2 values pass (they are contract tests on the old code). Report the observed counts; a different count is a finding to explain, not a reason to adjust a test.
2. After the change: the same command passes with 0 failures; report the counts.
3. `<lock> ./verify` exits 0.
4. `rg -n 'Access-Control-Allow-Origin' src` shows exactly one line, the `send_header` call inside `_send_cors_origin_header` (keep the header name out of the docstring and comments); `rg -n 'headers.get\("Origin"' src` shows exactly one line, in the same method; `rg -n 'parsed_origin' src` shows nothing.
5. Mutation check, on a scratch copy (`git archive` into the scratchpad, imports pointed at the copy), never in the checkout: for each mutant, name the test that fails, then restore. M1 `fullmatch` replaced by `match` (killed by C2 `http://localhost/path`). M2 `{1,5}` replaced by `+` (killed by C2 `http://localhost:123456`). M3 the `https?` replaced by `\w+` (killed by C2 `ftp://localhost`). M4 the call removed from `send_json` (killed by C1 for `send_json`). M5 the call removed from `do_OPTIONS` (killed by C1 for `do_OPTIONS`). M6 the echoed value replaced by the constant `*` (killed by C1). M7 `localhost` alternative removed (killed by C1). M8 `[0-9]` replaced by `\d` (killed by C2 `http://localhost:٨٧`). M9 the dots of `127\.0\.0\.1` unescaped (killed by C2 `http://127a0a0a1`). M10 the call moved to after the `Allow-Methods` header in `do_OPTIONS` (killed by C6). M11 `getattr(self, "headers", None)` replaced by `self.headers` (killed by the C2 no-`headers`-attribute case, for both methods). M12 `re.IGNORECASE` added (killed by C2 `HTTP://localhost`). The checkout must match the developer's own diff afterwards.
6. `git status --short` and `git diff --stat` list only the three files in section 0 scope; `git diff` shows no unrelated edit.
7. Post-merge, architect only (not a developer gate): after `master` is scanned, read `gh api 'repos/zbalint/SALTMDB/code-scanning/alerts?state=open'`. The goal of this spec is a correct, narrow echo, not a clean scanner report. If CodeQL still reports the echo because it does not treat the full match as a barrier, the code does not change to please it: the owner dismisses the alert with the reason written in the dismissal (a full match makes control characters impossible; the abuse tests C2 and C4 pin it).

## 9. Pre-lock gate notes

Claims about existing code:

| Claim | Where | Verified how |
|---|---|---|
| Both blocks are identical and only these two read `Origin` | `base.py` lines 104 to 109 and 150 to 155; `rg -n -e 'Access-Control-Allow-Origin' -e '"Origin"' src tests hooks scripts` finds only these four lines | read and grep |
| No test pins CORS today | `rg -nil -e 'allow-origin' -e '\bOrigin\b' tests` finds only `tests/test_prepush_sonar_script.py`, an unrelated use of the word | grep |
| `urlparse` drops CR and LF before taking the hostname, so a folded value passes the old check | Python 3.11.15 in `.venv` | probed: `urlparse('http://localhost/\r\n X-Evil: 1').hostname` is `localhost` |
| The folded request header survives parsing intact | `http.client.parse_headers` | probed: `headers.get("Origin")` returned `'http://localhost/\r\n X-Evil: 1'` |
| `send_header` writes the value unchanged | `http.server` in 3.11.15 | probed: buffer held `b'Access-Control-Allow-Origin: http://localhost/\r\n X-Evil: 1\r\n'` |
| The candidate regex matches the five canonical origins and rejects the 12 other probed values | `.venv` Python | probed with `fullmatch` |
| `[::1]` was never echoed | old check compares `parsed.hostname` to `("localhost", "127.0.0.1")`; `urlparse('http://[::1]:80').hostname` is `::1` | read and probed |
| `urllib.parse` is still used elsewhere in `base.py` | `do_GET` calls `urllib.parse.urlparse` | read |
| The unit fixtures construct the handler with no `headers` attribute for `send_json` | `tests/test_viewer_response_headers.py` lines 16 to 25 | read; `getattr(self, "headers", None)` keeps that path working |
| The tool-tester design assumes the narrow CORS allow list | `docs/specs/SPEC-VIEWER-TOOL-TESTER.md` lines 107 to 112 | read; this spec keeps the allow list as is |
| A `send_json` response is reachable without a database | `do_GET` falls through to `send_json({"error": "Endpoint not found"}, 404)` for an unknown path | read (`base.py` line 173; `do_HEAD` at line 269) |

Scope list versus acceptance: the section 8 greps touch only `src`; the scope files are the same three as `git diff --stat`. Data shape stated twice: the regex appears in D1 and in nothing else; the C2 and C4 values are in section 4 only. Re-grep of the spec after the consultant's review changes: no remaining `five inline`, `line 164`, `\|` in an `rg` pattern or `DRAFT`; the C2 count (12 starred, 8 unstarred values) appears in sections 4 and 8 only. Consultant review folded in: rg patterns, expected failure counts, `\d` and unescaped-dot mutants, header-order test C6, no-`headers`-attribute case, loop-until-EOF read in C4, anchors, and the single-line grep wording. The CodeQL outlook is recalled by the consultant, not verified; section 8 item 7 handles it without changing the code for the scanner.

## Amendment 1 (2026-10-25, review round 1, tests only)

An independent tester pass and the architect's own mutants found two gaps in the new tests; production code and the accepted behaviour did not change.

- Survivor: a lookup of the LAST `Origin` header (`get_all("Origin")[-1]` in place of `get("Origin", "")`) passed every test. Added C7 (real server, GET and OPTIONS): two `Origin` lines, canonical first and foreign second, echo exactly the first value once; foreign first and canonical second echo nothing. D2's "first `Origin` header" is now pinned.
- Survivor: `https?:/+` in place of `https?://` passed every test. Added `http:/localhost` and `http:///localhost` to the C2 values (absent header expected; both already passed on the old code).
- Mutants M13 (the last-header lookup) and M14 (the slash-count regex) are killed by C7 and by the two new C2 values.
- Dispositioned out of contract, no test: a mutant that swallows `TypeError` or `AttributeError` around the regex. It differs only when `headers.get` returns a non-string object, which HTTP parsing cannot produce, and pinning it would over-specify the helper. Equivalent mutants (alternation order, group capture, `$` with `fullmatch`, `{}` default for `getattr`, subscript lookup, `.lower()` on the echoed value) need no test.
- Review record: developer RED on the old code 26 failed, 7 passed, 30 subtests; GREEN 10 passed, 62 subtests; tester differential 36 recording cases and 23 raw-server cases per response method, every difference an intended narrowing, no status change; architect's fresh `./verify` on the final tree exited 0 (3744 passed, 10 skipped, 479 subtests).
- Sonar `python:S6353` (minor) on the `[0-9]` in `_LOOPBACK_ORIGIN` asks for `\d`. Not applied: in a `str` pattern `\d` also matches non-ASCII digits (the Arabic-Indic port case in C2 pins this), so `[0-9]` is the correct, explicit ASCII-only class. The owner dispositions the finding in the Sonar UI; no suppression comment is added.

## Amendment 2 (2026-10-25, CodeQL alert 12 on master, owner chose to rework the code)

After the fix reached `master`, CodeQL closed alerts 5 and 6 and opened alert 12 (`py/http-response-splitting`) on `base.py` line 105, the `self.send_header("Access-Control-Allow-Origin", origin)` call. The regex guard makes a line break impossible, but the header value is still the request string itself and CodeQL does not treat a compiled-pattern `fullmatch` as a sanitiser. Decision (owner, 2026-10-25): do not suppress; build the header value from constants so no request-derived string reaches `send_header`. This also fixes a latent inexactness: the old pattern accepted ports with leading zeros (`:08080`) and above 65535 (`:99999`), which a browser never sends and which would be echoed back unchanged.

Scope (may edit): `src/saltmdb/viewer/routes/base.py` (`_LOOPBACK_ORIGIN` and `_send_cors_origin_header` only) and `tests/test_viewer_cors_origin.py` (additions only). Does not touch: `send_json`, `do_OPTIONS`, any other file, `docs/architecture.md`.

Change in `base.py`:
- `_LOOPBACK_ORIGIN = re.compile(r"(https?)://(localhost|127\.0\.0\.1)(?::([1-9][0-9]{0,4}))?")` (three groups: scheme, host, optional port without a leading zero; `[0-9]` stays, see Amendment 1).
- `_send_cors_origin_header` matches the request value with `fullmatch`; on no match, or a port above 65535, it sends nothing. Otherwise it builds the value from constants: `scheme = "https" if match[1] == "https" else "http"`, `host = "127.0.0.1" if match[2] == "127.0.0.1" else "localhost"`, and the port as `int(match[3])` formatted in the f-string only when the group is present. The request string itself is never passed to `send_header`. Use exactly this form (a choice between literals, and `int()` for the port); do not look the value up in a dict keyed by the request string and do not `.lower()`/`.strip()` and pass it on.

Tests (additions to `tests/test_viewer_cors_origin.py`, no existing test changes): extend `allowed_origins` with `http://localhost:1`, `https://127.0.0.1:65535` (already present), `http://127.0.0.1` and `https://localhost:80`; extend `rejected_origins` with `http://localhost:0`, `http://localhost:08080`, `http://localhost:00`, `http://localhost:65536`, `http://localhost:99999`, `http://127.0.0.1:010`. The existing byte-for-byte echo assertions already prove the constructed value equals the canonical input. Contract tests (pass on the old code): the whole allowed list. Fail on the old code: the five new rejected values.

Acceptance:
1. `<lock> .venv/bin/python -m pytest -q tests/test_viewer_cors_origin.py tests/test_viewer_host_validation.py tests/test_viewer_response_headers.py tests/test_viewer_server.py tests/test_viewer_routes.py` passes; red phase first on the old code for the new rejected values.
2. `<lock> ./verify` exits 0.
3. Mutants on a scratch copy, each killed by a named test: ports compared `<= 65536`; the leading-zero rule removed (`[0-9]{1,5}`); `"https" if` condition inverted; host choice always `"localhost"`; port dropped from the echo; the old raw echo restored (this one is behaviourally equivalent for canonical input and is checked by reading the diff, not by a test).
4. After push to `develop`, the architect opens a draft pull request `develop` to `master` only to obtain a CodeQL analysis of the new code, reads the alerts of that analysis, and closes the pull request without merging. If a `py/http-response-splitting` alert remains on `base.py`, the finding is reported to the owner for a false-positive disposition; no suppression comment is added in code.

Amendment 2 review record (2026-10-25): developer RED on the old code 12 subtests failed (the six new rejected values on each response method), GREEN 84 passed, 112 subtests over the five viewer test files; independent tester differential over 100016 generated Origin strings found 34469 differences, all intended (34466 ports above 65535, two port-zero, one leading-zero), canonical values byte-identical, a tagged-`str` probe showed the header value is a new string; ten mutants killed, one equivalent survivor (writing the port text instead of the integer, since leading zeros are rejected by the pattern). The amendment prose says five new rejected values; the list names six, and all six are tested. Architect `./verify` on the final tree: 3763 passed, 10 skipped, 499 subtests.
