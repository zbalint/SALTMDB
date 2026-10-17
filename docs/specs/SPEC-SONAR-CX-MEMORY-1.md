# SPEC-SONAR-CX-MEMORY-1

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-SERVICES-2 is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `revise_memory` and `supersede_memory` (the callers) with temporary databases.
- Scope (may edit), source:
  - `src/saltmdb/domain/services/memory_service/lifecycle.py`
- Scope (may edit), tests: existing tests that drive `revise_memory`/`supersede_memory` (find them with `rg -l "revise_memory|supersede_memory" tests`); new tests go beside them.
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 1 functions above the cognitive-complexity limit of 15 in this group. `lifecycle._replacement_operation` (387 lines, score 58, 16 parameters, a `_write` closure) performs memory replacement for `revise_memory` and `supersede_memory`; it gets its own spec. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

## 2. Decisions

- D1. Behavior is unchanged: outputs, exit codes, log text, error messages, SQL text, call order and side-effect order stay identical. Public names and signatures stay as they are.
- D2. Characterization tests come first. For each function in section 3, run the focused tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <tests>` then `coverage report --show-missing --include=<file>`; `coverage` is already a project dev dependency) and list the function's uncovered lines and branches. Write failing-or-passing characterization tests through the test seams for every uncovered branch whose behavior the refactor could change. Expected values are literals taken from running the unrefactored code, written down before the refactor. A branch that cannot be reached through a seam is listed in the report; then refactor it only by pure mechanical extraction (moving lines unchanged into a helper).
- D3. Extract private module-level helpers (or private methods on the same class) in the same file, with names that say what they do. No new modules, no new dependencies, no behavior flags. Do not "improve" logic while moving it.
- D4. Measure with the proxy in section 6: every function in section 3 and every new helper reaches `complexipy` score at most 12 (a margin below Sonar's 15, because the two tools differ by a few points). If the proxy and Sonar disagree after the architect's scan, a follow-up fix is assigned.
- D5. Remove a `# noqa: C901` / `# noqa: PLR09xx` marker only when the function no longer needs it (`.venv/bin/ruff check` passes without it). Do not add suppressions.
- D6. A function that cannot be brought under the limit with a safe, mechanical change is left as it is and reported with the reason.
- D7. Service functions keep returning identical envelopes/dicts (keys, values, ordering) and issue the same SQL in the same order; log texts stay identical. Tests use temporary SQLite databases through the existing fixtures; never the working daemon or the real `~/.saltmdb`.
- D8. Extraction allowance (consultant-verified need). Two kinds of extraction are not a pure line move and are allowed, with statement order and transaction scope unchanged: (a) a block that contains an early `return`, `continue` or `break` inside a loop or `try` may be extracted with a sentinel or tuple return that the caller turns back into the same control flow; (b) a nested closure that captures locals (for example `_write`, `_do_store`, `_do_commit`, `render`) may be hoisted to a module-level or same-class private function taking the captured values as explicit parameters. A closure passed to the database coordinator keeps running in the same transaction, with the same statements in the same order. Sonar counts nested functions into the outer function, so hoisting is usually needed.
- D9. Call-time lookup rule. Tests patch module constants and module-global functions. Every helper must read them as module globals at call time (never as a default argument, never copied at import), and extracted code keeps calling `search_primitives.<name>` as a module attribute where the original does (see the comment at the top of `orchestrator.py`).
- D10. Frozen names and signatures. These are imported or patched elsewhere and keep their exact names and signatures: `lifecycle._assemble_memory_record`, `lifecycle._archive_entity_unchecked`, `relation_service._get_lineage_raw`, `core_governance_service._escape_yaml_line`, `librarian_service._librarian_trigger_pool`, every name re-exported from `memory_service/__init__.py` (including `_collapse_supersedes_families`, `_build_cross_encoder_candidate_texts`, `_resolve_supersession_chains`, `_store_raw_entity`, `bulk_archive_memory`, `check_duplicate_memories`, `chunk_candidate_search`, `fetch_memory_chunk`, `get_relevance_preview_data`, `resolve_or_create_tag`, `search_memory`, `store_memory`, `update_memory_metadata`), and `reranker_service.score_pairs` (its signature must keep matching its two `@overload` stubs). Refactor bodies only.
- D11. Proxy target: at most 12 for every new helper and for the listed function; where the function is very large (score above 60) at most 15 is accepted for the function itself if the report shows why 12 was not reachable safely, and the architect's Sonar re-read decides.
- D12. `Sonar S107` (16 parameters) on `_replacement_operation` is not part of this spec; it is a private function and a later parameter-object change may fix it. Report it.

- D13. No new findings (owner rule, 2026-10-17; amendment before assignment). The refactor must not introduce a new Sonar finding. Every extracted helper takes only the parameters it reads, and no parameter or local is left unused in an edited or new function (rules `python:S1172` and `python:S1481`; the earlier splits left both kinds). Check with `.venv/bin/ruff check --select ARG` on the edited files against a baseline taken before your first edit (only new hits count), and look for unused names bound by tuple-unpacking assignments, which ruff does not report (a small `ast` script is enough; loop targets are not flagged by Sonar). List both results in the report. Do not add suppressions, and remove a `# noqa` marker the refactor makes unnecessary. The goal is better code, not a quiet gate: if the right fix is outside scope, report it.
- D14. Pin what the extracted helpers now own. For every comparison operator at a boundary, every cap or limit, every sort or tie-break key, every early return and every access or governance gate in the functions of section 3, a repo test must fail when that one thing is flipped. Use exact equality at boundaries (measure float values the way the code does), fixed ids for tie-breaks (never uuid4 order), and assert full result sets and order, not counts or membership alone. After the extraction, run a mutation pass of your own on the new helpers with repo tests only, on a scratch copy of the current tree (never in the assigned checkout; check `saltmdb.__file__` resolves from the copy); a mutant counts as killed only when a named repo test fails; report survivors honestly. Each new test must also pass on the pre-refactor source (scratch copy of HEAD), except a test of a helper that only exists after the extraction, which skips there.

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/domain/services/memory_service/lifecycle.py` | `_replacement_operation` | 274 | 58 |

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

Other Sonar rules, other files, any change of behavior, performance work, and these items: S107.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file of a scratch copy of the current working tree (never in the assigned checkout), show a named repo test fails, delete the copy; the assigned checkout must still match your own diff afterward (hash it before and after).
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling, patch targets, extraction needs); see the assignment event.
