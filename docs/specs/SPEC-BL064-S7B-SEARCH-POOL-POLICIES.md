# SPEC-BL064-S7B-SEARCH-POOL-POLICIES

## 0. Status

LOCKED 2026-10-24

- Backlog item: BL-064, slice 7b of 7a and 7b (the search channel chain in `memory_service/orchestrator.py`). 7b covers the policy half of one pool pass: `_apply_search_pool_policies`, `_rerank_search_pool`, `_run_cross_encoder_scoring`, and the one call that feeds them in `_compute_search_pool`. It follows 7a (`SPEC-BL064-S7A-SEARCH-POOL-GATHER`), whose types it uses (`_ChannelRows`, `_PredecessorResolution`). `search_memory`'s public signature is slice 8. Shared `context_id`: `saltmdb-sonar-fix-2026-10`.
- Baseline: `develop` with the preceding BL-064 slices committed (reference: `develop` at `0a01736` or later; `./verify` exits 0 there with 3657 passed, 10 skipped, 395 subtests passed; the slice's developer reruns `./verify` at the assigned HEAD and records the counts). Characterization baseline (run before and after): the thirteen test files listed in 7a section 0.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Anchors: Line numbers in this spec are those of the tree at which it was written; locate every function by NAME (earlier slices shift the lines).
- Test seams: `memory_service.search_memory(...)` and the private helpers that tests call or patch by name (section 5).
- Scope (may edit): `src/saltmdb/domain/services/memory_service/orchestrator.py`, ONLY: `_apply_search_pool_policies`, `_rerank_search_pool`, `_run_cross_encoder_scoring`, the new types (defined ABOVE `_apply_search_pool_policies`, with 7a's types, because the module has no `from __future__ import annotations`), ONE added read-only property on `_SearchControls` (section 2), local-variable annotations that mypy asks for once the functions are annotated (expected: `scored_ids_in_order = []`, `topic_scores_map_, cross_encoder_scores_map_ = {}, {}`), with no behaviour change, and in `_compute_search_pool` the single call to `_apply_search_pool_policies` and the unpacking of its result; `tests/test_search_memory_characterization.py` (the call-shape edits of section 5 and new characterization tests only).
- Does not touch: `_SearchControls`' 19 fields, their order, their types and the constructor (pinned by `_search2_controls`, `tests/test_search_memory_characterization.py` ~2554 to 2579, which builds it with 19 keywords), the 7a functions, `_order_search_pool`, `_gate_search_pool`, `_record_*`, `_log_search_gap`, `_assemble_search_results`, `search_memory`, `validation.py` (the raw and resolved caps are produced there), `reranker_service`, `ranking.py`, any SQL, any threshold.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`_apply_search_pool_policies` takes thirteen positional parameters: the fused map, six channel values (rows and id sets) that 7a groups into `_ChannelRows`, two predecessor values that 7a groups into `_PredecessorResolution`, the connection, the controls, the window and the diagnostics. It then explodes eleven fields of `controls` into locals and passes six of them on to `_rerank_search_pool` (nine parameters), which passes eight on to `_run_cross_encoder_scoring` (eight parameters). Four of those are the cross-encoder caps, travelling in the same order through three levels: two RESOLVED values (`ce_candidate_cap`, `ce_text_cap`, defaults filled in by validation) and two RAW caller values (`cross_encoder_candidate_cap`, `cross_encoder_text_cap_chars`, `None` when the caller supplied nothing). `_run_cross_encoder_scoring` returns an untyped 3-tuple.

The caps are a 'raw or default' pair: validation (`validation._validate_cross_encoder_controls`, called only from `search_memory` with `enabled=True`) fills a default ONLY for `None`, so whenever the caller supplied a value the resolved cap EQUALS it. The pair therefore carries exactly one value per cap plus a was-supplied flag, and nothing else. The flag matters: the slice of the pool uses the resolved cap unconditionally, while `reranker_service.score_pairs` receives `candidate_cap=`/`text_cap_chars=` keywords ONLY when the caller supplied a value (`tests/test_search_memory_characterization.py` ~286 pins the 'both supplied' call; without supplied caps the reranker applies its own defaults). A refactor that drops the flag changes the `score_pairs` call signature.

Rejected: one value per cap without the flag (changes the `score_pairs` call); four fields with both the raw and the resolved value (it can represent states that cannot occur, such as a raw cap that differs from the resolved one); changing `_SearchControls`; a dict (a bag); moving the `score_pairs` keyword decision out of `_run_cross_encoder_scoring`.

## 2. Target shape

All private, in `orchestrator.py`, defined with 7a's types above `_apply_search_pool_policies`:

```python
@dataclass(frozen=True, slots=True)
class _CrossEncoderCaps:
    """The cross-encoder caps of one search: the resolved values plus whether the caller supplied each."""
    candidate_cap: int        # resolved (the default when the caller supplied none): always used to slice the pool
    text_cap: int             # resolved
    send_candidate_cap: bool  # True iff the caller supplied a candidate cap: only then does score_pairs receive candidate_cap=
    send_text_cap: bool       # True iff the caller supplied a text cap

class _PoolOutcome(NamedTuple):
    ranked_pool: list[str]
    cross_encoder_scores_map: dict[str, float]
    superseded_ids: set[str]

class _CrossEncoderScores(NamedTuple):
    scores: list[float] | None
    scored_ids_in_order: list[str]
    enabled: bool
```

and on the existing `_SearchControls` (fields and constructor unchanged) one read-only property:

```python
    @property
    def cross_encoder_caps(self) -> _CrossEncoderCaps:  # _CrossEncoderCaps(ce_candidate_cap, ce_text_cap, cross_encoder_candidate_cap is not None, cross_encoder_text_cap_chars is not None)
```

(`_SearchControls` is a plain frozen dataclass without slots, so a property is allowed; no code uses `fields()`, `replace` or `asdict` on it, and there is no `__post_init__`. The property builds a new object on each access: read it ONCE per `_apply_search_pool_policies` call.)

Functions after the change (hand count, all positional):

- `_run_cross_encoder_scoring(query_keywords, ranked_pool, conn, db_path, caps: _CrossEncoderCaps) -> _CrossEncoderScores`: 5 parameters (was 8). The body is today's: the lazy `from saltmdb.domain.services import reranker_service`; `reranker_service.is_cross_encoder_enabled()`; the pool is cut to `caps.candidate_cap` BEFORE the fetch; `ranking._build_cross_encoder_candidate_texts(query_keywords, ce_pool_ids, conn, db_path)`; `score_kwargs["candidate_cap"] = caps.candidate_cap` ONLY `if caps.send_candidate_cap`, `score_kwargs["text_cap_chars"] = caps.text_cap` ONLY `if caps.send_text_cap`; `reranker_service.score_pairs(query_keywords, ce_texts, **score_kwargs)` (the texts list is the 2nd positional argument; tests read `call_args.args[1]`).
- `_rerank_search_pool(query_keywords, ranked_pool, conn, db_path, caps, diagnostics) -> tuple[list[str], dict[str, float]]`: 6 parameters (was 9). It keeps returning a PLAIN 2-tuple `(ranked_pool, cross_encoder_scores_map)` because tests patch it with a 2-tuple return value and the caller unpacks two names. Same diagnostics writes: the `execution_count` accumulation across calls on the same dict, `update(ce_diagnostics)`, `executed`, `reason = "cross_encoder_disabled"`, and no reset of the dict.
- `_apply_search_pool_policies(conn, controls, channels: _ChannelRows, resolution: _PredecessorResolution, candidate_window, diagnostics) -> _PoolOutcome`: 6 parameters (was 13). `rrf_map` is `resolution.rrf_map`; the channel rows and ids come from `channels`; `resolved_from_` and `predecessor_grounded_map` from `resolution`. It reads `controls.mode`, `.query_keywords`, `.db_path`, `.cross_encoder_caps`, `.prefer_durable_types`, `.demote_superseded`, `.collapse_supersedes_families`, `.return_diagnostics` where it needs them instead of exploding them into locals that are only re-passed. Every call it makes keeps its argument shape: `_log_search_gap(fts_rows, semantic_rows, candidate_window, fts_ids, semantic_ids)`; `search_primitives._score_topics_with_fallback(query_keywords, ungrounded_ids, db_path)` (3 positional); `_rerank_search_pool(...)` (new shape); `ranking._build_candidate_evidence(ranked_pool, rrf_map, fts_rows, semantic_rows, topic_scores_map, resolved_from, predecessor_grounded_map, cross_encoder_scores_map, used_or_fallback=...)`; `_gate_search_pool(mode, ranked_pool, evidence_map, conn)` (4 positional, returns a 2-tuple); `_order_search_pool(ranked_pool, conn, prefer_durable_types, demote_superseded, mode, collapse_supersedes_families, diagnostics)`; `_record_search_candidates(ranked_pool, pool_order, evidence_map, diagnostics, return_diagnostics)` (tests pin `args[:2]` and `args[4] is True`). The empty-pool branch (`diagnostics["cross_encoder"]["reason"] = "no_candidates"`, empty pool, empty set) is unchanged. `_PoolOutcome` is a NamedTuple so that `result[:2]` and tuple unpacking keep working.
- `_compute_search_pool`: the fusion result is held under the name `fused_map` and the post-resolution map is `resolution.rrf_map`; the result dict's `"rrf_score_map"` is `resolution.rrf_map` (the SUBSTITUTED map, as today), so a pre-substitution map is never one token away. `_rerank_search_pool` keeps receiving a fresh `list(rrf_map.keys())`. Its call to `_apply_search_pool_policies` becomes `_apply_search_pool_policies(conn, controls, channels, resolution, candidate_window, diagnostics)` and it reads `ranked_pool`, `cross_encoder_scores_map`, `superseded_ids` from the returned `_PoolOutcome`; the result dict keys and order are unchanged (7a).

## 3. Decisions

- D1. Behaviour is identical: the order of the stages (gap log, predecessor-grounding topic scoring in strict mode, rerank, evidence, gate, order, record), every argument each stage receives, the `score_pairs` keyword rule above, the pool cut before the text fetch, the diagnostics writes and their accumulation, the empty-pool branch, and every exception behaviour.
- D2. Module-level seams stay module attributes read at call time, as in 7a D2: `_rerank_search_pool`, `_gate_search_pool`, `_order_search_pool`, `_record_search_candidates`, `_log_search_gap` (called as bare module globals), `ranking._build_candidate_evidence`, `ranking._build_cross_encoder_candidate_texts`, `search_primitives._score_topics_with_fallback`, and `reranker_service.is_cross_encoder_enabled`, `.score_pairs`, `.get_last_score_diagnostics` (imported lazily inside the functions that use them: keep the lazy `from saltmdb.domain.services import reranker_service`).
- D3. No `noqa`, no new third-party import; `NamedTuple` and `dataclass` may need importing (7a may already have added them: check).

## 4. Tests

Existing pins are listed in section 5. Add, with literals from a run on the unchanged code, only what is missing (the developer names the existing test per item first):
1. `score_pairs` keyword rule, all four combinations of supplied and omitted caps, observed through `search_memory` with `reranker_service.score_pairs` patched (module attribute): both omitted -> called with NO keyword arguments; only the candidate cap supplied -> exactly `{"candidate_cap": <that value>}`; only the text cap supplied -> exactly `{"text_cap_chars": <that value>}` (a supplied value equals the resolved one); both -> both (pinned at `T` ~286: cite it). The only existing keyword assertion across `tests/` is `T` ~286; `T` ~1648 and ~2318 read `args[1]` only.
2. The pool cut: with a resolved candidate cap of 10 and a 15-id pool, `ranking._build_cross_encoder_candidate_texts` receives exactly the first 10 ids (patched, assert `call_args.args[1]`), even when the raw cap is `None`.
3. Already pinned, cite instead of adding: the `_PoolOutcome` slice and three-name unpack (`T` ~2636 `result[:2]`; every search unpacks it), the empty-pool reason (`T` ~573, `test_search_accuracy_stage2.py` ~481 to 500), the disable reason (`T` ~572, stage2 ~390 and 406).
4. The cross-encoder diagnostics are UPDATED, not replaced: a scored search with `return_diagnostics=True` where `reranker_service.get_last_score_diagnostics` returns `{"model": "test"}` ends with `diagnostics["cross_encoder"]` still carrying `requested`, `candidate_cap`, `text_cap_chars` and `execution_count`, plus `model` (the helper pin at `T` ~2987 starts from an empty dict, so it cannot tell replace from update).
5. The substituted fusion map reaches the policies and the result: a strict search where `ranking._substitute_resolved_heads` (patched) changes the map: the policies see the substituted ids and the result item's `score` comes from the substituted map (`rrf_score_map`). Kills a mutant that passes or returns the pre-substitution map.
6. `_CrossEncoderCaps` from `_SearchControls`: `controls.cross_encoder_caps` is `(resolved candidate, resolved text, supplied?, supplied?)` for a controls built with raw `None` and with raw values.

## 5. Tests that call or patch the touched private helpers (call-shape edits ONLY)

`tests/test_search_memory_characterization.py` is the only file. The developer rewrites the arguments of these calls to the new signatures and changes no assertion:
- `_apply_search_pool_policies` (~2589 to 2650, `test_search2_policy_gate_and_candidate_snapshot_helper_pin`, guarded by `skipif(not hasattr(...))`): 13 positional arguments become `(conn, controls, channels, resolution, candidate_window, diagnostics)` built from the test's own values; `result[:2] == (["a", "b"], {"b": 0.9, "a": 0.1})` stays; the patches of `_log_search_gap`, `_rerank_search_pool` (return value is a 2-tuple), `_gate_search_pool` (a 4-positional `side_effect`), `_order_search_pool`, `_record_search_candidates` and `ranking._build_candidate_evidence` stay as they are.
- `_rerank_search_pool` (~2987 to 3030): the call `("needle", ["a"], conn, path, 10, 100, None, None, diagnostics)` becomes `("needle", ["a"], conn, path, _CrossEncoderCaps(10, 100, False, False), diagnostics)`; the assertions (`execution_count == 2` after two calls on the same dict; the failed run leaves `diagnostics["cross_encoder"] == {"failure": "runner_failed"}`) and the patches of `reranker_service.is_cross_encoder_enabled`, `.score_pairs`, `.get_last_score_diagnostics` and `ranking._build_cross_encoder_candidate_texts` stay.
- Everything that calls `search_memory(...)`, patches the primitives or builds `_SearchControls`: unchanged.

## 6. Out of scope

Slice 8 (`search_memory`'s signature); `_SearchControls`' fields; validation; the reranker; the order and gate stages.

## 7. Acceptance

1. `./verify` exits 0.
2. The characterization baseline passes before the change (with the new section 4 tests added) and after it, with the section 5 edits as the only edits to existing tests (judge: the architect reads `git diff` of the test file: every removed line is a call-shape line of section 5).
3. `rg -n "def _(apply_search_pool_policies|rerank_search_pool|run_cross_encoder_scoring)" -A12 src/.../orchestrator.py` shows no parameter named `rrf_map`, `fts_rows_`, `semantic_rows_`, `fts_ids_`, `fts_and_ids_`, `semantic_ids_`, `used_or_fallback_`, `resolved_from_`, `predecessor_grounded_map`, `ce_candidate_cap`, `ce_text_cap`, `cross_encoder_candidate_cap` or `cross_encoder_text_cap_chars` in those signatures (judge: the architect reads the definitions).
4. `git status --short` lists only the two files of section 0.
5. Mutation check on a scratch copy, killer named for each: send `candidate_cap` to `score_pairs` even when the caller supplied none; swap the candidate and text caps (killed by `T` ~286 for the keywords and item 1); slice the pool by the raw cap; slice AFTER the text fetch; reset the `execution_count` on each call; replace instead of `update` the cross-encoder diagnostics (killed only by item 4); pass or return the pre-substitution fusion map (killed only by item 5); skip the `reason` write on disable; change the empty-pool reason; return a 3-tuple from `_rerank_search_pool`; call `_gate_search_pool` before the rerank; pass `pool_order` and `ranked_pool` swapped to `_record_search_candidates`. EQUIVALENT, do not list as killable: sending the raw instead of the resolved value (they are equal whenever a raw value exists) and hoisting the lazy `reranker_service` import (tests patch with `patch.object` on the module object, which a module-level binding shares, and `reranker_service` imports only stdlib and `saltmdb.utils.process_memory`); keep the lazy import as a convention (D2). A surviving mutant is reported; accepted only with a proof of equivalence.
6. Design read (architect and consultant): at most 6 parameters per function, no tuple of more than 3, no controls exploded into locals only to be re-passed, the caps in one named type with no value merged.

## 8. Claims

| Claim | Where | Verified |
|---|---|---|
| Parameter counts: `_apply_search_pool_policies` 13, `_rerank_search_pool` 9, `_run_cross_encoder_scoring` 8 | `orchestrator.py` 40, 509, 467 (at `aa4fd75`; 7a moves the lines) | read; scout AST map |
| The raw caps are read only by `is not None` in `_run_cross_encoder_scoring` (lines 501, 503); threading sites 60 to 61, 93 to 94, 516 to 517, 534 to 535; `dispatch.py` 329 to 360 forwards them; the resolved caps feed the slice (493), the kwargs (502, 504) and the initial diagnostics (1426 to 1427) | `orchestrator.py` | consultant rg |
| Whenever a raw cap is not `None`, the resolved cap equals it (`validation.py` 156 to 157 substitutes defaults only for `None`; 174 returns the raw value; the not-enabled branch at 161 is unreachable because the only caller passes `enabled=True`) | `validation.py` 145 to 174; `orchestrator.py` 1392 | consultant read |
| From line ~796 `rrf_map` is the SUBSTITUTED map; every read in the policies and the returned `rrf_score_map` use it; `_log_search_gap` does not read it | `orchestrator.py` | consultant read |
| The resolved caps come from `validation._validate_cross_encoder_controls` (defaults filled; options 10/15/20 and 1000/2000) | `validation.py` 145 to 172 | read by the scout |
| `_SearchControls` is a plain `@dataclass(frozen=True)` without slots | `orchestrator.py` 563 | read |
| Tests call `_apply_search_pool_policies` with 13 positional arguments and index `result[:2]`; call `_rerank_search_pool` with 9; patch `_rerank_search_pool` with a 2-tuple | `T` ~2589 to 2650, 2987 to 3030, 2605 | read by the scout; consultant to re-verify |
| No test asserts that `score_pairs` is called with NO keyword arguments | scout; `rg` | to be re-verified by the developer before writing item 1 |

## Amendment 1 (2026-10-24): order and test style when S8 lands first

The consultant's read found S8 independent of 7a and 7b (see SPEC-BL064-S8 amendment 1), so S8 may land before 7b.

A1.1 Every `T ~nnn` / `tests/...` line citation in sections 4, 5 and 8 is a pre-S8 line number; if S8 landed first they have drifted. Locate each test by its NAME.

A1.2 If S8 has landed, a new 7b test that calls `search_memory` directly passes the cap names inside `tuning=SearchTuning(cross_encoder_candidate_cap=..., cross_encoder_text_cap_chars=...)`; a new test written through the `_search_case` wrapper keeps the flat option keys (the wrapper translates them, S8 amendment 1.2). Behaviour, signatures and acceptance of 7b are unchanged.
