# SPEC-BL064-S7A-SEARCH-POOL-GATHER

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 7a of 7a and 7b (the search channel chain in `memory_service/orchestrator.py`). 7a covers the candidate-gathering half of one pool pass: the two `_gather_*` helpers, `_resolve_search_predecessors`, `_resolve_predecessor_evidence`, `_compute_search_pool` and `_search_window_pool`. 7b (the policy half: `_apply_search_pool_policies`, `_rerank_search_pool`, `_run_cross_encoder_scoring`) follows. `search_memory`'s public signature is slice 8. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): `.venv/bin/python -m pytest tests/test_search_memory_characterization.py tests/test_search_pagination.py tests/test_search_ranking_flags.py tests/test_strict_ranking_defaults.py tests/test_search_scores.py tests/test_relevance_gate.py tests/test_search_accuracy_stage2.py tests/test_ranking_search_primitives_characterization.py tests/test_cross_owner_dedup.py tests/test_related_memory_service.py tests/test_memory_service_characterization.py tests/test_lexical_adapter.py tests/test_mcp_search_tool_v2.py -q`.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `memory_service.search_memory(...)` (about 200 direct service test calls, all keyword) and the private helpers that tests call or patch by name (section 5). Every name and call shape in section 5 either stays or is changed by exactly the test edit listed there, and by no other.
- Scope (may edit): `src/saltmdb/domain/services/memory_service/orchestrator.py`, ONLY: `_gather_retrieval_candidates`, `_gather_chunk_candidates`, `_resolve_search_predecessors`, `_resolve_predecessor_evidence`, `_compute_search_pool`, `_search_window_pool`, the new types (defined ABOVE `_apply_search_pool_policies`, right after the module constants at lines ~33 to 37; `_SearchControls` stays where it is, line ~563), and in `search_memory` the single call into `_search_window_pool` (plus one line that builds the filters object); `tests/test_search_memory_characterization.py` (the call-shape edits of section 5 and new characterization tests only).
- Does not touch: `_apply_search_pool_policies` (7b: keep its 13-argument positional signature and keep calling it with the same values), `_rerank_search_pool`, `_run_cross_encoder_scoring`, `_order_search_pool`, `_gate_search_pool`, `_record_*`, `_log_search_gap`, `_assemble_search_results` and the rest of `search_memory` (slice 8), `_SearchControls` (its 19 fields and its constructor are pinned by `_search2_controls` in the tests), `search_primitives.py`, `ranking.py`, `validation.py`, the MCP layer, any SQL, any threshold.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

One pool pass threads the same values through six functions as scalars, lists and tuples. `where_clauses, params` travel together through `_search_window_pool` (8 parameters), `_compute_search_pool` (6), both `_gather_*` helpers (8 and 9) and `_resolve_search_predecessors` (11). The rows from each retrieval channel (`fts_rows_`, `used_or_fallback_`, `semantic_rows_`, `chunk_rows_`, the two retrieval-text row lists) and the id sets derived from them (`fts_ids_`, `fts_and_ids_`, `semantic_ids_`) are separate locals in `_compute_search_pool` that are re-splatted into `_resolve_search_predecessors`, `_resolve_predecessor_evidence` and `_apply_search_pool_policies`. `_compute_search_pool` explodes eleven fields of `_SearchControls` into locals and passes them on one by one, although the callees could read the controls. `_resolve_search_predecessors` returns an untyped 3-tuple. `_search_window_pool` takes both `controls` and a separate `mode` that is the same value as `controls.mode`.

Rejected: changing `_SearchControls`; a dict of channel rows (a bag); calling the primitives with keyword arguments (tests pin positional indices); recomputing the id sets on every use (they are computed once per pass today).

## 2. Target shape

All private, in `orchestrator.py`, defined right after the module constants (lines ~33 to 37) and therefore ABOVE every helper that is annotated with them: the module has no `from __future__ import annotations`, so a type defined below `_resolve_predecessor_evidence` (378), `_resolve_search_predecessors` (427) or the `_gather_*` helpers (196, 236) would raise `NameError` at import time. `_SearchControls` (563) is not moved. `controls` stays UNANNOTATED in every helper in 7a, as today (annotating it as `_SearchControls` would make `query_keywords` and `db_path` `str | None` and produce 6 to 8 new mypy errors against primitives that declare plain `str`).

```python
@dataclass(frozen=True, slots=True)
class _SearchFilters:
    """The WHERE clauses and bound parameters of one search; read-only once built."""
    # must not be hashed or cached: carries lists
    where_clauses: list[str]
    params: list

@dataclass(slots=True)
class _ChannelRows:
    """Candidate rows from each retrieval channel of one pool pass, plus the id sets derived once."""
    fts_rows: list
    used_or_fallback: bool
    semantic_rows: list
    chunk_rows: list
    retrieval_fts_rows: list
    retrieval_vector_rows: list
    fts_ids: set[str] = field(init=False)
    fts_and_ids: set[str] = field(init=False)
    semantic_ids: set[str] = field(init=False)
    # __post_init__: fts_ids = {r[0] for r in fts_rows}; fts_and_ids = fts_ids if not used_or_fallback else set(); semantic_ids = {eid for eid, _ in semantic_rows}. Treated as immutable after construction.

class _PredecessorResolution(NamedTuple):
    rrf_map: dict[str, float]
    resolved_from: dict[str, list[str]]
    predecessor_grounded_map: dict[str, bool]
```

(`_SearchFilters` is a frozen dataclass, not a NamedTuple: no test indexes it, and a tuple invites `where_clauses, params = filters`, the splat by another name. `_ChannelRows` holds lists and sets: the same one-line comment. `_PredecessorResolution` stays a NamedTuple because `tests/test_search_memory_characterization.py` ~2928 indexes the result, `result[0]`.)

Functions after the change (hand count, all positional):

- `_gather_chunk_candidates(filters, controls, diagnostics) -> tuple[list, dict]`: 3 parameters (was 8); reads `controls.query_keywords`, `.chunk_window`, `.chunk_oversampling`, `.db_path`, `.use_chunk_candidates`; the primitive is still called as `search_primitives.chunk_candidate_search(query_keywords, where_clauses, params, chunk_window, chunk_oversampling, db_path)` with six POSITIONAL arguments in this order; the default 8-key diagnostics dict, the `executed` flag and the REPLACEMENT of `diagnostics["chunk_candidate"]` by a copy (not an update) are unchanged.
- `_gather_retrieval_candidates(conn, filters, controls, candidate_window, diagnostics) -> tuple[list, list]`: 5 parameters (was 9); reads `controls.sanitized_query`, `.query_keywords`, `.db_path`, `.use_retrieval_text_candidates`; `_run_retrieval_fts_search(conn, sanitized_query, where_clauses, params, candidate_window, 0)` and `retrieval_vector_search(query_keywords, where_clauses, params, candidate_window, db_path, 0)` keep their positional shapes (the offset `0` is argument index 5; tests assert it); the diagnostics writes are unchanged.
- `_resolve_predecessor_evidence(pre_pool_ids, rrf_map, channels: _ChannelRows, controls) -> dict`: 4 parameters (was 8); uses `channels.fts_rows`, `.semantic_rows`, `.fts_ids`, `.used_or_fallback` and `controls.query_keywords`, `.db_path`; `_score_topics_with_fallback(query_keywords, pre_or_fallback_only_ids, db_path)` stays a 3-positional call; `ranking._build_candidate_evidence(pre_pool_ids, rrf_map, fts_rows, semantic_rows, pre_topic_scores_map, {}, used_or_fallback=...)` is unchanged.
- `_resolve_search_predecessors(conn, controls, filters, channels, rrf_map) -> _PredecessorResolution`: 5 parameters (was 11); reads `controls.mode`; `ranking._resolve_supersession_chains(conn, pre_pool_ids, where_clauses, params)` keeps its positional call (a test asserts `call_args.args[2:] == (where_clauses, params)`); the substitution happens only when strict and the resolved map is non-empty, otherwise the SAME `rrf_map` object is returned, as today.
- `_compute_search_pool(candidate_window, conn, filters, controls, diagnostics) -> dict`: 5 parameters (was 6). It no longer explodes `controls` into eleven locals; it builds one `_ChannelRows` from the five channel calls in today's order (`_run_fts_search(conn, sanitized_query, where_clauses, params, candidate_window, 0, return_fallback_flag=True)`, `semantic_search(query_keywords, where_clauses, params, candidate_window, db_path, 0)` with its positional shape, then chunk, then retrieval), calls `weighted_reciprocal_rank_fusion` with the same four positional arguments and five keyword arguments (`chunk_weight=`, `retrieval_fts_results=`, `retrieval_vector_results=`, `retrieval_fts_weight=`, `retrieval_vector_weight=`: the tests read those keyword names), `_record_retrieval_contributions` with the same arguments, then `_resolve_search_predecessors`, then `_apply_search_pool_policies` called with its UNCHANGED 13 positional arguments filled from the channels and the resolution (7b changes that call). The fusion result is held under the name `fused_map` and `"rrf_score_map"` in the returned dict is `resolution.rrf_map` (the post-substitution map, as today: from the substitution on every reader uses the substituted map). The returned dict keeps exactly the keys and the order `ordered_ids, fts_rows, cross_encoder_scores_map, superseded_ids, exhausted, rrf_score_map, chunk_diagnostics`; `exhausted` keeps its expression.
- `_search_window_pool(conn, filters, controls, diagnostics, offset, limit) -> dict`: 6 parameters (was 8; the duplicate `mode` parameter is dropped and `controls.mode` is used; both are the same normalized value, see claims). `RERANK_CANDIDATE_POOL_SIZE` stays imported LAZILY inside the function from `saltmdb.config`, and `STRICT_OVERFETCH_CANDIDATE_CAP` stays read as an `orchestrator` module global at call time (tests patch both ways and one of the patches is only effective because of this difference).
- `search_memory`: builds `filters = _SearchFilters(where_clauses, params)` once, right before the pool call, and calls `_search_window_pool(conn, filters, controls, diagnostics, offset, limit)`. Every other use of `where_clauses` and `params` in `search_memory` stays as it is (slice 8).

## 3. Decisions

- D1. Behaviour is identical (the one intended reordering that is not observable: `_ChannelRows` is built right after the channel gathers, so the three id-set comprehensions run before `weighted_reciprocal_rank_fusion` and `_record_retrieval_contributions` instead of after them; they are pure and neither later call mutates the row lists, so only which exception surfaces first for malformed rows could differ): the order of the channel calls and of the diagnostics writes, which arguments the primitives receive and how (positional or keyword), the identity of the single `diagnostics` dict (tests assert identity), `diagnostics["chunk_candidate"]` being replaced by a copy, the strict-mode loop (window doubling, the two exit conditions, the cap), `exhausted`, the result dict, and every exception behaviour.
- D2. Module-level names that tests patch stay module attributes read at call time: `_run_fts_search`, `semantic_search`, `chunk_candidate_search`, `_run_retrieval_fts_search`, `retrieval_vector_search`, `weighted_reciprocal_rank_fusion`, `_score_topics_with_fallback` (all through `search_primitives.`), `ranking._resolve_supersession_chains`, `ranking._substitute_resolved_heads`, `ranking._build_candidate_evidence`, and the orchestrator helpers `_rerank_search_pool`, `_gate_search_pool`, `_order_search_pool`, `_record_search_candidates`, `_log_search_gap`, `_resolve_predecessor_evidence`, `_accumulate_resolved_predecessors` (called as bare module globals).
- D3. No `noqa`, no change to any threshold or SQL, no new third-party import. `NamedTuple`, `dataclass` and `field` may need importing: check the file's imports and add only what is missing.

## 4. Tests

Existing pins are heavy (see the baseline). Add, with literals from a run on the unchanged code, only what is missing (the developer lists the existing test per item first):
1. The `_compute_search_pool` result dict: exact key order and the types of its values for one fixed input, including `exhausted` true and false, and with `use_retrieval_text_candidates` true.
2. `diagnostics["chunk_candidate"]` after a chunk-enabled search: the exact key set (the replaced copy returned by the primitive plus `executed`, which lacks the initial `requested` and `chunk_weight` keys). The 'untouched when chunks are disabled' half is already pinned (`T` ~2145 and 2154): cite it.
3. The strict-mode window loop is ALREADY pinned: `tests/test_search_memory_characterization.py` 1268 (`test_strict_overfetch_doubles_window_until_cap`, fts calls `[20, 40]`), 1340 (`[30, 40]`), 1412 (stops at the patched cap), 1467 (initial window clamped), 1727 and 1776 (retrieval exhaustion), 3181 (a full window is not exhausted, `run_fts.call_count == 2`). Cite them; add nothing.
4. `_resolve_search_predecessors` non-strict and empty-map cases return the SAME `rrf_map` object (identity), AND in the non-strict case `ranking._resolve_supersession_chains` is NOT called (patch it and assert `assert_not_called()`; on the fixture database it returns `{}` anyway, so identity alone would not kill a mutant that resolves in non-strict mode).
5. `_ChannelRows` derived sets: `fts_and_ids` is empty exactly when `used_or_fallback` is true; `semantic_ids` equals the first elements of the semantic rows (`semantic_ids` feeds only `_log_search_gap`, which uses it for a debug log, so only this test can kill a wrong-index mutant).

## 5. Tests that call or patch the touched private helpers (call-shape edits ONLY)

`tests/test_search_memory_characterization.py` is the only file. The developer makes these edits and no other change to existing tests: the arguments are rewritten to the new signatures; every assertion stays as it is.
- `_resolve_predecessor_evidence` (~2833 to 2865): 8 positional arguments become `(pre_pool_ids, rrf_map, channels, controls)` built with the test's own values; the assertions on `_score_topics_with_fallback.call_args.args == ("needle", ["a"], "/tmp/search.db")` and `assert_not_called()` when `used_or_fallback` is false stay.
- `_resolve_search_predecessors` (~2892 to 2930, `_search2_filter_pin`): 11 positional arguments become `(conn, controls, filters, channels, rrf_map)`; `result[0] == {"b": 1.0}` and `resolve.call_args.args[2:] == (where_clauses, params)` stay (the NamedTuple keeps index access; the primitive call shape is unchanged).
- `_search2_filter_pin` (~2915) passes `fts_ids={"a"}` with `fts_rows=[]`, which is inconsistent; with derived sets `channels.fts_ids` becomes `set()`. Harmless there (`_resolve_predecessor_evidence` is patched in that test); say so in the report so the change is not read as a semantic edit. In `_search2_predecessor_topic_pin` (~2846 to 2858) the derived `{"a"}` equals the value passed today.
- Patches of `_rerank_search_pool`, `_gate_search_pool`, `_order_search_pool`, `_record_search_candidates`, `_log_search_gap`, `_resolve_predecessor_evidence` (return value only), `_accumulate_resolved_predecessors` in the `_apply_search_pool_policies` and `_resolve_search_predecessors` tests: unchanged (7b may change the former group).
- Everything that calls `search_memory(...)` or patches the primitives: unchanged.

## 6. Out of scope

7b and slice 8; `_SearchControls`; the primitives; ranking; the MCP tool; `scripts/benchmarking/` (they call the primitives, not these helpers).

## 7. Acceptance

1. `./verify` exits 0.
2. The characterization baseline passes before the change (with the new section 4 tests added) and after it, with the section 5 edits as the only edits to existing tests (judge: the architect reads `git diff` of the test file: every removed line is a call-shape line of section 5).
3. `rg -n "def _(gather_chunk_candidates|gather_retrieval_candidates|resolve_search_predecessors|resolve_predecessor_evidence|compute_search_pool|search_window_pool)" -A12 src/.../orchestrator.py` shows no parameter named `where_clauses`, `params`, `fts_rows_`, `semantic_rows_`, `fts_ids_`, `used_or_fallback_`, `sanitized_query`, `chunk_window`, `chunk_oversampling` or `use_chunk_candidates` in those signatures (judge: the architect reads the definitions).
4. `git status --short` lists only the two files of section 0.
5. Mutation check on a scratch copy, killer named for each (set-order mutants under `PYTHONHASHSEED` 0..7): `fts_and_ids` always `fts_ids`; `semantic_ids` from the wrong tuple index; call the retrieval primitives with offset `1`; pass `chunk_weight` positionally; drop `return_fallback_flag=True`; merge the chunk diagnostics into the existing dict instead of replacing; skip the `executed` flag; return a copy instead of the same `rrf_map`; resolve predecessors in non-strict mode (killed only by the `assert_not_called` of item 4); use a hardcoded mode instead of `controls.mode`; double the window without the cap; hoist `RERANK_CANDIDATE_POOL_SIZE` to a module-level import (killed by the `saltmdb.config` patch tests); reorder the retrieval and chunk gathering (the two write different diagnostics keys and share no state: equivalent modulo exceptions unless a shared `Mock` manager records the call order; add that spy test or declare the equivalence in the report). A surviving mutant is reported; it is accepted only with a proof of equivalence.
6. Design read (architect and consultant): at most 6 parameters per function, no tuple of more than 3, one responsibility per function, no controls exploded into locals only to be re-passed. WAIVER for one call: `_compute_search_pool` still passes the channel rows and the resolution to `_apply_search_pool_policies` as its unchanged 13 positional arguments; 7b replaces that call and its signature, together with `_CrossEncoderCaps` and `_PoolOutcome`.

## 8. Claims

| Claim | Where | Verified |
|---|---|---|
| Parameter counts: `_search_window_pool` 8, `_compute_search_pool` 6, `_gather_retrieval_candidates` 9, `_gather_chunk_candidates` 8, `_resolve_search_predecessors` 11, `_resolve_predecessor_evidence` 8 | `orchestrator.py` 988, 707, 196, 236, 427, 378 | read; scout AST map |
| `_compute_search_pool` reads eleven `controls` fields into locals | `orchestrator.py` 726 to 736 | read |
| `mode` passed to `_search_window_pool` equals `controls.mode` | scout map, `search_memory` 1376 and 1503 | read by the scout; the developer re-checks |
| Tests pin the primitives' positional indices and the RRF keyword names | scout map section 7c | read by the scout |
| The `saltmdb.config.STRICT_OVERFETCH_CANDIDATE_CAP` patch at `T` ~2956 is ineffective because the name is imported at module load, while `RERANK_CANDIDATE_POOL_SIZE` is imported lazily and its patch works | scout map section 7b | read by the scout |
| No test references `_compute_search_pool`, `_search_window_pool`, `_gather_*` directly; the only tests that call or patch the touched helpers are in `test_search_memory_characterization.py` (2579, 2621, 2805, 2846/2858, 2907/2910, 2915) | scout map 7e; consultant rg over tests, scripts, hooks, src | probed twice |
| `mode` is normalized once, never reassigned; `controls` is built only on the `elif sanitized_query:` branch and `_search_window_pool` is called only there with the same `mode` | `orchestrator.py` 1386, 1482, 1503 | consultant read |
| `where_clauses` and `params` are appended to in place only before the pool call (`_build_search_filters`, `_append_search_tag_filters` ~1458); the primitives copy them | consultant read | read |
| `scripts/benchmarking/` rebuild pools from the primitives, not from these helpers | scout map section 8 | read by the scout |
