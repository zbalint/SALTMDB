# SPEC-SONAR-CX-UTILS-DB

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-SERVER-MAIN is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `saltmdb.db.schema._migrate_legacy_lifecycle_invariants` (via `init_db`), `saltmdb.utils.nlp.*`, `saltmdb.utils.text.*`, `saltmdb.db.viewer_sessions._pid_alive`, `saltmdb.cli` commands, `saltmdb.__main__.main`.
- Scope (may edit), source:
  - `src/saltmdb/__main__.py`
  - `src/saltmdb/cli.py`
  - `src/saltmdb/db/schema.py`
  - `src/saltmdb/db/viewer_sessions.py`
  - `src/saltmdb/utils/nlp.py`
  - `src/saltmdb/utils/text.py`
- Scope (may edit), tests: existing tests for each module (find them with `rg -l <function name> tests`); new tests go beside them.
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 9 functions above the cognitive-complexity limit of 15 in this group. These are shared utilities, the CLI entry points and the schema migration code. `init_db` (score 96) is NOT in this spec; it has its own. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper).
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. NLP functions (`auto_format_markdown`, `validate_markdown_structure`, `evaluate_memory_quality`) keep returning identical dicts for identical input; characterize with literal inputs and literal expected dicts (`evaluate_memory_quality` is 163 lines and runs on every store).
- D8. `_pid_alive` (`db/viewer_sessions.py`): `tests/test_viewer_sessions.py` lines 75 to 100 patch `saltmdb.db.viewer_sessions.kernel32` and `.wintypes` and `ctypes.byref`/`ctypes.get_last_error`. Helpers must keep looking these up as module globals and `ctypes.<name>` attributes (no `from ctypes import byref`). The name `_pid_alive` stays (patched by `tests/test_copilot_session_file.py`).
- D9. No test calls `cli._corpus_correction_data` or `saltmdb.__main__.main` directly: for `_corpus_correction_data` write the characterization through `_corpus_health_data(conn, days, telemetry_limit)` or `cmd_corpus_health` on a temp database; for `__main__.main` import the module and call `main()` with patched `sys.argv` and the dispatch targets patched, or, if that is not feasible, refactor by pure line move and say so in the report.
- D10. `_migrate_legacy_lifecycle_invariants` is reached through `init_db` after resetting `PRAGMA user_version` to 2 (see `tests/test_lifecycle_invariant_migration.py` lines 43 and 124). The four starting states the existing tests build are user_version 1, user_version 2, current and a fresh empty database.
- D11. The CLI commands keep returning 0 where they return 0 today (deliberate; not a finding).

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/__main__.py` | `main` | 13 | 27 |
| `src/saltmdb/cli.py` | `_corpus_correction_data` | 233 | 27 |
| `src/saltmdb/db/schema.py` | `_migrate_legacy_lifecycle_invariants` | 18 | 24 |
| `src/saltmdb/db/viewer_sessions.py` | `_pid_alive` | 25 | 17 |
| `src/saltmdb/utils/nlp.py` | `auto_format_markdown` | 312 | 26 |
| `src/saltmdb/utils/nlp.py` | `validate_markdown_structure` | 380 | 21 |
| `src/saltmdb/utils/nlp.py` | `evaluate_memory_quality` | 509 | 50 |
| `src/saltmdb/utils/text.py` | `resolve_entity_ref` | 103 | 16 |
| `src/saltmdb/utils/text.py` | `extract_title_and_snippet` | 151 | 18 |

## 4. Tests

Per D2. New tests go in the existing test file for the module, or, when none exists, a new `test_<module>_characterization.py` next to the other tests of that area. Tests must not touch the real `~/.saltmdb` data or the working daemon: use temp directories and the existing fixtures/helpers.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus the complexity proxy. The proxy lives outside the repo and must never be added to `pyproject.toml` or `uv.lock`:

```sh
SCRATCH=<a scratch directory you create under /tmp>; echo "$SCRATCH"
uv venv -q "$SCRATCH/cxvenv" && uv pip install -q --python "$SCRATCH/cxvenv/bin/python" complexipy
"$SCRATCH/cxvenv/bin/complexipy" --max-complexity-allowed 12 <files in scope>
```

It must report no function above 12 in the files in scope (functions outside section 3 that already exceeded 12 before your change are listed in the report as pre-existing; do not touch them). Run it once before editing to record the baseline. Delete only under the scratch directory afterwards: `rm -rf "${SCRATCH:?}/cxvenv"` after printing the resolved path.

## 7. Out of scope

Other Sonar rules, other files, any change of behavior, performance work, and these items: `init_db`, the S3516 findings in `cli.py`, regex findings in `nlp.py`.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling); see the assignment event.

## Amendment 1 (2026-10-17)

Records the outcome of review; no contract changes. The three-line comment that justified `main()`'s `# noqa: C901, PLR0915` in `__main__.py` is dropped together with that suppression, which `main()` no longer needs (D5). Residual test gaps found by mutation and left open: the 365-day cap on the corrections window in `_correction_buckets` is not pinned by any test (also true before the refactor). Functions above the proxy limit and left unchanged under D6: `cmd_session_digest` (13), `cmd_related_memories` (17), `_migrate_predicate_drift` (14), `init_db` (assigned to SPEC-SONAR-CX-SCHEMA-INIT).
