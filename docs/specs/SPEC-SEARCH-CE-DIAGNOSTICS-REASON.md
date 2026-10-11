# SPEC-SEARCH-CE-DIAGNOSTICS-REASON

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-026. Shared `context_id`: `search-ce-diagnostics-2026-10-11`.
- Baseline: `edcc5d5` on `develop`; `./verify` exit 0, 2187 passed, 1 skipped, 172 subtests.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `search_memory` through the orchestrator with `return_diagnostics=True`, as
  `tests/test_search_accuracy_stage2.py` does (class `TestSearchRankingDiagnostics`, L250-295, and
  the test at L350-370).
- Scope (may edit):
  - `src/saltmdb/domain/services/memory_service/orchestrator.py` (L176-185, L595-662, L746-748)
  - `tests/test_search_accuracy_stage2.py` (new tests, plus the one added assertion in section 4 item 5)
- Does not touch: `reranker_service.py`, `scripts/benchmarking/run_evaluation_matrix.py`, the MCP
  tool descriptions, the `candidates[]` diagnostics.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

With `SALTMDB_RERANKER_MODEL` unset, `reranker_service.is_cross_encoder_enabled()` is False and the
orchestrator skips the cross-encoder. The `cross_encoder` diagnostics then read `forced: true`,
`executed: false`, `reason: null`. That contradicts itself, and the reader cannot tell why it did not
run.

On the same path the orchestrator merges `reranker_service.get_last_score_diagnostics()` (L654-662),
which returns the calling thread's last `score_pairs` result. In the daemon this merge is a no-op
today: every RPC is served on a fresh thread, so the thread-local is empty. It is real for in-process
callers that reuse a thread: unit tests (all tests share one thread), benchmark scripts that call
`score_pairs_preflight` and then search, and related-memory scoring, which calls `score_pairs` with an
explicit model name even when `SALTMDB_RERANKER_MODEL` is unset. A stale dict can even report
`executed: true` with `execution_count: 0`.

Rejected: changing `reranker_service` (its thread-local diagnostics are correct on the paths where
`score_pairs` did run); keeping `forced` as a renamed key (no reader exists: `rg` over `src`, `tests`,
`scripts`, `hooks`, `skills` and `docs` finds no reader of `diagnostics["cross_encoder"]["forced"]`).

Not addressed here: `dual_channel` was false on every candidate in one owner session (BL-026 notes the
cause was not checked). That needs its own investigation.

## 2. Decisions

- D1. The `forced` key is removed from `diagnostics["cross_encoder"]`.
- D2. When the cross-encoder is not enabled (`is_cross_encoder_enabled()` is False) and the pool is
  non-empty, `diagnostics["cross_encoder"]["reason"]` is exactly `"cross_encoder_disabled"`,
  `executed` stays `False`, and the thread-local diagnostics are not merged.
- D3. When the fused pool is empty (the existing `else:` of `if rrf_map:`, L746-748), `reason` is
  exactly `"no_candidates"` and nothing is merged.
- D4. When the cross-encoder is enabled and `score_pairs` returned `None`, the existing merge of
  `get_last_score_diagnostics()` stays, because that call just ran on this thread and carries a fresh
  `reason`.
- D5. On an executed run `reason` is whatever `score_pairs` recorded (`"ok"`); this is unchanged.

## 3. Changes per file

### 3.1 `orchestrator.py`

- L176-185: delete the `"forced": True,` line from the `cross_encoder` dict.
- L595-662, the existing `if ranked_pool_:` block (it sits inside `if rrf_map:` and `ranked_pool_` is
  never empty there, so do not add an `else` to it): when
  `reranker_service.is_cross_encoder_enabled()` is False, set
  `diagnostics["cross_encoder"]["reason"] = "cross_encoder_disabled"` and do not call
  `get_last_score_diagnostics()`. When it is True and `ce_scores is None`, keep the existing merge
  (D4). The executed branch is unchanged.
- L746-748, the existing `else:` of `if rrf_map:` that sets `ranked_pool_ = []`: add
  `diagnostics["cross_encoder"]["reason"] = "no_candidates"`.
- Do not change the comments about the gap gate. In `mode="strict"` the pool computation reruns in the
  overfetch loop and rewrites the diagnostics on each pass; the final pass wins. Leave that as is.

## 4. Tests

Written failing first, in `tests/test_search_accuracy_stage2.py`, modelled on the tests at L250-295
and L350-370 (they call `search_memory` synchronously on the test thread with
`is_cross_encoder_enabled` patched):

1. With `is_cross_encoder_enabled` patched to False: `diagnostics["cross_encoder"]` has
   `executed is False`, `reason == "cross_encoder_disabled"`, and no `forced` key.
2. Same patch, after priming the thread-local with
   `reranker_service._set_score_diagnostics({"executed": True, "reason": "runner_failure"})` and
   `self.addCleanup(reranker_service._set_score_diagnostics, {})`: the result still shows
   `executed is False` and `"cross_encoder_disabled"`.
3. Cross-encoder enabled, with `score_pairs` patched to set the thread-local `reason` to
   `"malformed_output"` and return `None`: the diagnostics carry `"malformed_output"` (D4).
4. A search whose fused pool is empty, using a fixture database with no entities (the semantic channel
   is a KNN with no distance cutoff, so any embedded entity yields rows): `reason == "no_candidates"`
   and no `forced` key.
5. The existing executed-run test (L350): add an assertion that there is no `forced` key; do not
   assert `reason` (it is `None` there only because that test patches
   `get_last_score_diagnostics` to `{}`).

## 5. Documentation

None. `rg -n forced src/saltmdb/mcp` at the baseline matches only the word "enforced" in
`mcp/tools.py` L355, so no tool description mentions the removed key.

## 6. Verification procedure

`./verify` alone on the host.

## 7. Out of scope

`dual_channel`; the `candidates[]` diagnostics; `reranker_service`; the evaluation-matrix script.
Paths on which `reason` stays `None` with `executed: false` and which this spec does not change: the
FTS-only path when semantic search is disabled or `disable_semantic=True` (the pool computation is
skipped), the `explain` early return (L324-335), and the exception path (L1041-1046). A reader who
needs those reasons opens a new backlog item.

## 8. Acceptance

1. `./verify` exits 0 with the new tests included.
2. `rg -n '"forced"' src/saltmdb/domain` prints nothing.
3. `git diff --stat` lists only the two files in section 0 scope.

## 9. Pre-lock gate notes

- Consultant review (m_1286): F1 (D3 attached to dead code) and F2 (test 4 fixture) fixed; F3 section 1
  corrected (no-op in the daemon, real for in-process callers); F4 D5 rewritten; F5 uncovered
  paths moved to section 7; F6 test 2 primes `executed` too and cleans up.
- Verified by me this turn: orchestrator L746-748 is the `else:` of `if rrf_map:`; L324-335 is the
  explain early return; L384 gates the pool computation; L1041-1046 is the exception path.
- Section 5 grep done. `reranker_service._set_score_diagnostics` exists (L154).
- Re-read at lock: D2-D5 against section 3.1 (D3 matches the L746-748 edit; D2 and D4 the L595-662
  block; D5 states unchanged behaviour). The old wording "else on `if ranked_pool_`" no longer
  appears (`rg "if ranked_pool_"` on this spec shows only the section 3.1 warning not to add one).
- No second consultant pass: the only reviewer finding class was fixed and the consultant waived a
  re-review (m_1284).

## Amendment 1 (2026-10-11)

Resolves a contradiction between section 0 ("`tests/test_search_accuracy_stage2.py` (new tests only)")
and section 4 item 5 (add one assertion to the existing executed-run test at L350). Section 4 item 5
governs: the file's scope is new tests plus exactly that one added `assertNotIn("forced", ...)`
assertion in `test_cross_encoder_scores_are_recorded_in_pool_order`. No existing assertion in that or
any other test is changed or removed. Re-read after the change: section 0 scope now reads "new tests
and the one added assertion in section 4 item 5"; no other section mentions the "new tests only"
wording (`rg "new tests only"` on this spec shows only this amendment).
