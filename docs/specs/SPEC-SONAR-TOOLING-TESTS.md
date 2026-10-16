# SPEC-SONAR-TOOLING-TESTS

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-PY-BEHAVIOR is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `scripts/diagnostics/recovered_audit_calls.RECOVERED_CALLS` (data), and the hook tests in `hooks/tests/test_stop_related_memories.py`.
- Scope (may edit): `scripts/diagnostics/recovered_audit_calls.py`, `hooks/tests/test_stop_related_memories.py`.
- Does not touch: everything else, including `scripts/diagnostics/live_audit_repro.py`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 28 `S7498` findings (a `dict(...)` constructor call where a literal works) in the transcribed audit-call data, and 3 `S6389` findings (a bidirectional control character written directly in test source). Both are fixed without changing any value: the first by writing dict literals, the second by writing the character as an escape. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. S7498: replace each `dict(key=value, ...)` call that appears as a row in section 3 with the literal `{"key": value, ...}`. Same keys, same values, same order. Keywords are always identifiers and duplicates are a SyntaxError, so no key changes and no duplicate can be hidden. The two unflagged `dict(**_cfg, ...)` calls (around lines 254 and 293) stay unchanged.
- D2. S6389: in the three flagged test lines, replace the raw bidi character with its escape `\u202e` (U+202E RIGHT-TO-LEFT OVERRIDE; the consultant checked that all three lines are ordinary non-raw string literals, so the runtime value is identical). Line 397 also contains a raw U+200B ZERO WIDTH SPACE: leave it as it is (not flagged). The string's runtime value must be identical.
- D3. The two `S5899` findings in this test file (`HookCase.calls` and `HookCase.log_text`) are NOT fixed. They are shared helpers of the base class that every test class uses; renaming them to `test*` would make unittest run them as tests. They go to the owner as a report.

## 3. Changes per file

| Rule | File | Lines |
|---|---|---|
| `S6389` | `hooks/tests/test_stop_related_memories.py` | 397 |
| `S6389` | `hooks/tests/test_stop_related_memories.py` | 409 |
| `S6389` | `hooks/tests/test_stop_related_memories.py` | 418 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 49 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 59 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 70 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 81 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 91 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 106 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 117 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 128 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 139 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 150 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 161 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 174 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 244 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 245 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 246 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 247 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 283 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 284 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 285 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 286 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 303 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 316 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 329 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 342 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 353 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 364 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 375 |
| `S7498` | `scripts/diagnostics/recovered_audit_calls.py` | 386 |

## 4. Tests

No new tests. Equivalence evidence (acceptance 2 and 3) replaces them.

## 5. Documentation

None.

## 6. Verification procedure

`./verify` only.

## 7. Out of scope

`S5899`, every other rule, the S3776 finding in `live_audit_repro.py` (handled in a later refactor spec).

## 8. Acceptance

1. `./verify` exits 0.
2. Before editing, from the repo root, save `.venv/bin/python -I -c "import sys; sys.path.insert(0,'scripts/diagnostics'); import recovered_audit_calls as r; print(repr(r.RECOVERED_CALLS))" > <scratch>/before.txt`; after editing, produce `after.txt` the same way; `cmp before.txt after.txt` shows no difference. (Adjust the import line if the module needs other paths; record the exact working form in the report.)
3. For D2: before and after, print `repr` of the three affected string literals through a small script and show they are equal; or show the three tests pass unchanged and `git diff` shows only the escape change on those lines.
4. `git diff --stat` lists only the two files from section 0.
5. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read.

## 9. Pre-lock gate notes

Acceptance 2 command probed on the unedited module at b0ad081 (prints a 26718-character repr). The raw-bidi code point per flagged line is checked by the developer before editing (D2). Scope list = the two files in the table.
