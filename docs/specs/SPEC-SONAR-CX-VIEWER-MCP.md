# SPEC-SONAR-CX-VIEWER-MCP

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-DAEMON is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the viewer HTTP handlers called directly on a handler (see `tests/test_viewer_rework_contracts.py` lines 82 to 139) and through `do_GET`; the MCP tool wrappers through `tests/test_mcp_tools.py` and the surface tests.
- Scope (may edit), source:
  - `src/saltmdb/mcp/tools.py`
  - `src/saltmdb/viewer/routes/base.py`
  - `src/saltmdb/viewer/routes/entities.py`
  - `src/saltmdb/viewer/routes/entity_detail.py`
  - `src/saltmdb/viewer/routes/relations.py`
  - `src/saltmdb/viewer/routes/scatterplot.py`
  - `src/saltmdb/viewer/routes/sessions.py`
- Scope (may edit), tests: existing `tests/test_viewer_*.py` files, `tests/test_mcp_tools.py`, `tests/test_manage_relation_id_resolution.py`, `tests/test_bitemporal_relations.py` and the neighbouring MCP surface tests; new tests go in those files.
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 8 functions above the cognitive-complexity limit of 15 in this group. The viewer routes and the MCP tool wrappers are the user-facing HTTP and tool surfaces. `manage_relation` in `mcp/tools.py` is an MCP tool whose signature and docstring are its public schema (FastMCP generates the schema from them): refactor its body only. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper).
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. HTTP status codes, JSON keys, result ordering and the error texts that reach responses (see `tests/test_viewer_error_disclosure.py`) stay byte-identical. MCP tool signatures and docstrings stay byte-identical.
- D8. The viewer handler mixins subclass `ViewerHandlerProtocol` (`viewer/routes/_protocol.py`, out of scope): extract helpers as methods of the same mixin or as module-level functions, never as methods called across mixins.
- D9. `Sonar S107` on `mcp/tools.py` is NOT part of this spec: those parameters are the tool schema. It goes to the owner as a report. Acceptance 6 below pins `manage_relation`'s signature and docstring.

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/mcp/tools.py` | `call` | 214 | 18 |
| `src/saltmdb/mcp/tools.py` | `manage_relation` | 823 | 38 |
| `src/saltmdb/viewer/routes/base.py` | `do_GET` | 113 | 30 |
| `src/saltmdb/viewer/routes/entities.py` | `get_entities` | 25 | 45 |
| `src/saltmdb/viewer/routes/entity_detail.py` | `get_lineage` | 24 | 19 |
| `src/saltmdb/viewer/routes/relations.py` | `get_relations_neighborhood` | 190 | 43 |
| `src/saltmdb/viewer/routes/scatterplot.py` | `get_scatterplot` | 74 | 18 |
| `src/saltmdb/viewer/routes/sessions.py` | `get_sessions` | 220 | 25 |

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

Other Sonar rules, other files, any change of behavior, performance work, and these items: S107 findings, the JavaScript files.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. `git diff -U0 src/saltmdb/mcp/tools.py` shows no changed line between `def manage_relation(` and the end of its docstring.
7. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling); see the assignment event.
