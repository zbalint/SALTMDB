# SPEC-RELEVANCE-PREVIEW: Query-Focused Extractive Preview on search_memory Results

## 0. Status

**LOCKED.**

**Scope — files that may be edited:**
- `src/saltmdb/config.py` (add constants only)
- `src/saltmdb/domain/services/memory_service/search_primitives.py` (add one new function)
- `src/saltmdb/domain/services/memory_service/__init__.py` (export the new function)
- `src/saltmdb/domain/services/memory_service/orchestrator.py` (wire the new function into `search_memory`'s result assembly)
- `src/saltmdb/mcp/tools.py` (extend the `search_memory` tool's `description=` text only — no signature change)
- New test file: `tests/test_relevance_preview.py` (MCP-level, modeled on `tests/test_search_memory_drift_flag.py`)
- New test file: `tests/test_relevance_preview_chunks.py` (unit-level, modeled on `tests/test_topic_rerank.py`'s `TestRerankCandidatesByTopic`)

**Explicitly does not touch** (see §7 Out of scope for why): `src/saltmdb/domain/services/memory_service/search_primitives.py`'s existing `rerank_candidates_by_topic` function body (a new sibling function is added instead — nothing about that existing function's signature, body, or behavior changes); `src/saltmdb/utils/text.py` / `extract_title_and_snippet`; the existing `snippet`/`fts_snippet` machinery anywhere; `src/saltmdb/db/vector_schema.py` / `entity_chunk_embeddings` schema; `src/saltmdb/utils/chunking.py` / `chunk_text`; `src/saltmdb/domain/services/embedding_service.py`; `src/saltmdb/domain/services/reranker_service.py`; `src/saltmdb/daemon/dispatch.py`, `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/protocol.py`.

## 1. Why

`search_memory` today gives an agent, per result, a title, a `snippet`, and metadata — but the `snippet` is one of two things depending on how the result matched (verified against source 2026-09-14): a genuinely query-centered SQLite FTS5 `snippet()` excerpt (~32 tokens, `<mark>`-highlighted) when the result matched via FTS5 keyword search, or a 150-char "first three lines of the document" heuristic (`extract_title_and_snippet`, `src/saltmdb/utils/text.py:133-161`) with zero query relevance when the result only surfaced via dense-vector semantic search.

For long memories (some live memories, e.g. wayfinder-map roadmap documents, exceed 60,000 characters), this is a real usability gap: the 150-char heuristic-path snippet reveals almost nothing about a 60k-char document, and disambiguating candidates currently requires calling `get_memory` on each one — which, past `CONTENT_FILE_DUMP_THRESHOLD_CHARS = 20000` (`config.py:210`), doesn't even return inline content, only a `content_file_path` requiring a second round-trip (a file read) to inspect. The realistic cost today, for exactly the class of memory where disambiguation matters most, is "search → get_memory → file read → still might be irrelevant," repeated per candidate.

This spec adds a query-focused, purely extractive preview to `search_memory` results, built entirely from verbatim spans of each candidate's own content (no generated prose — SALTMDB has no internal LLM and this spec does not add one), so an agent can judge relevance without a `get_memory` round-trip in the common case. The design was pressure-tested via a full `grilling`-skill session with the user (2026-09-14) covering activation scope, chunk-boundary coherence, tunnel-vision risk, redundancy handling, ordering, budget distribution, and the correlated-failure-mode risk of reusing one embedding model for both ranking and preview selection; every decision below reflects that session's resolution, not a fresh design choice made while writing this document.

Mechanism: `entity_chunk_embeddings` (`db/vector_schema.py:109-115`) already persists chunk-level embeddings for every entity's content (`CHUNK_SIZE_CHARS=1200`, `CHUNK_OVERLAP_CHARS=200`, content-hash staleness guarded, backfilled on repair sweep), and `rerank_candidates_by_topic` (`search_primitives.py:437-534`) already proves the query-time consumption pattern — comparing a chunked, embedded query against these precomputed vectors via one SQL query per query-chunk, with zero re-chunking or re-embedding of candidate content. This spec adds a new, separate function that reuses the identical precomputed-vector/staleness-guard pattern but returns *which* chunks matched (with their `char_start`/`char_end` offsets, already stored) instead of only an aggregate score, then assembles those spans into displayable text.

## 2. `src/saltmdb/config.py` — new constants

Insert a new block immediately after the existing `RERANK_BROAD_THEME_THRESHOLD` definition (the block ending at):
```python
RERANK_BROAD_THEME_THRESHOLD = (
    0.5322  # topic_score >= this (and below SAME_TOPIC) -> "BROADLY_RELATED_THEMES"
)
```
and before the existing `# Stage-2 chunk candidate generation` comment block. Insert exactly:

```python
# Query-focused extractive preview for search_memory results (relevance_preview /
# relevance_preview_meta fields). Reuses entity_chunk_embeddings (CHUNK_SIZE_CHARS,
# CHUNK_OVERLAP_CHARS above) via a new sibling function to rerank_candidates_by_topic --
# see get_relevance_preview_data in search_primitives.py. NOT benchmarked -- these are
# placeholder defaults pending real measurement (SALTMDB grilling session 2026-09-14,
# search_memory design discussion); do not treat as final without new benchmark evidence,
# matching this file's existing convention for RERANK_*/DEDUP_CROSS_ENCODER_THRESHOLD above.
RELEVANCE_PREVIEW_CHUNK_PERCENT = 0.20  # fraction of a candidate's total chunk count selected
RELEVANCE_PREVIEW_MIN_CHUNKS = 1  # floor on selected chunk count before the opening-chunk add
RELEVANCE_PREVIEW_MAX_CHUNKS = 4  # ceiling on selected chunk count before the opening-chunk add
RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS = 200  # per-side cap when expanding a selected chunk's
# span outward to the nearest paragraph/block boundary in full_content
RELEVANCE_PREVIEW_MERGE_GAP_CHARS = 50  # two expanded spans within this many chars of each
# other (or overlapping) merge into one contiguous excerpt instead of staying separate
RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS = 20000  # cumulative cap across one search_memory
# response's previews combined; never removes a result row, only omits relevance_preview
# on lower-ranked results once the running total would exceed this
```

No existing constant in this file is modified.

## 3. `src/saltmdb/domain/services/memory_service/search_primitives.py` — new function

Add `RELEVANCE_PREVIEW_CHUNK_PERCENT`, `RELEVANCE_PREVIEW_MIN_CHUNKS`, `RELEVANCE_PREVIEW_MAX_CHUNKS`, `RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS`, `RELEVANCE_PREVIEW_MERGE_GAP_CHARS` to the existing `from saltmdb.config import (...)` block at the top of this file (lines 10-18) — `SNIPPET_ELLIPSIS` is already imported there (line 17) and is reused as-is, not redefined. `RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS` is NOT imported here — it is consumed only in `orchestrator.py` (§4).

Add a new function immediately after `rerank_candidates_by_topic` ends (line 534), as a sibling — `rerank_candidates_by_topic` itself is not modified (see §7 for why). New function:

```python
def get_relevance_preview_data(
    query_text: str,
    candidate_ids: list[str],
    db_path: str,
) -> dict[str, dict]:
    """Returns {entity_id: {"text": str}} -- a query-focused extractive preview built
    exclusively from verbatim spans of each candidate's own full_content, for candidates
    scorable from PRECOMPUTED entity_chunk_embeddings rows (SALTMDB relevance-preview spec,
    2026-09-14 grilling session).

    Sibling to rerank_candidates_by_topic, NOT a modification of it -- kept as a separate
    function so mode="strict"'s existing relevance-gate behavior (which depends on
    rerank_candidates_by_topic's exact current contract) is never put at risk by this
    purely-additive, best-effort feature. Shares its no-re-chunk/no-re-embed discipline:
    reuses precomputed chunk vectors, same content_hash/status staleness guard.

    IDs with zero (or all-stale) chunk rows are absent from the returned dict -- caller
    (search_memory's orchestrator) treats an absent id as "no relevance_preview for this
    result," never as an error.

    On ANY failure (embedding call, DB error, sqlite_vec load failure) returns {} and logs a
    warning -- mirrors rerank_candidates_by_topic's own try-except-log-and-return-{} shape.
    This is a purely additive, best-effort UX feature; it must never turn a failure inside it
    into a failed search_memory call.
    """
    if not candidate_ids:
        return {}
    conn = None
    try:
        import sqlite_vec
        from saltmdb.utils.chunking import chunk_text
        from saltmdb.domain.services import embedding_service

        query_chunks = chunk_text(query_text or "", CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        if not query_chunks:
            query_chunks = [{"text": query_text or ""}]
        query_vectors = embedding_service.embed_query_texts([c["text"] for c in query_chunks])
        if not query_vectors:
            return {}

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        placeholders = ",".join("?" for _ in candidate_ids)
        sql = f"""
            SELECT c.entity_id, c.chunk_index, c.char_start, c.char_end,
                   vec_distance_cosine(c.embedding, ?) AS distance
            FROM entity_chunk_embeddings c
            JOIN entities e ON e.id = c.entity_id
            WHERE c.entity_id IN ({placeholders})
              AND e.status != 'archived'
              AND c.content_hash IS e.content_hash
        """
        # best[(entity_id, chunk_index)] = (min_distance_seen, char_start, char_end) -- min
        # across every query chunk, mirroring rerank_candidates_by_topic's per-candidate
        # MIN(distance) reduction but keyed per CHUNK, not aggregated per entity, since every
        # individual chunk's own span is needed here, not just one rolled-up score.
        best: dict[tuple[str, int], tuple[float, int, int]] = {}
        for qv in query_vectors:
            exec_params = [sqlite_vec.serialize_float32(qv)] + list(candidate_ids)
            for entity_id, chunk_index, char_start, char_end, distance in conn.execute(
                sql, exec_params
            ).fetchall():
                key = (entity_id, chunk_index)
                if key not in best or distance < best[key][0]:
                    best[key] = (distance, char_start, char_end)

        if not best:
            return {}

        by_entity: dict[str, list[tuple[int, int, int, float]]] = {}
        for (entity_id, chunk_index), (distance, char_start, char_end) in best.items():
            by_entity.setdefault(entity_id, []).append(
                (chunk_index, char_start, char_end, distance)
            )

        content_placeholders = ",".join("?" for _ in by_entity)
        content_rows = conn.execute(
            f"SELECT id, full_content FROM entities WHERE id IN ({content_placeholders})",
            list(by_entity.keys()),
        ).fetchall()
        full_content_by_id = {row[0]: row[1] or "" for row in content_rows}

        results: dict[str, dict] = {}
        for entity_id, chunks in by_entity.items():
            full_content = full_content_by_id.get(entity_id, "")
            total_chunks = len(chunks)
            k = min(
                RELEVANCE_PREVIEW_MAX_CHUNKS,
                max(RELEVANCE_PREVIEW_MIN_CHUNKS, round(RELEVANCE_PREVIEW_CHUNK_PERCENT * total_chunks)),
            )
            by_score = sorted(chunks, key=lambda c: c[3])  # ascending distance = best match first
            selected = list(by_score[:k])

            # Opening-chunk guarantee: ADDS chunk_index 0 when it isn't already selected on
            # merit, it never displaces the top-scoring pick -- so worst case this is K+1
            # chunks selected, not a swap that could silently drop the single best match when
            # K == 1. (Deliberate: displacing the top pick would defeat the point of a
            # query-focused preview for exactly the K=1 case that RELEVANCE_PREVIEW_MIN_CHUNKS
            # makes common.)
            if total_chunks > k and not any(c[0] == 0 for c in selected):
                opening = next((c for c in chunks if c[0] == 0), None)
                if opening is not None:
                    selected.append(opening)

            # Boundary-expand each selected chunk's (char_start, char_end) outward to the
            # nearest "\n\n" (or string boundary), capped at RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS
            # per side.
            expanded = []
            for chunk_index, char_start, char_end, _distance in selected:
                back_limit = max(0, char_start - RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS)
                boundary = full_content.rfind("\n\n", back_limit, char_start)
                expanded_start = boundary + 2 if boundary != -1 else back_limit
                fwd_limit = min(len(full_content), char_end + RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS)
                boundary = full_content.find("\n\n", char_end, fwd_limit)
                expanded_end = boundary if boundary != -1 else fwd_limit
                expanded.append([expanded_start, expanded_end])

            # Merge overlapping/near-adjacent spans; sorting by start also yields document
            # order for free, so no separate reorder step is needed afterward.
            expanded.sort(key=lambda s: s[0])
            merged: list[list[int]] = []
            for start, end in expanded:
                if merged and start <= merged[-1][1] + RELEVANCE_PREVIEW_MERGE_GAP_CHARS:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])

            text = SNIPPET_ELLIPSIS.join(full_content[s:e] for s, e in merged)
            results[entity_id] = {"text": text}

        return results
    except Exception as e:
        logger.warning("Relevance-preview chunk selection failed, falling back: %s", e)
        return {}
    finally:
        if conn:
            close_connection(conn)
```

Note: `CHUNK_SIZE_CHARS`, `CHUNK_OVERLAP_CHARS`, `get_connection`, `close_connection`, `logger` are all already imported/available in this file's existing top-of-file imports (lines 10-21) and `rerank_candidates_by_topic`'s own local `from saltmdb.config import (...)` (line 477-482) — reuse those existing bindings; do not re-import anything already in scope.

## 4. `src/saltmdb/domain/services/memory_service/orchestrator.py` — wire into `search_memory`

Add `RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS` to the existing import line:
```python
from saltmdb.config import get_db_path, STRICT_OVERFETCH_CANDIDATE_CAP
```
becomes:
```python
from saltmdb.config import (
    get_db_path,
    STRICT_OVERFETCH_CANDIDATE_CAP,
    RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS,
)
```

Immediately after the existing related-entities batch-fetch block (ends line 919 with the closing of the `for anchor, bpred, beid, betitle in batch_rel_cursor.fetchall():` loop) and before `results = []` (line 921), insert:

```python
        preview_map: dict[str, dict] = {}
        if sanitized_query and rows:
            preview_map = search_primitives.get_relevance_preview_data(
                query_keywords, [r[0] for r in rows], db_path
            )
        cumulative_preview_chars = 0
```

**Gate condition is `sanitized_query`, not raw `query_keywords`** — this was corrected during this spec's own pre-lock verification (2026-09-14), so it is called out explicitly rather than left implicit. `rows` at this point in the function can come from any of three branches: (1) the exact-title-match fast path (line 351, `if exact_title_id is not None and not collapse_supersedes_families:`), reachable only when `query_keywords` was truthy; (2) the hybrid FTS+dense path (`elif sanitized_query:`, line 368) — the main case; (3) the plain tags/filter browse path (`else`, line 859), reached whenever there was no usable query. `sanitized_query = sanitize_fts_query(query_keywords) if query_keywords else ""` (line 293) is truthy if and only if `query_keywords` was truthy AND didn't sanitize down to an empty string — a strict subset of raw `query_keywords` truthiness. Gating on raw `query_keywords` (as an earlier draft of this spec did) would incorrectly attempt preview generation for a degenerate query that sanitizes to empty (e.g. one consisting only of FTS5 special characters), where `rows` actually came from the browse branch. Gating on `sanitized_query` correctly covers branch (1) too (a real, non-degenerate `query_keywords` produced it) as well as branch (2), and correctly excludes branch (3). The TEXT passed to `get_relevance_preview_data` stays the raw `query_keywords`, not `sanitized_query` — matching the existing precedent set by every other `rerank_candidates_by_topic` call site in this same file (lines 528, 594, 617, 630 all pass raw `query_keywords`, since `sanitize_fts_query` produces FTS5 MATCH-safe syntax, not text suitable for embedding).

(`search_primitives` is already imported at module level, line 23: `from . import ranking, search_primitives, tags, validation` — call it via the qualified module reference per this file's own documented convention, lines 4-9.)

Inside the `for r in rows:` loop, immediately after the existing:
```python
            if drift_flag:
                item["drift_flag"] = drift_flag
```
and before `results.append(item)` (currently line 973), insert:
```python
            if eid in preview_map and cumulative_preview_chars < RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS:
                item["relevance_preview"] = preview_map[eid]["text"]
                item["relevance_preview_meta"] = {
                    "auto_generated": True,
                    "extractive": True,
                    "query_specific": True,
                    "complete": False,
                }
                cumulative_preview_chars += len(preview_map[eid]["text"])
```

This fires for both query-driven row sources (the exact-title-match fast path and the hybrid FTS+dense path) and correctly excludes the plain tags/filter browse path — see the gate-condition rationale above. The budget check is evaluated before attaching, so the item that crosses the cumulative threshold still gets its full preview; only items evaluated after the cap is already reached lose the field. No result item is ever removed from `results` by this logic — `len(results)` is unaffected by `RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS`.

## 5. `src/saltmdb/domain/services/memory_service/__init__.py` — export

In the existing:
```python
from .search_primitives import (
    STOP_WORDS,
    semantic_search,
    chunk_candidate_search,
    retrieval_vector_search,
    rerank_candidates_by_topic,
    reciprocal_rank_fusion,
    weighted_reciprocal_rank_fusion,
    _run_fts_search,
    _run_retrieval_fts_search,
    _batch_semantic_similarities,
    _score_topics_with_fallback,
)
```
add `get_relevance_preview_data,` immediately after `rerank_candidates_by_topic,`.

In `__all__`, add `"get_relevance_preview_data",` immediately after the existing `"rerank_candidates_by_topic",` entry.

## 6. `src/saltmdb/mcp/tools.py` — document the new fields

In the `@mcp.tool(description="""...""")` block above `def search_memory(` (starts line 492), insert a new paragraph immediately after the existing `drift_flag` paragraph:
```
    Search by query, tags, context, memory type, or core status. `mode="strict"` resolves
    superseded matches and applies relevance abstention; `mode="history"` keeps matched history
    visible and labels superseded results; `mode="broad"` preserves ordinary retrieval. Each result
    item may include an optional `drift_flag` field, present only when a drift sweep flagged the
    memory; this field is advisory-only and never auto-corrected -- an agent seeing it should
    re-verify the citation itself, not blindly trust either the flag or the original claim.
```
becomes (new paragraph appended, nothing above it changed):
```
    Search by query, tags, context, memory type, or core status. `mode="strict"` resolves
    superseded matches and applies relevance abstention; `mode="history"` keeps matched history
    visible and labels superseded results; `mode="broad"` preserves ordinary retrieval. Each result
    item may include an optional `drift_flag` field, present only when a drift sweep flagged the
    memory; this field is advisory-only and never auto-corrected -- an agent seeing it should
    re-verify the citation itself, not blindly trust either the flag or the original claim.

    A query-based result item may also include `relevance_preview` (a string) plus
    `relevance_preview_meta` (an object with `auto_generated`, `extractive`, `query_specific`,
    and `complete` booleans, `complete` always `false`) -- a small set of verbatim excerpts
    pulled from that memory's own content, selected for relevance to this specific query. It
    is a hint for deciding whether to call `get_memory`, never a substitute for it: absence of
    a detail from the preview is never evidence that detail is absent from the memory itself.
```

No other line in this file changes; the function signature (lines 511-523) and its body (lines 524-541+) are untouched — this is output-only, no new input parameter.

## 7. Out of scope

- **A second embedding model.** Considered and explicitly rejected during design (correlated-failure-mode discussion, 2026-09-14): the only bi-encoder SALTMDB bundles is reused as-is; no new model, no new vec0 table, no new bundling-budget question. The correlated-failure risk (same model drives both ranking and preview-chunk selection) is accepted as a known v1 limitation, not solved here.
- **Cross-encoder reranker dependency.** `reranker_service.py` is not called anywhere in this feature. It is feature-flagged off by default and caps candidate text at 1000 chars (`CROSS_ENCODER_MAX_CHARS`) — unsuitable as a primary mechanism for long documents even when enabled.
- **Lexical/BM25 fusion at the chunk level.** Named as a cheap future mitigation for the correlated-failure risk if benchmarking ever shows it's needed; not built here.
- **Modifying `rerank_candidates_by_topic` itself.** A new sibling function is added instead specifically so `mode="strict"`'s existing relevance-gate behavior (which depends on that function's current exact contract) is never put at risk.
- **A per-call opt-out/suppress toggle** on `search_memory` for this feature. Always-on for v1 (per design-session decision); a toggle is an explicitly deferred, benchmark-triggered follow-up, not built here.
- **Changing `CHUNK_SIZE_CHARS`, `CHUNK_OVERLAP_CHARS`, or the `entity_chunk_embeddings` schema.** Reused exactly as they exist today; no migration.
- **Changing the existing `snippet` field, `extract_title_and_snippet`, or the FTS5 `snippet()` path.** `relevance_preview` is additive; nothing about how `snippet` is computed or when it's populated changes.
- **The `daemon/dispatch.py` / `daemon/server.py` / `daemon/protocol.py` layer.** Confirmed (`dispatch.py:159-160`, `_dispatch_search_memory`) to call `memory_service.search_memory(...)` directly and return its result verbatim with no field-level re-serialization — new dict keys on result items pass through automatically. No RPC/wire-format change, no new dispatch entry.
- **Exact numeric tuning of `RELEVANCE_PREVIEW_CHUNK_PERCENT`/`_MIN_CHUNKS`/`_MAX_CHUNKS`/`_MAX_EXPANSION_CHARS`/`_MERGE_GAP_CHARS`/`_TOTAL_BUDGET_CHARS`.** Shipped as placeholder defaults per this spec, explicitly flagged for future benchmarking (matching this codebase's existing convention for provisional constants, e.g. `DEDUP_CROSS_ENCODER_THRESHOLD`) — not a target for this implementation to "improve" via guessing.
- **Any change to `search_memory`'s existing parameters, `limit` semantics, or pagination (`cursor`, `offset`).** Untouched.
- **A benchmark/evaluation harness measuring fewer `get_memory` calls.** Out of scope for this implementation pass; a natural follow-up, not required for acceptance here.

## 8. Acceptance

From the worktree root (`/home/zbalint/workspace/SALTMDB-relevance-preview`), using its own venv:

```bash
.venv/bin/python -m unittest discover tests
```
must print `OK` (skips allowed — the pre-implementation baseline run on 2026-09-14 was `Ran 1236 tests ... OK (skipped=2)`; no new errors or failures versus that baseline), and must include the two new test files (`tests/test_relevance_preview.py`, `tests/test_relevance_preview_chunks.py`) among the discovered tests, each passing.

New tests must cover, at minimum:
1. **MCP-level** (`tests/test_relevance_preview.py`, modeled on `tests/test_search_memory_drift_flag.py`'s structure — `tools.store_memory` + `tools.search_memory` through `tools.DirectDispatchBackend()`): a stored memory long enough to produce multiple chunks (content > `CHUNK_SIZE_CHARS`) returns a `relevance_preview` string and a `relevance_preview_meta` dict with `complete: False` on a query-based search; a memory short enough to produce exactly one chunk still gets a `relevance_preview` (no length gate — selection is additive, opening chunk is the only chunk); a browse-mode call (`tools.search_memory(tags_filter=[...])`, no `query_keywords`) produces results with **no** `relevance_preview` key on any item; a query-based result item's `relevance_preview` text is verified to be a substring drawn from that memory's own stored content (never generated prose).
2. **Unit-level** (`tests/test_relevance_preview_chunks.py`, modeled on `tests/test_topic_rerank.py`'s `TestRerankCandidatesByTopic` — axis-aligned unit vectors inserted directly into `entity_chunk_embeddings`, `embedding_service.embed_query_texts` mocked, hand-computable exact cosine similarities): opening-chunk (`chunk_index=0`) is present in the result even when it does not score in the top-K on merit (verifies the ADD-not-displace behavior — assert the top-scoring non-zero chunk's text is *also* present, not evicted); two adjacent/overlapping selected spans merge into one contiguous excerpt (assert no duplicated substring in the output and exactly one `SNIPPET_ELLIPSIS`-free merged block where expected); a candidate with zero chunk rows (never embedded) is absent from the returned dict entirely, not an error; a candidate whose only chunk rows are stale (`content_hash` mismatch) is likewise absent, mirroring `rerank_candidates_by_topic`'s existing staleness contract; any exception raised inside the function (e.g. a forced `embed_query_texts` failure via mock) results in `{}` returned, not a raised exception.

Additionally: `rg -rn "get_relevance_preview_data" src/saltmdb/domain/services/memory_service/__init__.py` must show it present in both the import block and `__all__`.

## 9. Amendments

(none yet)
