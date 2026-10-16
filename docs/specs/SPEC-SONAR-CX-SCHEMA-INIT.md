# SPEC-SONAR-CX-SCHEMA-INIT

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-UTILS-DB is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `saltmdb.db.schema.init_db` called on temporary databases.
- Scope (may edit), source:
  - `src/saltmdb/db/schema.py`
- Scope (may edit), tests: existing schema and migration tests (`rg -l init_db tests`, in particular `tests/test_lifecycle_invariant_migration.py` and `tests/test_predicate_migration.py`).
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 1 functions above the cognitive-complexity limit of 15 in this group. `init_db` is 956 lines and scores 96. It builds the schema and migrates user databases on every daemon start, so a mistake corrupts user data. It is its own spec, done in small steps. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper).
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. Move one migration or schema block per helper, unchanged. Each `if user_version < N` block has SAVEPOINT/ROLLBACK and permissive `OperationalError` handling (around lines 1330 to 1350) that must move intact.
- D8. Characterize first with real temporary SQLite databases through `init_db` for every starting state (fresh, user_version 1, user_version 2, current): record `sqlite_master` rows (type, name, sql) and `PRAGMA user_version` and the contents of the seed tables before and after the refactor, and assert the post-state of the refactored code equals the post-state recorded from the unmodified code (literal expected values written into the tests, not recomputed with the code under test).
- D9. Do the work in steps and run the characterization after each step. If at the end `init_db` itself is still above the limit, keep the best safe result and report.

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/db/schema.py` | `init_db` | 407 | 96 |

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

Other Sonar rules, other files, any change of behavior, performance work, and these items: `_migrate_legacy_lifecycle_invariants` (handled in UTILS-DB).

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling); see the assignment event.
