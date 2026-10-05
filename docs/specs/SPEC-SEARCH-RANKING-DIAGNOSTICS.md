# SPEC: Per-candidate ranking diagnostics for `search_memory`

## 0. Status

**LOCKED** (2026-10-03, reviewed by the architect).

- **Location / branch:** main checkout `<repo>`, branch `develop`
  (baseline `693c0bf`). One active writer: the developer, until handoff.
- **Shared `context_id`:** `saltmdb-search-ranking-diagnostics-2026-10-03`.
- **Scope (files that may be edited):**
  1. `src/saltmdb/config.py`
  2. `src/saltmdb/domain/services/memory_service/orchestrator.py`
  3. `src/saltmdb/mcp/tools.py`
  4. `tests/test_search_accuracy_stage2.py`
  5. `tests/test_mcp_tools.py`
- **Does not touch:** `src/saltmdb/domain/services/memory_service/ranking.py`
  (`_build_candidate_evidence` is reused unchanged), `daemon/dispatch.py`, `daemon/protocol.py`,
  `hooks/`, `src/saltmdb/viewer/`, `skills/`, `docs/architecture.md`, `uv.lock`, `pyproject.toml`.
- **Public test seams:** `memory_service.search_memory(..., return_diagnostics=True,
  db_connection=..., db_path=...)` for ranking behaviour (fixture `_DbFixture` in
  `tests/test_search_accuracy_stage2.py`); `await tools.mcp.call_tool("search_memory", {...})` and
  `await tools.mcp.list_tools()` for the wire contract (pattern at
  `tests/test_mcp_tools.py` around line 1779).

## 1. Why

When a retrieval benchmark misses, the only visible fact is that the wrong memory ranked first.
The ranking pipeline has several stages (FTS rank, vector rank, weighted RRF, optional
cross-encoder, supersession/type adjustments, collapse), and today nothing says which stage put a
given candidate where it is.

Most of the needed data already exists in the engine:

- `ranking._build_candidate_evidence` (`ranking.py:603`) already builds a per-candidate record with
  `rrf_score`, `fts_rank`, `fts_bm25`, `semantic_rank`, `semantic_distance`, `dual_channel`,
  `cross_encoder_score` and more. It is only called when `mode="strict"` (`orchestrator.py:666`) and
  only feeds the relevance gate; it is never put into `diagnostics`.
- `search_memory` already accepts `return_diagnostics` and returns
  `{"results": [...], "diagnostics": {...}}` (`orchestrator.py:1017-1018`), and
  `daemon/dispatch.py:297` already forwards it. But `diagnostics` holds only aggregate counters and
  cross-encoder status, and the MCP tool (`mcp/tools.py:630`) has no such parameter, so agents
  cannot reach any of it.

This spec attaches the already-built per-candidate record to `diagnostics` (all modes, opt-in only)
and exposes `return_diagnostics` on the MCP tool. It changes **no ranking behaviour** and, with the
flag off, changes no response byte. It is Phase A of the retrieval-observability plan: without it,
overlap/confuser failures cannot be classified by stage.

`explain_mode` is a different, pre-retrieval feature (term/tag existence checks, returns before any
ranking) and is deliberately neither reused nor exposed here; the flag keeps the engine's existing
name `return_diagnostics` so the two are not confused.

## 2. `src/saltmdb/config.py`

Immediately after the `RERANK_BROAD_THEME_THRESHOLD = (...)` assignment (currently
`config.py:159-161`), add:

```python
# Upper bound on per-candidate records search_memory attaches to diagnostics["candidates"] when
# return_diagnostics=True. Bounds response size for strict mode, whose overfetch pool can reach
# STRICT_OVERFETCH_CANDIDATE_CAP (200); ordinary broad/history pools are RERANK_CANDIDATE_POOL_SIZE.
SEARCH_DIAGNOSTICS_MAX_CANDIDATES = 50
```

## 3. `src/saltmdb/domain/services/memory_service/orchestrator.py`

Line numbers are for baseline `693c0bf`.

### 3.1 Import

Add `SEARCH_DIAGNOSTICS_MAX_CANDIDATES` to the existing `from saltmdb.config import (...)` block
(lines 19-24), keeping its current style.

### 3.2 `diagnostics` initialiser (lines 156-185)

Add two keys after the `"cross_encoder"` entry, so they exist (empty) on every path including
errors and `explain_mode`:

```python
        "candidates": [],
        "candidate_pool_size": 0,
```

### 3.3 Build the evidence record in every mode when asked (lines 664-688)

Currently the block reads `superseded_ids_ = set()` then
`if mode == "strict": evidence_map = ranking._build_candidate_evidence(...)` followed by the gate
loop, then `elif mode == "history": ...`.

Change it as follows, **keeping the `_build_candidate_evidence` argument list byte-identical to
today's strict call** (`ranked_pool_, rrf_map, fts_rows_, semantic_rows_, topic_scores_map_,
resolved_from_, predecessor_grounded_map, cross_encoder_scores_map_, used_or_fallback=
used_or_fallback_`):

1. Before the existing `if mode == "strict":` statement, add

   ```python
   pool_order = list(ranked_pool_)
   evidence_map = None
   if mode == "strict" or return_diagnostics:
       evidence_map = ranking._build_candidate_evidence(<identical args>)
   ```

2. Inside the existing `if mode == "strict":` branch, **delete** the inline
   `evidence_map = ranking._build_candidate_evidence(...)` assignment (now done above); the gate
   loop and `ranked_pool_ = accepted_pool` stay exactly as they are and keep reading `evidence_map`.
3. The `elif mode == "history":` branch is unchanged.

`pool_order` is the pool as it stands after the cross-encoder stage and before the strict gate,
type bias, supersession demotion, strict defaults and family collapse. In non-strict modes
`topic_scores_map_`, `resolved_from_` and `predecessor_grounded_map` are empty, which
`_build_candidate_evidence` already accepts (it defaults them), so the same call is valid there.

With `return_diagnostics=False` and a non-strict mode, `evidence_map` stays `None` and nothing new
is computed.

### 3.4 Record candidates after all ordering adjustments

Still inside the `if rrf_map:` block, immediately after the
`elif mode == "broad" and collapse_supersedes_families:` branch (which ends at line ~725) and before
the `else: ranked_pool_ = []` that closes the block, add (at the indentation of that
`if/elif` chain):

```python
if return_diagnostics:
    final_rank = {eid: i for i, eid in enumerate(ranked_pool_)}
    diagnostics["candidate_pool_size"] = len(pool_order)
    diagnostics["candidates"] = [
        {**evidence_map[eid], "pool_rank": i, "final_rank": final_rank.get(eid)}
        for i, eid in enumerate(pool_order[:SEARCH_DIAGNOSTICS_MAX_CANDIDATES])
    ]
```

(`evidence_map` is not `None` here by construction of §3.3; add whatever assertion or narrowing
`mypy src` needs, without changing behaviour.)

### 3.5 Record contract (exact keys)

Each element of `diagnostics["candidates"]` has exactly these keys, in this order of meaning:

- every key `_build_candidate_evidence` returns today: `entity_id`, `provenance`, `rrf_score`,
  `in_fts`, `in_fts_and`, `in_fts_or_only`, `fts_rank`, `fts_bm25`, `in_semantic`, `semantic_rank`,
  `semantic_distance`, `dual_channel`, `topic_score`, `semantic_verdict`, `is_resolved_head`,
  `predecessor_grounded`, `cross_encoder_score`;
- plus `pool_rank` (int): 0-based position in `pool_order` (post-cross-encoder, pre-adjustment
  order);
- plus `final_rank` (int or `None`): 0-based position in the final ordered list **before** the
  `offset:offset+limit` page slice, so it is absolute, not page-relative. `None` means the candidate
  was removed after `pool_order` (strict gate rejection or family collapse).

`diagnostics["candidates"]` is ordered by `pool_rank`. `diagnostics["candidate_pool_size"]` is
`len(pool_order)` before capping, so truncation is detectable
(`candidate_pool_size > len(candidates)`).

Rules:

- **Ids and numbers only.** No title, content, snippet or retrieval text may appear in a record.
- **Last pass wins.** Strict mode re-runs `_compute_pool` with a doubled window; each pass
  overwrites `candidates` and `candidate_pool_size`, so the returned values describe the final pass.
- **Rank fields are 0-based** (`fts_rank`, `semantic_rank`, `pool_rank`, `final_rank`), matching
  how `_build_candidate_evidence` already numbers them.
- **Known limitation, must be documented in the tool description (§4.3):** when
  `use_chunk_candidates` or `use_retrieval_text_candidates` is on, `rrf_score` includes channels not
  represented by `fts_rank` / `semantic_rank`, so `rrf_score` is not reproducible from those two
  ranks alone.
- All values must be JSON-serialisable (`json.dumps(result)` must succeed).

## 4. `src/saltmdb/mcp/tools.py`

### 4.1 Signature

Add `return_diagnostics: bool | None = None` as the **last** parameter of `def search_memory(`
(currently ends at `mode: Literal[...] | None = None,` at line 641). Change the return annotation
from `-> list:` to `-> Any:` (`Any` is already imported on line 1).

The annotation change is deliberate and load-bearing: probed against the pinned `mcp==1.29.0`,
`-> list | dict` makes the server advertise an `outputSchema` and wrap every response in
`structuredContent: {"result": ...}`, changing the default wire format for all callers.
`-> Any` leaves `outputSchema` absent and the default response byte-identical, and returns the
flagged envelope as one JSON text block. Do not use `list | dict`.

### 4.2 Forwarding

Add to the dict passed to `_backend_or_raise().call("search_memory", {...})`:

```python
"return_diagnostics": return_diagnostics if return_diagnostics is not None else False,
```

No change to `daemon/dispatch.py` is needed: `_dispatch_search_memory` already reads and validates
the key (`dispatch.py:297`), and `RpcBackend.call` forwards all keys other than `agent_id`.

### 4.3 Tool description

Extend the string passed to the `@mcp.tool(...)` decorator directly above `def search_memory` (it
ends at line 628) with a short paragraph stating, in the file's existing voice:

- `return_diagnostics=True` changes the return shape from a list of results to
  `{"results": [...], "diagnostics": {...}}`; the default (omitted/False) is unchanged;
- `diagnostics["candidates"]` carries the per-candidate record of §3.5 (name the keys by reference to
  the field names, do not restate semantics that contradict §3.5), capped at 50 with
  `candidate_pool_size` showing the uncapped count;
- ids and numbers only, never memory text; use `get_memory` for content;
- `final_rank` is absolute, `None` means dropped after the pool was ranked;
- the §3.5 `rrf_score` limitation for chunk / retrieval-text channels;
- it is for investigating ranking, not for ordinary retrieval.

## 5. Tests

### 5.1 `tests/test_search_accuracy_stage2.py`

Add `class TestSearchRankingDiagnostics(_DbFixture)` (patch model inference the same way the
neighbouring `TestSupersedesFamilyCollapse` / `TestCrossEncoderControls` do). Cases:

1. **Shape and order (broad).** Store several memories, call `search_memory(...,
   return_diagnostics=True)`. Assert the result is a dict with `results` and `diagnostics`; every
   `candidates` element has exactly the §3.5 key set; `pool_rank == index`; the ids with non-`None`
   `final_rank`, sorted by `final_rank`, equal the ids in `results` when `limit` covers them all;
   `json.dumps(result)` succeeds.
2. **Flag off is inert.** Same query without the flag returns a list, and
   `get_last_search_diagnostics()["candidates"] == []` and `["candidate_pool_size"] == 0`.
3. **Cap.** Patch `orchestrator.SEARCH_DIAGNOSTICS_MAX_CANDIDATES` to `2` with more than two
   candidates: `len(candidates) == 2` and `candidate_pool_size > 2`.
4. **No memory text.** Store a memory containing a distinctive phrase; the serialised diagnostics
   must not contain it (title or content).
5. **Dropped candidate.** Using the supersedes-family fixture, run broad mode with
   `collapse_supersedes_families=True` and the flag: the family head has an int `final_rank`, the
   collapsed member has `final_rank is None`, and both appear in `candidates`.
6. **Cross-encoder score and order.** Using the fixture that patches `score_pairs`, assert each
   scored candidate's `cross_encoder_score` equals the patched value and the candidates' `pool_rank`
   order follows descending score.
7. **Strict mode still gates.** Existing strict-mode tests must pass unchanged (§6); additionally
   assert that a strict call with the flag returns `candidates` whose ids include every id in
   `results`.

### 5.2 `tests/test_mcp_tools.py`

1. **Flagged call through the real tool.** `await tools.mcp.call_tool("search_memory",
   {"query_keywords": <stored memory's distinctive word>, "return_diagnostics": True})` returns one
   `TextContent` whose JSON has `results` (containing the stored memory's id) and `diagnostics`
   with a non-empty `candidates`.
2. **Wire contract unchanged by default.** `[t for t in await tools.mcp.list_tools() if t.name ==
   "search_memory"][0].outputSchema is None`, and an unflagged call still returns the existing shape
   asserted by `test_search_memory_accepts_query_keywords_argument`.
3. **Forwarding.** Using the file's existing capture/patch pattern, assert `return_diagnostics` is
   forwarded as `True` when passed and as `False` when omitted.

## 6. Out of scope

- Any ranking change: no tie-break, boost, demotion or reordering; evidence is recorded, never fed
  back. (Later experiment phases are separate specs.)
- Chunk-channel rank and retrieval-text-channel rank inside the per-candidate record, and
  per-channel RRF contribution values. Rank plus `rrf_score` plus the documented `k=60` suffice for
  this phase; revisit only if a failure analysis needs them.
- Titles, snippets or any memory text in diagnostics.
- Exposing or changing `explain_mode`; changing `diagnostics` keys that exist today.
- `daemon/dispatch.py`, `daemon/protocol.py`, `ranking.py`, hooks, viewer, skills,
  `docs/architecture.md`.
- Building a frozen-snapshot replay harness or a failure-taxonomy report (separate spec).
- Behaviour of the post-tool hook (`hooks/saltmdb-post-tool-response-nudges.py`): its
  empty-result pattern matches `"result": []`; the envelope's key is `"results"`, so no change is
  required or allowed here.

## 7. Acceptance

Baseline already run at lock time on `693c0bf`:
`PYTHONPATH=src .venv/bin/python -m pytest tests/test_search_accuracy_stage2.py
tests/test_retrieval_text.py tests/test_mcp_tools.py tests/test_relevance_gate.py -q` ->
**180 passed, 24 subtests passed**. Pre-lock probe on `mcp==1.29.0`: `-> list` and `-> Any` leave
`outputSchema` absent and return identical default content; `-> list | dict` adds an `outputSchema`
and `structuredContent`.

After implementation (all require the new code; run from the main checkout on `develop`):

1. `./verify` (ruff check, ruff format --check, mypy src, bandit, pip-audit, deptry, full pytest).
   If `pip-audit` cannot reach the network, report that and run the other steps individually.
2. `PYTHONPATH=src .venv/bin/python -m pytest tests/test_search_accuracy_stage2.py
   tests/test_retrieval_text.py tests/test_mcp_tools.py tests/test_relevance_gate.py
   tests/test_strict_ranking_defaults.py tests/test_search_scores.py -q` -> no failures, and the
   baseline's 180 tests all still present (new tests only add).
3. `git diff --name-only` lists exactly the five scope files of §0 and nothing else.
4. `rg -n "return_diagnostics" src/saltmdb/mcp/tools.py` shows the parameter, the forwarding line
   and the description text.
5. `rg -n "_build_candidate_evidence" src/saltmdb/domain/services/memory_service/orchestrator.py`
   shows exactly two call sites: the existing `pre_evidence` strict-resolution call (unchanged) and
   the single consolidated call of §3.3, with no second copy of that call's argument list.

## 8. Pre-lock gate record

Executed: baseline pytest (180 passed); FastMCP annotation probes (two scripts, scratch only);
reads of `orchestrator.py` (init, evidence, cross-encoder, adjustment, slice and error paths),
`ranking.py:603-692`, `dispatch.py:281-332`, `tools.py:160-262,630-661`, `protocol.py`
(READ_TOOLS only), the post-tool hook, existing diagnostics tests. Cross-checks: scope list vs
acceptance greps (step 3, 5); no lockfile or generated output involved (step 4); no string or
constant content changed (step 5); the record key list is stated once (§3.5) and referenced by
§4.3 and §5.1 (step 7); worked instances walked against the other requirements: strict re-pass
(last pass wins), empty `rrf_map` (candidates stay `[]`), `offset > 0` (final_rank absolute),
`explain_mode` (returns earlier, untouched) (step 8); runtime/library behaviour confirmed by probe
(step 9/11); no rename of any symbol (step 10). Deferred to implementation: acceptance commands
1-5 above.
