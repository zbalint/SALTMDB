# SPEC-BL064-S6A-CONSOLIDATION-PREFLIGHT

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 6a of 6a, 6b, 6c (the consolidation chain in `relation_service.py`). Shared `context_id`: `saltmdb-sonar-fix-2026-10`. 6a covers the PRE-transaction half: validation, parent resolution, cohesion measurement and override, content and duplicate preflight, and how `consolidate_memories` consumes the result. 6b (the commit half) and 6c (the bulk path) follow.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): `.venv/bin/python -m pytest tests/test_relation_service.py tests/test_consolidate_memories_phase4.py tests/test_consolidation_grouping.py tests/test_consolidation_batch_v2.py tests/test_consolidation_atomicity.py tests/test_consolidation_is_core_inheritance.py tests/test_consolidation_quality.py tests/test_consolidation_tag_alias_bugfix.py tests/test_consolidation_embedding_trigger.py tests/test_phase4_mcp_surface.py tests/test_mcp_consolidate_tool_v2.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `consolidate_memories` (public service function; about 40 direct test calls), `apply_consolidations` and `bulk_commit_consolidation` (which call it with `_bulk=`), the MCP tool above them, and one direct call of `_consolidation_measure_cohesion` (section 4, item 3).
- Scope (may edit): `src/saltmdb/domain/services/relation_service.py`, ONLY: `_consolidation_measure_cohesion`, `_consolidation_apply_cohesion_override`, `_consolidation_content_preflight`, `_consolidation_duplicate_preflight`, `_consolidation_validate_request`, `_consolidation_open_and_validate_parents` (REMOVED, its body becomes `_consolidation_validate_parents`), `_consolidation_preflight`, the new `_consolidation_build_plan`, `_consolidation_cohesion` (DELETED), the dataclass block next to `_BulkContext` (new types), and in `consolidate_memories` the lines from `provenance = provenance or Provenance()` through the construction of `cohesion` and `entity_values`, plus local aliases that keep the rest of the function byte-identical (D5); `tests/test_consolidation_preflight.py` (new characterization tests only).
- Does not touch: the commit half (`_consolidation_commit`, `_consolidation_audit_override`, `_consolidation_archive_parents`, `_consolidation_insert_entity`, `_consolidation_core_state`, `_consolidation_revalidate`, `_consolidation_collect_worklist`, `_consolidation_insert_lineage`; 6b), the bulk functions and `apply_consolidations` (6c), the second half of `consolidate_memories` (from the `try:` onward), its public signature and docstring, the `commit_consolidation` alias, `_resolve_and_validate_parent_ids`, any SQL text, any threshold, any other module.
- Names and shapes that tests pin and MUST stay: `_BulkContext(precomputed_centroids, precomputed_unresolved, precomputed_observed_state)` in this field order (positional and keyword: `tests/test_consolidation_grouping.py` 70, 176, 382, 402); the module attributes `_consolidation_commit`, `_write_bulk_consolidations` and `_consolidation_insert_lineage`; module attributes read at call time: `get_connection`, `close_connection`, `evaluate_memory_quality`, `check_duplicate_memories`, `min_pairwise_cohesion`, `get_fresh_entity_centroids` (called as `get_fresh_entity_centroids(resolved_parents, conn)` with exactly two positional arguments: `tests/test_consolidate_memories_phase4.py` ~902 asserts `len(call.args) == 2`), `core_governance_service.parse_is_core`; `validate_tag_names` imported LAZILY inside the function that uses it (tests patch it at its source module, `phase4` 380 to 384: hoisting the import breaks them); the exact rejection envelopes (codes, messages, fields) compared with `assertEqual` in `phase4` 318 to 400 and 541.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The pre-transaction half of a consolidation passes its results as long positional tuples and scalar lists. `_consolidation_preflight` takes twelve parameters and returns EITHER an error dict OR a 16-element tuple that `consolidate_memories` unpacks into sixteen locals; `_consolidation_cohesion` is pure glue that returns a 7-element tuple; `_consolidation_measure_cohesion` takes five parameters (three of them the three pieces of `_BulkContext` taken apart again) and returns 4 values; `_consolidation_content_preflight` returns 7 values; and every helper carries `conn` plus `should_close` solely so that each rejection branch can close a connection it owns by hand (six `if should_close: close_connection(conn)` sites). `consolidate_memories` then rebuilds `_ConsolidationCohesionResult` and `_ConsolidationEntityValues` from the sixteen locals it just unpacked.

A latent defect sits in the same code: an EXCEPTION (not a rejection) raised between opening an owned connection and the end of the preflight (a `sqlite3.Error` from a parent or observed-state read, or `evaluate_memory_quality` raising) leaks the connection, because only rejection branches close it.

Decisions already taken (do not reopen): ownership of the connection lives in ONE place, `_consolidation_preflight`; the helpers take a plain `conn` and never close it. Rejected: a `reject()` method on a connection-owner object (it still leaves eight helpers that know about ownership, and a later plain `_consolidation_rejected(...)` return would leak without anything catching it); a context manager (the single-call path closes the connection in a `finally` after the commit, so the preflight must hand the connection over, not release it); a dict result (a bag); changing `consolidate_memories`' signature.

## 2. Target shape

All private, in `relation_service.py`, next to `_BulkContext`. `_ConsolidationCoreRequest`, `_ConsolidationCohesionResult` (`override_applied`, `min_sim`, `offending_pair`, `unresolved`, `justification`) and `_ConsolidationEntityValues` keep their fields (6b changes `_ConsolidationEntityValues` to embed `_ConsolidationContent`: 6a must not rename the fields of `_ConsolidationContent` in a way 6b would undo).

```python
@dataclass(frozen=True, slots=True)
class _ConsolidationRequest:
    """The arguments of one consolidate call that the preflight needs."""
    parent_ids: list[str]
    title: str
    content: str
    tags: list[str] | None
    override_justification: str | None
    core_request: _ConsolidationCoreRequest
    agent_id: str | None

@dataclass(frozen=True, slots=True)
class _CohesionMeasurement:
    min_sim: float
    offending_pair: tuple[str, str] | None
    unresolved: dict[str, str]
    observed_state: dict[str, tuple[str, str]]

@dataclass(frozen=True, slots=True)
class _ConsolidationContent:
    redacted_content: str
    clean_title: str
    agent_val: str
    content_hash: str
    quality_score: float
    quality_status: str
    quality_flags_str: str

@dataclass(frozen=True, slots=True)
class _ConsolidationPlan:
    """Everything the preflight established; the commit and the response read it."""
    conn: sqlite3.Connection
    owned: bool
    resolved_parents: list[str]
    cohesion: _ConsolidationCohesionResult
    observed_state: dict[str, tuple[str, str]]
    content: _ConsolidationContent
```

(All four hold lists or dicts: add the one-line "must not be hashed or cached" comment `ReplacementRequest` carries. `_ConsolidationCohesionResult.offending_pair` may stay `Any`.)

Functions after the change (hand count, all positional; "X | dict" means the function returns X or a rejection envelope dict):

- `_consolidation_validate_request(request) -> dict | None`: 1 parameter (was 3); runs BEFORE any connection exists.
- `_consolidation_preflight(request, db_connection, db_path, bulk: _BulkContext | None) -> _ConsolidationPlan | dict`: 4 parameters (was 12). The ONLY closer, kept small (about McCabe 6) so that it stays under ruff's C901 limit of 12 and Sonar's cognitive limit of 15 (an inline version of the five steps with an `if owned` after each would not): validate the request (return the rejection; nothing is open); open the connection (`conn = db_connection or get_connection(db_path or get_db_path())`; write `owned = not db_connection` literally, i.e. truthiness exactly as today's `if not conn`, so a falsy non-None object is owned as before; a real `sqlite3.Connection` is always truthy; `get_connection` can return the active coordinator connection for which `close_connection` is a no-op); then `try: outcome = _consolidation_build_plan(conn, owned, request, bulk)` / `except BaseException: if owned: close_connection(conn); raise` (closes the pre-existing leak; the one intended behaviour change, D1); `if owned and isinstance(outcome, dict): close_connection(conn)`; `return outcome`. A success returns the plan WITHOUT closing (the single-call `finally` in `consolidate_memories` closes it).
- `_consolidation_build_plan(conn, owned, request, bulk) -> _ConsolidationPlan | dict`: 4 parameters; never closes. The five steps in today's order with early returns of a rejection dict: `_consolidation_validate_parents`, `_consolidation_measure_cohesion`, `_consolidation_apply_cohesion_override`, `_consolidation_content_preflight`, `_consolidation_duplicate_preflight` (which returns `dict | None`: check `is not None`, the others `isinstance(..., dict)`), then the `_ConsolidationPlan` (about McCabe 5).
- `_consolidation_validate_parents(conn, request) -> list[str] | dict`: 2 parameters; parent resolution and tag validation, i.e. the body of today's `_consolidation_open_and_validate_parents` without the opening; it is the first step of `_consolidation_build_plan`. The lazy import of `validate_tag_names` stays inside it.
- `_consolidation_measure_cohesion(conn, resolved_parents, bulk: _BulkContext | None) -> _CohesionMeasurement`: 3 parameters (was 5); the precomputed maps are used only when ALL THREE fields of `bulk` are not `None`, exactly as today; the `len(resolved_parents) >= 2` structure and the single-parent branch (`observed_state` captured with `_observe_parent_state`, `min_sim` 1.0) are kept even though the single-parent branch cannot be reached through `consolidate_memories` (section 4, item 3).
- `_consolidation_apply_cohesion_override(content, override_justification, measurement) -> tuple[str, _ConsolidationCohesionResult] | dict`: 3 parameters (was 7); no connection parameter (the caller closes); returns the possibly annotated content and the filled `_ConsolidationCohesionResult` with `justification` = the STRIPPED justification, `offending_pair` and `min_sim` unchanged, `override_applied` as today; returns the `REJECT_LOW_COHESION` rejection as today. `_consolidation_cohesion` is deleted; the preflight calls measure, then override, and reads `observed_state` from the measurement.
- `_consolidation_content_preflight(request, content) -> _ConsolidationContent | dict`: 2 parameters (was 6); `title`, `is_core` and `agent_id` come from the request, `content` is the possibly annotated content; the core-flag parse (`core_governance_service.parse_is_core`) and the quality gate keep their order.
- `_consolidation_duplicate_preflight(conn, resolved_parents, content) -> dict | None`: 3 parameters (was 7).
- `consolidate_memories`: builds `_ConsolidationRequest` once from its arguments and calls the preflight; on a dict returns it; then `cohesion = plan.cohesion` and builds `_ConsolidationEntityValues` from `plan.content` and `provenance`, `annotations`, `scope`, `weight` (15 keyword arguments as today). Local aliases right after: `conn, should_close = plan.conn, plan.owned`, `resolved_parents = plan.resolved_parents`, `observed_state = plan.observed_state`, so that the second half of the function (from the `try:`) stays byte-identical for 6b to rebase on. The commit reads `tags` from the local, as today. The builds of `consolidated_id`, `now`, the two dataclasses stay before the `try:`, as today.

## 3. Decisions

- D1. Behaviour is identical, with ONE intended exception: an exception (not a rejection) raised inside the preflight now closes an owned connection before it propagates (previously it leaked it). Everything else is unchanged: the order of the checks (request validation, parent resolution, tag validation, cohesion measurement, override and content annotation, core-flag parse, quality gate, exact-duplicate lookup, near-duplicate warning), every rejection code, message and field, the closing of an owned connection on every rejection and never of a caller-owned one, success leaving it open, the precomputed-maps rule, the quality and hash values (the hash is computed from the ANNOTATED content), and the log messages.
- D2. `close_connection` is called in exactly one function in the preflight family, `_consolidation_preflight`; no other helper (including `_consolidation_build_plan`) calls it.
- D3. No new third-party import; `dataclass` is already imported. No `noqa`.
- D4. The exact-hash lookup keeps its `try/except sqlite3.Error` (only that exception) and its WARNING; the near-duplicate check keeps its `try/except (sqlite3.Error, ValueError)` and its WARNING. Do not widen either (rule 15: deliberate, logged, unchanged).
- D5. The second half of `consolidate_memories` (from the `try:`) is not edited; it keeps reading the names `conn`, `should_close`, `resolved_parents`, `observed_state`, `tags`, `cohesion`, `entity_values`, `core_request`, `consolidated_id`, `now`, `orphaned_edges`.

## 4. Tests (characterization, green before and after)

Most of this behaviour is pinned already; the developer lists the existing test per item and adds ONLY the missing ones, with literals taken from a run on the unchanged code.
1. Codes and messages of every preflight rejection: already pinned (`tests/test_consolidate_memories_phase4.py` 318 to 400 and 541). Connection closes already pinned for `REJECT_LOW_COHESION` and `UNKNOWN_PARENT_ID` (`phase4` 919 to 946; the pattern: patch `get_connection` to return the shared test connection and `close_connection` with a plain `Mock`, so the shared fixture connection is not really closed; a `wraps=` spy would close it). The rule to assert for a call WITHOUT `db_connection` (owned), with that Mock: one close per rejection that happens after the connection was opened, zero for the three that return before it is opened (`INVALID_PARENT_IDS`, `MISSING_REQUIRED_FIELDS`, `CONTENT_TOO_LONG`: `phase4` 330 already asserts `get_connection` is never called), and exactly one on SUCCESS (the `finally` in `consolidate_memories`); for a call WITH a caller-owned `db_connection`: zero in every case. MISSING (add): the owned-connection close count for `INVALID_TAG_NAME`, `INVALID_CORE_REQUEST`, `REJECT_QUALITY` and `REJECT_EXACT_DUPLICATE`.
2. Exception closes an owned connection (the intended fix): with `close_connection` as the Mock, make `evaluate_memory_quality` (module attribute) raise `RuntimeError` for a call without `db_connection`. The preflight runs before the `try:` of `consolidate_memories`, so the `RuntimeError` propagates to the caller; assert that it propagates AND that `close_connection` was called exactly once. RED on the old code (zero closes). Fixture note for ALL new close-count tests (items 1 and 2): the call must first get past the cohesion gate, or it ends in `REJECT_LOW_COHESION` before quality is evaluated; patch `get_fresh_entity_centroids` with aligned centroids as the `phase4` fixture does (~298 to 302) and `enqueue_embedding_jobs_for_entity` at its source module (~307).
3. Single-parent branch: unreachable through `consolidate_memories` (`_resolve_and_validate_parent_ids` returns `REJECT_PARENT_COUNT` for fewer than two distinct parents before the cohesion step; pinned at `phase4` 362 to 366: cite it). Pin the branch directly: call `_consolidation_measure_cohesion(conn, [one_id], None)` and assert `min_sim == 1.0`, `offending_pair is None`, `unresolved == {}` and a one-entry `observed_state` with the parent's `(content_hash, status)`.
4. Override: the content gets the `[Consolidation Override]` annotation (pinned at `phase4` ~905 to 917: cite) and the stored `content_hash` equals the hash of the ANNOTATED content (add if not pinned).
5. Precomputed maps: already pinned (`tests/test_consolidation_grouping.py` 176 with a `None` field so fresh centroids are computed, 382 and 402 with precomputed maps and `side_effect=AssertionError`): cite.

## 5. Out of scope

6b (including embedding `_ConsolidationContent` in `_ConsolidationEntityValues`) and 6c; the SQL; the thresholds; the MCP tool; deleting the unreachable single-parent branch (a separate decision); the stale `_do_commit` mention in the `consolidate_memories` docstring.

## 6. Acceptance

1. `./verify` exits 0.
2. The new tests of section 4 (item 1's four missing close counts, item 2, item 3, and item 4 if not pinned) are written first; item 2 is RED on the unchanged code, the others pass on it; all pass after the change (the developer reports both runs; the architect reruns on the final tree).
3. `rg -n "close_connection" src/saltmdb/domain/services/relation_service.py` shows no call inside the preflight helpers other than in `_consolidation_preflight`; `rg -n "should_close"` over the preflight helpers returns nothing (the second half of `consolidate_memories` still uses the alias) (judge: the architect reads the hits).
4. `git status --short` lists only the files of section 0.
5. Mutation check on a scratch copy, killer named for each: close the connection on success; do not close on one rejection branch (one mutant for each of the four branches of item 1); do not close on exception; close a caller-owned connection; swap the duplicate check and the quality gate; skip the override annotation; compute the hash from the unannotated content; treat a `None` field of `_bulk` as present; change a rejection code; hoist the `validate_tag_names` import to module level (killed by the `phase4` patch); call `get_fresh_entity_centroids` with a keyword. A surviving mutant is reported (the single-parent mutant "skip `observed_state` for one parent" is killed only by item 3).
6. Design read (architect and consultant): at most 4 parameters per function, no tuple of more than 2, one responsibility per function, ownership of the connection in one place; `.venv/bin/ruff check --select C901,PLR0911,PLR0912,PLR0915 src/saltmdb/domain/services/relation_service.py` shows no new finding for the new functions (the architect compares with the baseline copy).

## 7. Claims

| Claim | Where | Verified |
|---|---|---|
| `_consolidation_preflight` has 12 parameters and returns a 16-tuple; `_consolidation_cohesion` has 8 parameters, returns a 7-tuple and is glue; `_consolidation_content_preflight` returns a 7-tuple | `relation_service.py` 2434, 2250, 2290 (byte-identical at `766b59c` for these functions) | read; consultant diff |
| Six `if should_close: close_connection(conn)` sites in the preflight helpers | lines 2219, 2301, 2311, 2355, 2418, 2427 | probed (6), consultant confirmed |
| `consolidate_memories` rebuilds `_ConsolidationCohesionResult` and `_ConsolidationEntityValues` from the unpacked locals | `relation_service.py` ~2594 to 2626 | read |
| The single-parent branch is unreachable through `consolidate_memories` (REJECT_PARENT_COUNT first) | `relation_service.py` 1746 to 1753; `phase4` 362 to 366 | consultant read |
| The three early validations return before a connection is opened | `phase4` 330 | consultant read |
| An exception inside the preflight leaks an owned connection today | no try/finally between the open and the end of the preflight | consultant read; RED test item 2 |
| `get_fresh_entity_centroids` is called with two positional arguments and asserted so | `phase4` ~902 | consultant read |
| `validate_tag_names` is patched at its source module | `phase4` 380 to 384 | consultant read |
| Tests reference none of the preflight helper names outside `relation_service.py` | `rg` over tests | consultant read |

## Amendment 1 (2026-10-24): section 4 item 3 is written in the baseline call form, then moved to the new one

The developer reported `BLOCKED` before any production edit. Section 4 item 3 names the 3-argument call `_consolidation_measure_cohesion(conn, [one_id], None)` and section 6 item 2 says that test passes on the unchanged code, but on the unchanged code the function has five parameters (`conn, resolved_parents, precomputed_centroids, precomputed_unresolved, precomputed_observed_state`, `relation_service.py` `_consolidation_measure_cohesion`) and returns the 4-tuple `(min_sim, offending_pair, unresolved, observed_state)` that `_consolidation_preflight` unpacks. The 3-argument call is a `TypeError` there, so the item cannot be green before the change. Verified: the baseline signature and its unpacking site read directly; no other caller or test references the function.

A1.1 Item 3 is written FIRST in the baseline form: `_consolidation_measure_cohesion(conn, [one_id], None, None, None)`, unpacked as `min_sim, offending_pair, unresolved, observed_state`, asserting `min_sim == 1.0`, `offending_pair is None`, `unresolved == {}` and the one-entry `observed_state` with the parent's `(content_hash, status)`. It passes on the unchanged code (section 6 item 2 holds as written) and the literals come from that run.

A1.2 After the change the developer makes exactly one mechanical edit to that test: the call becomes `_consolidation_measure_cohesion(conn, [one_id], None)` and the four assertions read the same values from the returned `_CohesionMeasurement` (`measurement.min_sim`, `.offending_pair`, `.unresolved`, `.observed_state`). No literal changes. The developer reports both runs (baseline form before, new form after).

A1.3 Nothing else changes: section 4 item 2 stays the only RED test on the unchanged code; the section 6 item 5 single-parent mutant is still killed only by item 3; the tests for items 1, 2, 4 and 5 are unaffected. Scope is unchanged (`tests/test_consolidation_preflight.py` is the new test file named by the developer; it is in the section 0 test scope).
