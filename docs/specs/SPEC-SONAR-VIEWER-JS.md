# SPEC-SONAR-VIEWER-JS

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-TOOLING-TESTS is committed; `./verify` exits 0. `node` is on PATH (v24).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the existing Python contract tests that read the viewer front end (`tests/test_viewer_frontend_contracts.py`, `tests/test_viewer_rework_contracts.py`, `tests/test_viewer_traces.py`, `tests/test_viewer_quality.py`, `tests/test_viewer_vendor_assets.py`) and `node --check`.
- Scope (may edit): `src/saltmdb/viewer/static/viewer.js`, `src/saltmdb/mcp/prototype_retrieve_context.html` (inline script, lines 197 to 614 only), and `tests/test_viewer_rework_contracts.py` (only the two asserted strings named in D8).
- Does not touch: other Python files, other tests, vendored files under `static/vendor/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 36 low-risk JavaScript findings. Most are the same pattern: a call to the async `render()` (which wraps its own work in try/catch) is not awaited, which Sonar flags as an unhandled promise. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Runtime behavior is unchanged. No suppression comments (`// NOSONAR`).
- D2. S9383 (promise not handled): `render` is `const render = async () => { ... try { await loaders[...]() ... } catch (err) { ... } }` at `viewer.js:870` and handles its own errors. For each flagged site, first confirm the called function is one that cannot reject (read its body); then write `void <call>;`. `document.querySelector('#refresh').addEventListener('click', render)` passes a function reference and is not a floating promise; leave it. If a flagged call is to a function that can reject, do not add `void`; report it.
- D3. S2681 (statement after a braceless `if` on the same line): put the guarded statement in braces and the following statements on their own lines. Semantics stay exactly as they run today (the later statements stay unconditional).
- D4. S7764 (`window` vs `globalThis`) and S6653 (`Object.hasOwn`): apply as Sonar describes. Both are available in every browser the viewer's bundled code already requires (`Object.hasOwn` since Chrome 93, Firefox 92, Safari 15.4); check the file for any existing support statement and report if it names older browsers.
- D5. S3358 and S4624 (nested ternary, nested template literal) and S1121 (assignment in expression): extract into plain statements with the same evaluation order.
- D6. S2486 (`viewer.js:76`): `catch (_)` currently calls `setNotice(...)`; read the actual flagged catch block and add a one-line comment saying why the exception is ignored, or handle it, only if that does not change behavior.
- D8. Two existing contract tests assert the exact text of lines that D2 changes: `tests/test_viewer_rework_contracts.py` line 335 asserts `state.explorerPreset = {}; state.explorerPage = 1; render();` (viewer.js line 356) and line 373 asserts `state.sessionsPreset = {}; state.sessionsPage = 1; state.sessionDetailId = ''; state.view = 'sessions'; render();` (viewer.js line 308). Update exactly those two expected strings to the `void render();` form. The consultant checked the other viewer contract tests (`test_viewer_frontend_contracts.py`, `test_viewer_traces.py`, `test_viewer_quality.py`) and found they keep matching.
- D9. Consultant check of all 22 S9383 sites at `5a4e7ac`: every flagged call is `render()` (viewer.js lines 231, 297, 301, 308, 315, 356, 401, 412, 438, 461, 496, 550, 677, 733, 885, 888, 906, 908, 909) or `openDetail()` (lines 264, 623, 748). Both wrap their work in try/catch and do not reject. Lines 623 and 748 each contain two `openDetail` calls (click and keydown handlers): add `void` to both. Line 907 (`beforeunload`, `window.`) is not flagged and stays as it is.
- D7. Not fixed, and reported: S7758 at `viewer.js:761` (`charCodeAt` to `codePointAt` changes the hash for non-BMP characters and so the derived result), and the JavaScript S3776 in `prototype_retrieve_context.html` (complexity refactor, later spec).

## 3. Changes per file

| Rule | File | Lines |
|---|---|---|
| `S1121` | `src/saltmdb/mcp/prototype_retrieve_context.html` | 259, 269 |
| `S2486` | `src/saltmdb/viewer/static/viewer.js` | 76 |
| `S2681` | `src/saltmdb/viewer/static/viewer.js` | 150, 503, 637 |
| `S3358` | `src/saltmdb/mcp/prototype_retrieve_context.html` | 532 |
| `S3358` | `src/saltmdb/viewer/static/viewer.js` | 709 |
| `S4624` | `src/saltmdb/viewer/static/viewer.js` | 296 |
| `S6653` | `src/saltmdb/viewer/static/viewer.js` | 847, 849 |
| `S7764` | `src/saltmdb/viewer/static/viewer.js` | 218, 219, 908 |
| `S9383` | `src/saltmdb/viewer/static/viewer.js` | 231, 264, 297, 301, 308, 315, 356, 401, 412, 438, 461, 496, 550, 623, 677, 733, 748, 885, 888, 906, 908, 909 |

## 4. Tests

No new tests; the viewer front end has no JavaScript test runner. The existing Python contract tests read these files; run them after each file.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus `node --check src/saltmdb/viewer/static/viewer.js`. For the HTML prototype, extract its inline `<script>` into a scratch file and run `node --check` on it (record the command in the report).

## 7. Out of scope

Everything in D7, Python findings, all other rules.

## 8. Acceptance

1. `./verify` exits 0.
2. `node --check src/saltmdb/viewer/static/viewer.js` exits 0, and the extracted prototype script (lines 197 to 614 of the HTML, between the `<script>` and `</script>` tags) also passes `node --check`.
3. `git diff --stat` lists only the three files in scope; `git diff -w` shows only the changes described in D2 to D6.
4. The report lists any flagged site that was left and why.
5. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read.

## 9. Pre-lock gate notes

The S9383 call targets, the two contract-test strings, the S4624 trailing-space string (`filter-link ` with an empty status must stay exactly that), and the prototype script line range were checked by the consultant against `5a4e7ac`. Gate step 7: scope lists three files in section 0, section 8 acceptance 3 and the `Does not touch` line.

## Amendment 1 (2026-10-16)

Resolves a contradiction found by the developer in D8: two existing tests pin the exact source text of two `render();` lines, so re-pinning them conflicts with the rule that tests must not be edited to make a check pass, and deleting them is out of scope. D8 is withdrawn and `tests/test_viewer_rework_contracts.py` is removed from scope. The S9383 findings at the two pinned lines (baseline `viewer.js` lines 308 and 356) are not fixed; they stay byte-identical and are reported to the owner. All other S9383 sites in D2 and D9 are fixed with `void`.
