# SPEC-TAINT-VIEWER-STATIC

## 0. Status

LOCKED 2026-10-20

- Backlog item: none. Sonar rule `pythonsecurity:S5131` (BLOCKER, reflected XSS), one open issue at `src/saltmdb/viewer/routes/base.py` line 98 (`self.wfile.write(candidate.read_bytes())` in `send_static_asset`) on `develop` `3fd3c98`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decision (2026-10-20): taint findings get real hardening, no suppression, no status change; a finding that stays flagged after a correct fix is reported to the owner.
- Baseline: `develop` at `ac8e629` or later; `./verify` exits 0 (3015 passed or more, 1 skipped, 233 subtests; `tests/test_adapter_signal_shutdown.py` and `tests/test_daemon_server_main.py` are known load- and port-sensitive: rerun a failing one alone once before calling it real).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `SALTMDBHandler.send_static_asset`, and `do_GET` and `do_HEAD` (the routing level, where the query string and path handling live), driven with the handler fixture of `tests/test_viewer_response_headers.py` (`_handler_recording_headers`, lines 16-24: a `SALTMDBHandler` with recorded `send_header`, `send_response`, `end_headers` and `wfile` as `io.BytesIO`; copy it into the new file, do not import it).
- Scope (may edit): `src/saltmdb/viewer/routes/_shared.py` (the `STATIC_ASSETS` table), `src/saltmdb/viewer/routes/base.py` (`send_static_asset` and the unused `mimetypes` import) and the NEW file `tests/test_viewer_static_assets.py`.
- Does not touch: any existing test, the static files themselves, the route that calls `send_static_asset` (`base.py` 141 and 220), `send_json`, `send_html`, the CSP.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The consultant's survey (m_2654) found no exploitable XSS: the request path only selects a key in the fixed `STATIC_ASSETS` allowlist (four files), unknown paths get a 404, and no request data reaches the body. Two real gaps remain on the static path and are the hardening this spec delivers: (a) `Content-Type` comes from `mimetypes.guess_type`, whose answer depends on the platform's MIME database (for example the Windows registry can map `.js` to `text/plain`, and Python 3.10/3.11 and 3.12 differ for `.js`); (b) static responses carry no `X-Content-Type-Options: nosniff`, unlike every dynamic response. Sonar's rule text for S5131 is fixed by "correctly setting the Content-Type HTTP header ... to a type that is not HTML", so an explicit constant Content-Type is the compliant shape. Whether the engine then stops flagging file content written to `wfile` is not proven; if the issue stays open after the push the architect reports it to the owner (no suppression).

## 2. Decisions

- D1. `STATIC_ASSETS` (`_shared.py` 16-21) maps each path to a pair `(relative_path, content_type)` instead of a bare relative path: `viewer.css` -> `"text/css; charset=utf-8"`; `viewer.js`, `vendor/marked-18.0.7.umd.js` and `vendor/dompurify-3.4.16.min.js` -> `"text/javascript; charset=utf-8"`. Same four keys, same relative paths. Existing users of the table use membership only (`tests/test_viewer_vendor_assets.py` 33, `tests/test_viewer_rework_contracts.py` 92, the re-export in `routes/__init__.py`), so no existing test changes.
- D2. `send_static_asset`: look up the pair; an unknown path still returns the same 404 JSON; send `Content-Type` from the pair (a constant, no `mimetypes`), keep `Cache-Control: public, max-age=86400`, and add `X-Content-Type-Options: nosniff`. The body logic (`HEAD` writes nothing) is unchanged. Remove the now-unused `import mimetypes` (`base.py` 13).
- D3. The new test file asserts, with literal expected values. At the `send_static_asset` level, for each of the four assets: status 200, the exact `Content-Type`, `X-Content-Type-Options` equal to `nosniff`, the existing `Cache-Control: public, max-age=86400`, and a body equal to the file's bytes (read from `src/saltmdb/viewer/static/...`); for `HEAD` on a known asset an empty body with the same headers. At the routing level (`handler.path` set to the raw request target, then `do_GET` or `do_HEAD`, status recorded by replacing `send_response`): `/static/viewer.js?v=1` and `/static/viewer.css?v=2` succeed (the shell requests assets with a `?v=` cache-buster, `templates.py` 29 and 48); `/static/%2e%2e/x`, `/static/../../etc/passwd`, `/static/viewer.js/` and an unknown `/static/nope.js` return 404 for GET and read no file (patch `pathlib.Path.read_bytes` with a mock and assert it was not called); `//static/viewer.js` does not reach `send_static_asset` (it parses with netloc `static` and falls through to the final 404). Do not assert the body of a HEAD 404 (today `send_json` writes a JSON body even for HEAD; that route behaviour is pre-existing and out of scope). A table test asserts that every `STATIC_ASSETS` value is a pair whose relative path exists under the static directory and whose content type is exactly one of the two allowed strings (this pins the table's shape against a future asset added without its type).
- D4. No new Sonar finding and no suppression. `ruff check --select ARG,F,RET` on the two source files shows nothing new.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/viewer/routes/_shared.py` | `STATIC_ASSETS` values become `(relative_path, content_type)` |
| `src/saltmdb/viewer/routes/base.py` | `send_static_asset` uses the pair, adds `nosniff`; drop `mimetypes` import |
| `tests/test_viewer_static_assets.py` | new (D3) |

## 4. Tests

The new file only. It fails on the unmodified source on every Python version: the code today never sends `nosniff` and never sends `; charset=utf-8` (the bare type comes from `mimetypes.guess_type`, which depends on the host's MIME database; on this venv's 3.11.15 it gives `text/javascript` and `text/css`, CPython's built-in default for `.js` was `application/javascript` before 3.12, and a Windows registry can say `text/plain`).

## 5. Documentation

`docs/architecture.md` line 197 says dynamic responses carry `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`; that stays true and is not a quotation of the static path, so nothing changes. Optionally add one clause saying static assets now carry `nosniff` and explicit types. Update only an exact quotation of a Content-Type.

## 6. Verification procedure

`./verify`; the new test file in isolation; `ruff check --select ARG,F,RET` on the two source files against the baseline copy. Manual `curl` evidence WITHOUT a daemon: the viewer runs inside the daemon, and a scratch daemon would load models on a shared 4-core host and can collide with the live daemon's election and probe ports (the port slot comes from a hash of the database path). Instead run the handler class in a scratch script that starts no database and no daemon: `http.server.ThreadingHTTPServer(("127.0.0.1", 0), SALTMDBHandler)` on an ephemeral port, serve a few requests in a thread, `curl -sI` the four `/static/` URLs (with and without `?v=1`) and `/`, then shut it down. The static routes and `/` read only package files (`send_static_asset`, `get_frontend_html`). Every `/api/` call fails in that setup; it is not needed. Optionally open `/` in a browser to confirm the scripts and the stylesheet load under the CSP and `nosniff` (the page itself will show API errors).

## 7. Out of scope

The route that dispatches static paths, the dynamic response headers, other taint findings.

## 8. Acceptance

1. `./verify` exits 0.
2. `git status` lists only the three files of section 3 (the new test untracked).
3. `curl -sI` evidence for the four assets and for the `?v=1` forms from the in-process scratch server of section 6 (no daemon, no database, never the live ones).
4. Mutation sample on a scratch copy (repo tests as killers; the tester names them): change one Content-Type string; drop the `nosniff` header; swap two paths' relative files; make `HEAD` write the body; remove the unknown-path 404. Each fails a test of the new file.
5. Architect, after commit and CI: the Sonar search shows the S5131 issue closed or still open on `develop`; if still open, report to the owner (the explicit Content-Type is the rule's documented fix; the engine may still see file content reaching the response).

## 9. Pre-lock gate notes

Consultant review (m_2665): the only VALUE reader of `STATIC_ASSETS` is `base.py` 87 (membership uses at `tests/test_viewer_vendor_assets.py` 33, `tests/test_viewer_rework_contracts.py` 92, re-export `routes/__init__.py` 21 and 34); routing confirmed (`urlparse` strips the query, `%2e%2e` and `../` miss the dict, `//static/...` parses as a netloc); `nosniff` accepts the two chosen MIME strings (three classic `<script src>` and one stylesheet, no module scripts); existing viewer tests assert only the `Cache-Control` of a static asset. Folded in: the blocker (no scratch daemon; in-process `ThreadingHTTPServer` for the curl step), the routing-level test cases, the HEAD-404 caveat, the corrected failure statement, the table-shape test. Not checked: a real browser, a Windows host, the CPython 3.12 changelog line (recalled).
