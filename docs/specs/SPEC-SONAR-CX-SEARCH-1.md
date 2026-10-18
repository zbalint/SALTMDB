# SPEC-SONAR-CX-SEARCH-1

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776`, function `search_memory` in `src/saltmdb/domain/services/memory_service/orchestrator.py`, score 350, 1,015 lines, 30 parameters). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.
- Baseline: `develop` after SPEC-SONAR-CX-MEMORY-3 is committed; `./verify` exits 0.
- Test seams: `saltmdb.domain.services.memory_service.search_memory` (and its `memory_service` re-export), called with a temporary database as the existing search tests do, plus the viewer search route only where it already calls `search_memory`.
- Scope (may edit): tests only. New file `tests/test_search_memory_characterization.py` (and, if a fixture helper is clearly shared, the existing neighbouring test file it belongs to).
- Does not touch: every production file, including `orchestrator.py`.

## 1. Why

`search_memory` is the heart of retrieval and the biggest Sonar complexity finding (350). 26 test files reference it, but nobody has written down which branches are pinned. A refactor of 1,015 lines without that safety net is unacceptable. This spec only builds the net. SPEC-SONAR-CX-SEARCH-2 and -3 then split the function in two halves.

## 2. Decisions

- D1. No production code changes. This spec is tests only.
- D2. Measure first: run the existing search tests under branch coverage (`.venv/bin/python -m coverage run --branch -m pytest <the 26 files>` then `coverage report --show-missing --include=src/saltmdb/domain/services/memory_service/orchestrator.py`) and write down, in an event under the shared context, the line ranges of `search_memory` that no test executes.
- D3. For each uncovered or unpinned branch reachable through `search_memory`'s arguments, add a characterization test with literal expected results taken from running the current code, written down before any later refactor: every `mode` (`broad`, `strict`, `history`), `collapse_supersedes_families` on and off, cursor handling (`offset:` cursors and the missing-connection path near the top of the function), `return_diagnostics`, metadata filters (including the invalid-key rejection and its exact error text), tag/agent/scope/type filters, relevance gating and abstention, empty results, and the `**search_kwargs` pass-through used by `viewer/routes/search.py`.
- D4. Tests use temporary SQLite databases and the existing fixtures; stubs for embedding/reranker follow the existing tests (they patch `search_primitives.<name>` and `reranker_service` module names, so patch the same way). Expected values are literals, never recomputed with the code under test.
- D5. Branches that cannot be reached through the public arguments are listed in the report with the line range; do not alter production code to reach them.

- D13. No new findings (owner rule, 2026-10-17; amendment before assignment). The refactor must not introduce a new Sonar finding. Every extracted helper takes only the parameters it reads, and no parameter or local is left unused in an edited or new function (rules `python:S1172`, `python:S1481` and `python:S3358`; the earlier splits left all three kinds). Also write no nested conditional expressions (`a if x else b` inside another conditional expression): use an `if` statement or a small helper. Check with `.venv/bin/ruff check --select ARG` on the edited files against a baseline taken before your first edit (only new hits count), and look for unused names bound by tuple-unpacking assignments, which ruff does not report (a small `ast` script is enough; loop targets are not flagged by Sonar). List both results, plus a scan for nested conditional expressions (an `ast` script counting `IfExp` nodes that contain another `IfExp`, compared with the same count in HEAD), in the report. Do not add suppressions, and remove a `# noqa` marker the refactor makes unnecessary. The goal is better code, not a quiet gate: if the right fix is outside scope, report it.
- D14. Pin what the extracted helpers now own. For every comparison operator at a boundary, every cap or limit, every sort or tie-break key, every early return and every access or governance gate in the functions of section 3, a repo test must fail when that one thing is flipped. Use exact equality at boundaries (measure float values the way the code does), fixed ids for tie-breaks (never uuid4 order), and assert full result sets and order, not counts or membership alone. After the extraction, run a mutation pass of your own on the new helpers with repo tests only, on a scratch copy of the current tree (never in the assigned checkout; check `saltmdb.__file__` resolves from the copy); a mutant counts as killed only when a named repo test fails; report survivors honestly. Each new test must also pass on the pre-refactor source (scratch copy of HEAD), except a test of a helper that only exists after the extraction, which skips there.

## 3. Changes per file

| File | Change |
|---|---|
| `tests/test_search_memory_characterization.py` | new characterization tests per D3 |

## 4. Tests

The deliverable is the tests. Every new test passes on the unmodified code.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`. The coverage command in D2 is not part of `./verify`; run it for the measurement only and keep it out of the repo.

## 7. Out of scope

Any production change, any refactor, any other function.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only test files (the new file and, at most, one existing neighbouring test file).
3. The report states coverage of `search_memory` lines before and after, and lists the remaining uncovered line ranges with reasons (D5).
4. Mutation check, at least eight distinct mutations spread over the function (for example flip a `mode` comparison, drop the supersession collapse, change a filter, change the abstain threshold, alter the cursor offset, change a diagnostics key): break each in a scratch copy of the current working tree (never in the assigned checkout), show a named repo test fails, delete the copy; the assigned checkout must still match your own diff afterward (hash it before and after).
5. Architect: tester pass; no Sonar re-read (no production change).

## 9. Pre-lock gate notes

Consultant (m_2030): `search_memory` has 26 referencing test files, is called with keywords from `dispatch.py` line 251 and with `**search_kwargs` from `viewer/routes/search.py` line 47; module-attribute calls into `search_primitives` are deliberate.
