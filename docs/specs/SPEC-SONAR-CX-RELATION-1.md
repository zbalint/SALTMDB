# SPEC-SONAR-CX-RELATION-1

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776` cognitive complexity). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` after SPEC-SONAR-CX-MEMORY-3 is committed; `./verify` exits 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the public functions of `relation_service` with temporary databases.
- Scope (may edit), source:
  - `src/saltmdb/domain/services/relation_service.py`
- Scope (may edit), tests: existing relation tests (`tests/test_relation_service.py`, `tests/test_bitemporal_relations.py`, `tests/test_manage_relation_id_resolution.py`, `tests/test_conflict_set_service.py`, `tests/test_lineage_assembly_service.py`).
- Does not touch: everything else.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Sonar reports 6 functions above the cognitive-complexity limit of 15 in this group. `relation_service.py` holds the graph writes; this spec takes the six functions below score 35. The fix is a behavior-preserving refactor: extract cohesive private helpers until each function is below the limit. The owner's rule: never close or hide a finding; whatever cannot be fixed with certainty is reported.

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
- D12. `_get_lineage_raw` is imported by `lineage_assembly_service.py` and `conflict_set_service.py`: its name and signature are frozen. `_resolve_and_validate_parent_ids` has no direct test: it is reached through `consolidate_memories` (line 1433) and `bulk_commit_consolidation` (line 1954); characterize it through those.

## 3. Changes per file

Line numbers and scores come from the Sonar analysis of commit `5a4e7ac` and may have drifted by a few lines; match by function name.

| File | Function | Line | Sonar score |
|---|---|---|---|
| `src/saltmdb/domain/services/relation_service.py` | `invalidate_relation` | 579 | 30 |
| `src/saltmdb/domain/services/relation_service.py` | `analyze_dependencies` | 772 | 34 |
| `src/saltmdb/domain/services/relation_service.py` | `_get_lineage_raw` | 972 | 29 |
| `src/saltmdb/domain/services/relation_service.py` | `_resolve_and_validate_parent_ids` | 1274 | 18 |
| `src/saltmdb/domain/services/relation_service.py` | `bulk_commit_consolidation` | 1907 | 21 |
| `src/saltmdb/domain/services/relation_service.py` | `bulk_store_relations` | 2054 | 25 |

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

Other Sonar rules, other files, any change of behavior, performance work, and these items: `store_relation` and `consolidate_memories` (their own specs), S107.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy command in section 6 reports no function above 12 in the files in scope, except items listed under D6 in the report.
3. `git diff --stat` lists only files from section 0 scope.
4. The report lists, per function: before and after proxy score, the characterization tests added (or why none), and anything left under D6.
5. Mutation check for the new tests: for a sample of at least one behavior per refactored function, break it in the production file in the assigned checkout, show a new or existing test fails, restore it; the tree must match your own diff afterward.
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read show the S3776 keys for these functions gone.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling, patch targets, extraction needs); see the assignment event.
