# SPEC-SONAR-CX-SEARCH-2

## 0. Status

LOCKED 2026-10-16

- Backlog item: none (Sonar cleanup, rule `python:S3776`, function `search_memory` in `src/saltmdb/domain/services/memory_service/orchestrator.py`, score 350, 1,015 lines, 30 parameters). Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.
- Baseline: `develop` after SPEC-SONAR-CX-SEARCH-1 is committed; `./verify` exits 0. The characterization tests of SPEC-SONAR-CX-SEARCH-1 exist and pass.
- Test seams: `search_memory` through the characterization tests of SEARCH-1 and the existing search tests.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/orchestrator.py`; test files only to add a characterization test for a branch SEARCH-1 left unpinned.
- Does not touch: `search_primitives.py`, `ranking.py`, `__init__.py` and every other file; the signature and docstring of `search_memory`.

## 1. Why

First half of the `search_memory` split: argument validation and normalization, the connection and cursor handling at the top, and the candidate-gathering stages (FTS, semantic, chunk candidates, the `_compute_pool` closure) up to the point where candidates are fused.

## 2. Decisions

- D1. Behavior is unchanged: result lists, diagnostics keys, error texts, SQL, log texts and call order identical.
- D2. Plan gate. Before any tracked edit, the developer writes the proposed helper boundaries (name, the line range of `search_memory` it takes, its parameters and return value, and which locals it reads or writes) into an event under the shared context and sends the architect a `QUESTION` with the same list. The architect confirms or adjusts it. Do not edit before the reply.
- D3. Extract private module-level helpers in `orchestrator.py`. Early `return`, `continue` or `break` inside the extracted block may use a sentinel or tuple return that the caller turns back into the same control flow. The nested closure `_compute_pool` may be hoisted with explicit parameters. Statement order and exception propagation stay identical.
- D4. Call-time lookup rule: helpers keep calling `search_primitives.<name>` as a module attribute (see the comment at the top of `orchestrator.py`), and read module constants such as `SEARCH_DIAGNOSTICS_MAX_CANDIDATES` as module globals at call time, because tests patch them.
- D5. Frozen: the name, parameters (30), defaults and docstring of `search_memory`; the `memory_service/__init__.py` re-export. `Sonar S107` is not part of this spec; report it.
- D6. Target after this spec: the front half moves into helpers; the function may still be above 15 after this spec, but it must be at least 40 percent shorter and the proxy score at most 200. If a safe extraction cannot reach it, stop and report; do not force it.
- D7. Measure with the complexipy proxy pinned at 8.0.1 in a scratch venv outside the repo (see `docs/specs/SPEC-SONAR-CX-HOOKS.md` section 6 for the exact commands). Every new helper at most 12.

- D13. No new findings (owner rule, 2026-10-17; amendment before assignment). The refactor must not introduce a new Sonar finding. Every extracted helper takes only the parameters it reads, and no parameter or local is left unused in an edited or new function (rules `python:S1172` and `python:S1481`; the earlier splits left both kinds). Check with `.venv/bin/ruff check --select ARG` on the edited files against a baseline taken before your first edit (only new hits count), and look for unused names bound by tuple-unpacking assignments, which ruff does not report (a small `ast` script is enough; loop targets are not flagged by Sonar). List both results in the report. Do not add suppressions, and remove a `# noqa` marker the refactor makes unnecessary. The goal is better code, not a quiet gate: if the right fix is outside scope, report it.
- D14. Pin what the extracted helpers now own. For every comparison operator at a boundary, every cap or limit, every sort or tie-break key, every early return and every access or governance gate in the functions of section 3, a repo test must fail when that one thing is flipped. Use exact equality at boundaries (measure float values the way the code does), fixed ids for tie-breaks (never uuid4 order), and assert full result sets and order, not counts or membership alone. After the extraction, run a mutation pass of your own on the new helpers with repo tests only, on a scratch copy of the current tree (never in the assigned checkout; check `saltmdb.__file__` resolves from the copy); a mutant counts as killed only when a named repo test fails; report survivors honestly. Each new test must also pass on the pre-refactor source (scratch copy of HEAD), except a test of a helper that only exists after the extraction, which skips there.

## 3. Changes per file

| File | Function | Sonar score at 5a4e7ac |
|---|---|---|
| `src/saltmdb/domain/services/memory_service/orchestrator.py` | `search_memory` | 350 |

## 4. Tests

The SEARCH-1 characterization tests are the net: run them after every extraction step. Add a test only for a branch they leave unpinned.

## 5. Documentation

None.

## 6. Verification procedure

`./verify`, plus the proxy command of D7.

## 7. Out of scope

Everything outside `search_memory`'s body in `orchestrator.py`; S107.

## 8. Acceptance

1. `./verify` exits 0.
2. The proxy reports every new helper at most 12 and `search_memory` at the score stated in D6.
3. `git diff -U0 src/saltmdb/domain/services/memory_service/orchestrator.py` shows no changed line between `def search_memory(` and the end of its docstring.
4. `git diff --stat` lists only `orchestrator.py` and, at most, test files.
5. Mutation sample: at least three behaviors in the extracted code broken one at a time, each making a named repo test fail, on a scratch copy of the current working tree (never in the assigned checkout).
6. Architect, after review and commit: `scripts/prepush-sonar.sh` and a Sonar re-read.

## 9. Pre-lock gate notes

Reviewed by a consultant before lock (function table, tests per function, coupling, patch targets, extraction needs); see the assignment event. The plan gate (D2) replaces guessing stage boundaries from a 1,015-line function at spec time.
