# SPEC-SONAR-CLI-RETURNS

## 0. Status

LOCKED 2026-10-20

- Backlog item: none (Sonar rule `python:S3516`, severity BLOCKER, four open issues in `src/saltmdb/cli.py` at lines 8, 32, 79 and 493 on `develop` `973052f`). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` at `973052f` or later; `./verify` exits 0 (2940 passed, 1 skipped, 233 subtests at `844970e`). `tests/test_cli.py` passes.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the four command functions called directly, and `main()` through `tests/test_cli.py`.
- Scope (may edit): `src/saltmdb/cli.py` (the bodies of `cmd_bootstrap_digest`, `cmd_session_digest`, `cmd_related_memories`, `cmd_corpus_health` only) and `tests/test_cli.py` (the return-value assertions of those four functions only).
- Does not touch: every other function in `cli.py`, `main()`, the argument parser, `saltmdb_cli.py`, hooks, docs and every other file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Each of the four commands returns the literal `0` on every path, so Sonar reports that the function "always returns the same value". They are best-effort hook entry points that must never fail the caller. `main()` already turns a missing return value into exit status 0 (`sys.exit(args.func(args) or 0)`, `cli.py` line 654), so a command that cannot fail needs no return value. This spec deliberately reverses D11 of the locked SPEC-SONAR-CX-UTILS-DB ("the CLI commands keep returning 0 where they return 0 today"), because that decision left these four findings open; the exit status stays 0. Rejected: returning a computed status for these four (they have no failing return path: `main()`'s handler turns an exception into exit 0 for three of them and exit 1 for `corpus-health`, which this spec does not change), and a Sonar issue-status change (owner rule: never change issue status).

## 2. Decisions

- D1. In the four functions, every early `return 0` becomes a bare `return` and the final `return 0` is deleted (do not leave a trailing bare `return`: the repo's ruff config selects PLR, and PLR1711 rejects it), so the function ends without a value. The `os._exit(0)` call in `cmd_related_memories` (line 117 at `973052f`) stays as it is. Comments on the early returns stay.
- D2. Exit status is unchanged for every command line: `saltmdb-cli bootstrap-digest`, `session-digest`, `related-memories` and `corpus-health` still exit 0 on every path that exited 0 before, through `main()`'s `or 0`. The exception handler in `main()` is not touched.
- D3. In `tests/test_cli.py`, every `self.assertEqual(rc, 0)` whose `rc` comes from `cmd_bootstrap_digest`, `cmd_session_digest` or `cmd_corpus_health`, directly or through the helper `_run()` (`tests/test_cli.py` lines 311-315, which returns `rc, report`), becomes `self.assertIsNone(rc)`: 21 sites at `973052f`: lines 41, 51, 64, 73 (bootstrap), 88, 98, 113, 122, 133, 153 (session) and 322, 327, 382, 400, 419, 448, 472, 497, 560, 626, 655 (corpus health). Lines 271 and 286 test `cmd_orphans` and stay; assertions on any other command's return value stay. Add three new tests, in the style of `TestSnapshotCli._main` (`tests/test_cli.py` lines 666-673), that call the real `main()` with `sys.argv` set to `['saltmdb-cli', '--db-path', <nonexistent path>, '<command>']` (the option comes BEFORE the subcommand; after it the parser exits 2) for `bootstrap-digest`, `session-digest` and `corpus-health`, with `saltmdb.daemon.client.call` patched and asserted not called, and expect `SystemExit` with code 0 and empty stdout (do not assert empty stderr for `corpus-health`, which prints a notice there). `related-memories` already has this test: `tests/test_cli_related_memories.py` `test_missing_db_is_silent` (lines 98-106); do not add another and do not edit that file.
- D4. No new Sonar finding: no unused locals or parameters, no new suppressions, no change of the other functions. Check `ruff check --select ARG,RET` on both files against the baseline (only new hits count). Sonar rule `python:S3801` (consistent returns) must not appear: every return in the four functions is bare.
- D5. Report to the owner that D3 edits assertions of existing tests: the assertions change because the return contract changes from `0` to no value, with the process exit status identical.

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/cli.py` | `cmd_bootstrap_digest`: bare `return` at the three exits (lines 14, 26, and the final line 29 is deleted). `cmd_session_digest`: lines 40, 69 and final 76. `cmd_related_memories`: lines 85, 121, 127 and final 133. `cmd_corpus_health`: lines 507 and final 512. Line numbers are at `973052f` and shift as lines are removed. |
| `tests/test_cli.py` | The 21 return assertions of D3 and the three `main()` exit-status tests. |

## 4. Tests

The 21 updated assertions plus the three `main()` exit-status tests of D3. The new tests pass on the unmodified source too (the exit status is the same), so they are contract tests; the 21 assertions fail on the unmodified source until the return values change. Expected values are literals (`None`, exit code 0).

## 5. Documentation

None.

## 6. Verification procedure

`./verify`; then a manual check of exit status through the real entry point on a missing database, which every command short-circuits: `HOME=<empty temp dir> .venv/bin/python -c "import sys; sys.argv=['saltmdb-cli','--db-path','/nonexistent/x.db','bootstrap-digest']; from saltmdb.cli import main; main()"; echo $?` prints 0 for `bootstrap-digest`, `session-digest`, `related-memories` (stdin closed) and `corpus-health`. Never point these at the live database.

## 7. Out of scope

Other Sonar findings in `cli.py`; changing `main()`; changing which commands exit nonzero; the S3516 style in other files.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff -U0 src/saltmdb/cli.py` shows only `return 0` to `return` replacements and deleted final `return 0` lines, inside the four functions.
3. `.venv/bin/ruff check --select ARG,RET src/saltmdb/cli.py tests/test_cli.py` reports no hit that the baseline copy of the same files does not report.
4. `git diff --stat` lists only `src/saltmdb/cli.py` and `tests/test_cli.py`.
5. Mutation sample on a scratch copy (never the assigned checkout): turn one bare `return` into `return 1` in each of the four functions, each making a named repo test fail while the same test passes on the unmutated tree (for `related-memories`, `tests/test_cli_related_memories.py` is an allowed killing test, for example `test_missing_db_is_silent` and the `--json` test).
6. Architect, after commit and CI: the Sonar search shows the four S3516 issues no longer open on `develop`, with no new issue in these two files.

## 9. Pre-lock gate notes

Claims and how each was verified (read or probed) at `973052f`:

| Claim | Where | Verified |
|---|---|---|
| `main()` uses `args.func(args) or 0` | `cli.py` 654 | read; consultant read |
| The four functions return only literal `0`: bootstrap 14, 26, 29; session 40, 69, 76; related 85, 121, 127, 133; corpus-health 507, 512 | `cli.py` | AST scan by architect and consultant |
| `os._exit(0)` in `cmd_related_memories` is at 117 and is not a return | `cli.py` | read by consultant |
| 21 `assertEqual(rc, 0)` sites on these functions (4 bootstrap, 6 session, 11 corpus health, five of them through `_run()`); 271 and 286 are `cmd_orphans` | `tests/test_cli.py` | AST scan by consultant (the architect's first scan missed the `_run()` sites) |
| No test calls `cmd_related_memories` directly; `test_missing_db_is_silent` covers its missing-database exit 0 through `main()` | `tests/test_cli_related_memories.py` 98-106 | read by consultant |
| No other caller of the four functions; hooks use the CLI as a subprocess and read only exit status or stdout | grep of src, tests, hooks, scripts | consultant |
| A missing database short-circuits before any daemon call in all four | `cli.py` 12-14, 38-40, 83-85, 504-507 | read by consultant |
| `--db-path` is a top-level option and must precede the subcommand | `cli.py` 559; `tests/test_cli.py` 678 | consultant probe (wrong order exits 2) |
| Bare returns do not trigger S3516 or S3801 | Sonar rules; 18 functions in `src/saltmdb` with several bare returns have no open S3516 | consultant; confirmed after commit by acceptance 6 |
| Ruff baseline on the two files has one ARG001 hit (`cli.py` 546, out of scope), so "no new hit" can pass | ruff | consultant |
| Conflicting locked decision: SPEC-SONAR-CX-UTILS-DB D11 | `docs/specs/SPEC-SONAR-CX-UTILS-DB.md` 38 | read by consultant; reversed in section 1 |

Reviewed by a consultant before lock (one pass; its three blockers, the line and wording corrections and the existing related-memories test are folded in). Gate step 7: the number of assertions (21) is stated in D3, section 3 and section 4 identically.
