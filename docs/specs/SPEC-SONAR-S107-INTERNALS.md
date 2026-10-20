# SPEC-SONAR-S107-INTERNALS

## 0. Status

LOCKED 2026-10-20

- Backlog item: none. Sonar rule `python:S107` (2 issues of 9; targets confirmed at `c3916ab`: Sonar's reported ranges 446-467 in `write.py` and 718-734 in `lifecycle.py` are the parameter lists of these two functions, and the issue messages name them): `_insert_raw_entity` (21 parameters, `src/saltmdb/domain/services/memory_service/write.py` 445) and `_replacement_operation` (16 parameters, `memory_service/lifecycle.py` 717), open on `develop` `0ec5fb1`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Owner decision (2026-10-20): internals first. The five public service functions (`store_memory`, `search_memory`, `consolidate_memories`, `revise_memory`, `supersede_memory`) and the two `mcp/tools.py` functions are NOT touched and stay open: their public signatures have about 540 test call sites and, for the tools, are the MCP schema.
- Baseline: `develop` at `c3916ab` or later; `./verify` exits 0 (2951 passed or more, 1 skipped, 233 subtests).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the existing suites that reach these functions through `store_memory`, `revise_memory` and `supersede_memory`; no test calls the two private functions directly (rg: zero hits in `tests/`, `hooks/`, `scripts/`).
- Scope (may edit): `memory_service/write.py` and `memory_service/lifecycle.py`, plus the cascade of D3 within these two files.
- Does not touch: the public signatures of the five service functions, `mcp/tools.py`, `__init__.py` re-exports, any test, any SQL text or bound-parameter order.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Two private functions have 21 and 16 parameters, so Sonar reports `python:S107`. Each has one fixed caller shape: `_insert_raw_entity` has one caller (`write.py` 680) that passes 20 keyword values; `_replacement_operation` is a pure delegate of `revise_memory` and `supersede_memory`, which forward all their parameters. A small frozen dataclass per function carries the values as one argument. Rejected: changing the five public service signatures now (about 540 test call sites, owner deferred), `**kwargs` bags (hide the parameter set from type checking and from Sonar without removing it), suppressions and issue-status changes (owner rules).

## 2. Decisions

- D1. `write.py`: add `@dataclass(frozen=True, slots=True) class _RawEntityRow` with exactly the 20 keyword-only fields of `_insert_raw_entity` today (`entity_id, now, agent_id, scope, is_core_val, weight, title, redacted_content, metadata_str, context_id, agent_session_id, content_hash, quality_score, quality_status, quality_flags_str, memory_type, final_retrieval_text, final_retrieval_hash, core_state, core_detail_ids_str`), same names, same annotations, in the same order. `_insert_raw_entity(conn, row: _RawEntityRow) -> None`. Its body reads `row.<field>`; the SQL text, the parameter tuple order and the `memory_type` that appears twice in that tuple are unchanged byte for byte. The caller at 680 builds `_RawEntityRow(...)` with the same keyword values and passes it.
- D2. `lifecycle.py`: add `@dataclass(frozen=True, slots=True) class _ReplacementRequest` with the 15 values `_replacement_operation` takes after `operation` (`entity_id, title, tags, content, reason, agent_id, context_id, scope, memory_type, agent_session_id, metadata, repoint_relations, db_connection, db_path, in_transaction`), same annotations and defaults as today (`repoint_relations` False, `in_transaction` False; the old `_in_transaction` parameter becomes the field `in_transaction`), with ONE annotation added: today `db_connection` is unannotated, and a dataclass only turns annotated attributes into fields, so declare `db_connection: sqlite3.Connection | None = None` (`sqlite3` is already imported in `lifecycle.py`). Every other field is annotated today. `_replacement_operation(operation, request: _ReplacementRequest) -> dict`. `revise_memory` and `supersede_memory` keep their signatures exactly and build the request from their own parameters. The body of `_replacement_operation` reads `request.<field>`, with EXACTLY four local rebindings that exist today for typing (`lifecycle.py` 763-766): `entity_id = cast(str, request.entity_id)`, `title = cast(str, request.title)`, `content = cast(str, request.content)`, `reason = cast(str, request.reason)`, placed where the current casts are (a frozen dataclass cannot be reassigned, and mypy runs on `src`). Bind no other field to a local (review grep: `= request\.` shows exactly these four lines). Do not change `_replacement_prepare` or any other helper's parameters (`_replacement_prepare` has exactly 13, the S107 limit; passing it the request would drag a third function into the slice). Behaviour is identical: the validation order, the `cast` calls, the connection handling (`should_close`), the `partial(_replacement_write, ...)` call, the error log text "Error %sing memory: %s" and the response are unchanged.
- D2a. Both dataclasses carry mutable fields (`core_state`, `tags`, `metadata`), so their generated `__hash__` raises on `hash()`. Nothing hashes them; add a one-line comment on each class saying they must not be hashed or cached.
- D3. Cascade rule (fixed point, from the lessons of SPEC-SONAR-UNUSED-PARAMS): after the change run `ruff check --select ARG,F,RET` on the two edited files against the baseline copy. A private function (leading underscore, not re-exported, not an MCP tool or RPC handler) that is left with an unused parameter, local or import is cleaned too, with its callers. STOP and report BLOCKED only if the cascade reaches a public function, an existing test, or a file outside `write.py` and `lifecycle.py`.
- D4. Exact keyword/positional compatibility is not needed anywhere else: the two functions are private and have the callers named above only. The developer states the rg evidence in the report (callers of `_insert_raw_entity` and `_replacement_operation` in `src`, `tests`, `hooks`, `scripts`).
- D5. No new Sonar finding: no function with more than 13 parameters, no unused locals, no nested ternaries, no suppression. `_RawEntityRow` and `_ReplacementRequest` are private names (leading underscore).

## 3. Changes per file

| File | Change |
|---|---|
| `src/saltmdb/domain/services/memory_service/write.py` | `_RawEntityRow`; `_insert_raw_entity` signature and body reads; the one caller |
| `src/saltmdb/domain/services/memory_service/lifecycle.py` | `_ReplacementRequest`; `_replacement_operation` signature; `revise_memory` and `supersede_memory` call sites |

## 4. Tests

No new test and no edited test: both functions are reached by the existing store, revise and supersede suites, whose results must not change. The mutation check in section 8 proves those suites detect a mis-wired field.

## 5. Documentation

None. Docstring text of `_replacement_operation` stays (its `repoint_relations` explanation is referenced from `revise_memory` and `supersede_memory`).

## 6. Verification procedure

`./verify`; `ruff check --select ARG,F,RET` on both files against the baseline copy; `rg -n "_insert_raw_entity|_replacement_operation|_RawEntityRow|_ReplacementRequest" src tests hooks scripts` and read every hit; a diff of the `INSERT` statement text and the parameter tuple old versus new (they must be identical).

## 7. Out of scope

The nine other S107 issues, `__init__` re-exports, any public signature, the MCP tools.

## 8. Acceptance

1. `./verify` exits 0.
2. `git diff --stat` lists only `write.py` and `lifecycle.py`; no test file changes.
3. The SQL string and the parameter tuple of `_insert_raw_entity` are byte-identical to `HEAD` (the architect compares with an AST string-literal multiset and a text diff of those lines).
4. Mutation sample on a scratch copy (repo tests as killers, tester names them): swap two fields when building `_RawEntityRow` at the caller (for example `quality_status` and `quality_flags_str`); drop `scope` from the `INSERT` tuple read (`row.scope` replaced by `row.agent_id`); in `revise_memory`, build the request with `"supersedes"` semantics (pass the wrong operation string); drop `repoint_relations` when building the request in `supersede_memory`; set `in_transaction` forwarding in `revise_memory` (always `False`; killed by `tests/test_lifecycle_replacements_characterization.py` 215-221, where the caller holds `BEGIN IMMEDIATE` and passes `_in_transaction=True`). The reverse mutant (`True` always) may survive, because on a shared connection status-only assertions still see the write; if the tester finds a surviving mutant it is reported, not a blocker. Each other mutant fails an existing test (predicted killers: the store characterization tests for the swapped quality columns and for `scope`, `tests/test_lifecycle_replacements.py` 54 for the operation string and 202-208, 306-312, 364-370 for `repoint_relations`).
5. Architect, after commit and CI: the Sonar search shows the two S107 issues (`_insert_raw_entity`, `_replacement_operation`) no longer open on `develop`, with no new issue in the two files. If the dataclass form does not clear them, report; do not suppress.

## 9. Pre-lock gate notes

Consultant read-only review (m_2630, AST pass at `f026c00`, no change applied; the scratch-copy apply-and-test version was withdrawn because it is implementation work, the developer's D3 fixed point and the tester's mutation pass cover it): callers confirmed (one each in `write.py` 680, `lifecycle.py` 856 and 896; none in tests, hooks or scripts); frozen slots dataclass with a dict field and a Connection field builds on 3.11 (the 3.10 `slots=` support is from the documentation, not run); after the change both functions have 2 parameters and the next largest function in the two files is `_replacement_prepare` at 13 (not flagged); predicted cascade: none. Folded in: B1 (four cast locals), B2 (`db_connection` annotation), the hash note, the scope wording, the mutant list. Not executed: the mutants, ruff on a modified tree, the 3.10 floor.
