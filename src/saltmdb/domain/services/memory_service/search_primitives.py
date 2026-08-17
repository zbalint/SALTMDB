"""Search primitives for memory_service: FTS/vector/chunk candidate lookups and RRF fusion.

Pure code-motion extraction (see refactor plan). No cross-module dependencies
within this package (verified: zero calls into ranking.py either direction).
"""

import math
from typing import Any

from saltmdb.config import (
    BM25_TITLE_WEIGHT,
    BM25_CONTENT_WEIGHT,
    BM25_ALIAS_WEIGHT,
    RELATION_COUNT_BOOST,
    SNIPPET_MAX_TOKENS,
    SNIPPET_MATCH_START,
    SNIPPET_MATCH_END,
    SNIPPET_ELLIPSIS,
)
from saltmdb.db.connection import get_connection, close_connection

from ._shared import logger

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
    "what",
    "my",
    "when",
    "i",
    "type",
    "did",
    "how",
    "does",
    "or",
    "which",
    "who",
    "whom",
    "this",
    "these",
    "those",
}


def _run_fts_search(
    conn,
    sanitized_query: str,
    where_clauses: list,
    params: list,
    limit: int,
    offset: int,
    *,
    return_fallback_flag: bool = False,
) -> list | tuple[list, bool]:
    """Execute the FTS5/BM25 query with AND->OR fallback. Returns sqlite3 Row list.

    Each row's last column, fts_snippet, is a query-centered excerpt of full_content
    (FTS5 snippet(), column index 2) -- populated because this row genuinely matched via
    FTS5 MATCH in this query, distinct from rows that only surface via semantic_search().

    `return_fallback_flag` (opt-in, default False for backward compatibility with the two
    benchmark scripts that call this function directly): when True, returns
    `(rows, used_or_fallback)` instead of a bare row list, where `used_or_fallback` is True iff
    the AND-joined MATCH found nothing and the OR-joined retry is what actually produced `rows`.
    This is a single pool-level bool, not a per-row property -- the OR-fallback is an
    all-or-nothing property of how the query as a whole was executed. Every return path
    (including the early-return empty-terms case) honors the flag: `([], False)`, never a bare
    `[]`, when `return_fallback_flag=True`.
    """
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in STOP_WORDS]
    if not terms:
        terms = raw_terms

    if not terms:
        return ([], False) if return_fallback_flag else []

    fts_query_str = " ".join(f'"{t}"*' for t in terms)
    where_sql = f" AND {' AND '.join(where_clauses)}" if where_clauses else ""
    bm25_weights = f"{BM25_TITLE_WEIGHT}, {BM25_CONTENT_WEIGHT}, {BM25_ALIAS_WEIGHT}"
    # full_content is column index 2 in entities_fts (id=0 UNINDEXED, title=1, full_content=2,
    # search_aliases=3 -- see db/schema.py). Markers/ellipsis/budget are static config
    # constants inlined as literals (not `?` params) to avoid disturbing exec_params ordering.
    snippet_sql = (
        f"snippet(entities_fts, 2, '{SNIPPET_MATCH_START}', '{SNIPPET_MATCH_END}', "
        f"'{SNIPPET_ELLIPSIS}', {SNIPPET_MAX_TOKENS})"
    )
    sql = f"""
        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
               bm25(entities_fts, {bm25_weights}) as rank_score,
               e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id, e.memory_type,
               (SELECT COUNT(*) FROM relations r WHERE r.target_id = e.id
                AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))) as rel_count,
               {snippet_sql} as fts_snippet
        FROM entities_fts fts
        JOIN entities e ON fts.id = e.id
        WHERE fts.entities_fts MATCH ?{where_sql}
        ORDER BY (bm25(entities_fts, {bm25_weights}) * e.weight - (rel_count * {RELATION_COUNT_BOOST})) ASC,
                 e.updated_at DESC
        LIMIT ? OFFSET ?
    """
    exec_params = [fts_query_str] + params + [limit, offset]
    rows = conn.execute(sql, exec_params).fetchall()
    used_or_fallback = False
    if not rows and len(terms) > 1:
        fts_fallback_query = " OR ".join(f'"{t}"*' for t in terms)
        exec_params_fb = [fts_fallback_query] + params + [limit, offset]
        rows = conn.execute(sql, exec_params_fb).fetchall()
        # True iff the OR retry actually produced rows (Codex review finding) -- not merely
        # "the OR branch ran." An AND-empty query whose OR retry ALSO finds nothing leaves
        # `rows` empty either way, but the flag's own contract ("the OR-joined retry is what
        # produced rows", see this function's docstring) should reflect reality precisely, not
        # just "the fallback was attempted."
        used_or_fallback = bool(rows)
    return (rows, used_or_fallback) if return_fallback_flag else rows


def _run_retrieval_fts_search(
    conn,
    sanitized_query: str,
    where_clauses: list,
    params: list,
    limit: int,
    offset: int,
) -> list[tuple[str, float]]:
    """Return ranked ids from the optional retrieval-text FTS channel.

    Only the entity id and opaque BM25 rank leave this helper.  In particular, retrieval text is
    never selected into a result/diagnostic payload.  The equality guard against the live entity
    value makes direct legacy writes fail closed until the maintenance trigger catches up.
    """
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in STOP_WORDS] or raw_terms
    if not terms:
        return []
    fts_query = " ".join(f'"{term}"*' for term in terms)
    where_sql = f" AND {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT e.id, bm25(retrieval_fts) AS rank_score
        FROM retrieval_fts
        JOIN entities e ON e.id = retrieval_fts.id
        WHERE retrieval_fts MATCH ?
          AND e.retrieval_text IS NOT NULL
          AND retrieval_fts.retrieval_text = e.retrieval_text
          {where_sql}
        ORDER BY bm25(retrieval_fts) ASC, e.updated_at DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, [fts_query] + params + [limit, offset]).fetchall()
    if not rows and len(terms) > 1:
        fallback = " OR ".join(f'"{term}"*' for term in terms)
        rows = conn.execute(sql, [fallback] + params + [limit, offset]).fetchall()
    return [(row[0], row[1]) for row in rows]


def retrieval_vector_search(
    query: str,
    where_clauses: list[str],
    params: list,
    limit: int,
    db_path: str,
    offset: int = 0,
) -> list[tuple[str, float]]:
    """Return fresh, successfully persisted retrieval-text vector candidates.

    A vector participates only when its auxiliary source hash equals the live entity hash and a
    matching retrieval job is in ``succeeded`` state.  Both checks are synchronous, so missing,
    stale, queued, retrying, or failed embeddings contribute no candidates.
    """
    conn = None
    try:
        import sqlite_vec
        from saltmdb.domain.services import embedding_service

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        query_vector = embedding_service.embed_text(query)
        knn_k = limit + offset
        # vec0 rejects ordinary WHERE predicates on auxiliary columns in its KNN query.  Keep
        # this first read pure MATCH+k, then synchronously reapply hash/job/status/caller filters
        # against ordinary tables (same pattern as chunk_candidate_search).
        rows = conn.execute(
            "SELECT entity_id,distance,source_hash FROM retrieval_embeddings "
            "WHERE embedding MATCH ? AND k = ?",
            [sqlite_vec.serialize_float32(query_vector), knn_k],
        ).fetchall()
        if not rows:
            return []
        entity_ids = list({row[0] for row in rows})
        placeholders = ",".join("?" for _ in entity_ids)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        entity_rows = conn.execute(
            f"SELECT e.id,e.retrieval_text_hash,e.status FROM entities e "
            f"JOIN retrieval_embedding_jobs rj ON rj.entity_id=e.id "
            f"AND rj.source_hash=e.retrieval_text_hash AND rj.state='succeeded' "
            f"WHERE e.id IN ({placeholders}) AND e.retrieval_text IS NOT NULL AND {where_sql}",
            entity_ids + params,
        ).fetchall()
        fresh = {row[0]: row[1] for row in entity_rows if row[2] != "archived"}
        ranked = [
            (entity_id, distance)
            for entity_id, distance, source_hash in rows
            if entity_id in fresh and source_hash == fresh[entity_id]
        ]
        return ranked[offset : offset + limit]
    finally:
        if conn:
            close_connection(conn)


def semantic_search(
    query: str,
    where_clauses: list[str],
    params: list,
    limit: int,
    db_path: str,
    offset: int = 0,
) -> list[tuple[str, float]]:
    """Return [(entity_id, cosine_distance), ...] ascending by distance.

    Opens its own dedicated connection so it can safely load the sqlite_vec
    extension without conflicting with a concurrent FTS search on a shared conn.

    Raises on failure (post-Codex-review fix, P0): model loading, embedding,
    sqlite-vec extension loading, or the vector SQL query all propagate their
    exception to the caller instead of being swallowed into an empty result. A
    caller-side `except -> []` here would be indistinguishable from a genuinely
    successful query that just found zero candidates -- search_memory's RRF merge
    would then quietly present ordinary-looking FTS-only results, which is
    exactly the silent-fallback failure mode Part 0 already retired for the
    semantic-disabled case. search_memory() is expected to let this exception
    propagate up to its own top-level `except Exception -> [{"error": ...}]`
    handler rather than catching it locally.
    """
    conn = None
    try:
        import sqlite_vec
        from saltmdb.domain.services import embedding_service
        from saltmdb.db.connection import get_connection

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        query_vector = embedding_service.embed_text(query)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        # vec0's `MATCH ... AND k = N` is its KNN execution path.  Calling
        # vec_distance_cosine() in ORDER BY forces a full scan of every embedding and made
        # realistic frozen-corpus evaluation (and normal search at scale) effectively hang.
        # Request enough neighbours to retain the existing pagination contract, then apply the
        # ordinary SQL limit/offset below.
        knn_k = limit + offset
        sql = f"""
            SELECT e.id, ee.distance as distance
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            WHERE ee.embedding MATCH ? AND k = ?
              AND e.embedding_status = 'ready' AND {where_sql}
            LIMIT ? OFFSET ?
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector), knn_k] + params + [limit, offset]
        rows = conn.execute(sql, exec_params).fetchall()
        return [(row[0], row[1]) for row in rows]
    finally:
        if conn:
            close_connection(conn)


def chunk_candidate_search(
    query: str,
    where_clauses: list[str],
    params: list,
    candidate_window: int,
    oversampling_multiplier: int,
    db_path: str,
) -> tuple[list[tuple[str, float]], dict[str, Any]]:
    """Return fresh entity candidates generated from the chunk-vector KNN index.

    The vec0 query deliberately asks for ``candidate_window * oversampling_multiplier`` chunk
    rows, then performs the entity-level deduplication in Python using the minimum distance.  A
    stale row can never consume a candidate slot: the join requires the chunk's content hash to
    equal the currently-live entity hash, and archived entities are excluded before the KNN rows
    reach the deduper.  This helper does not alter the entity-vector or FTS windows, preserving
    the legacy pagination contract when the feature is disabled.

    The second return value is transport-safe execution evidence for benchmark runs.  In
    particular, ``candidate_shortfall`` records how many unique entities were unavailable after
    freshness filtering/deduplication, rather than silently making a small chunk pool look like a
    full one.
    """
    if not query:
        return [], {
            "requested_chunk_rows": 0,
            "candidate_window": candidate_window,
            "oversampling_multiplier": oversampling_multiplier,
            "raw_chunk_rows": 0,
            "unique_fresh_entities": 0,
            "candidate_shortfall": candidate_window,
        }
    conn = None
    requested_rows = candidate_window * oversampling_multiplier
    try:
        import sqlite_vec

        from saltmdb.domain.services import embedding_service

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        query_vector = embedding_service.embed_text(query)
        # MATCH + k is vec0's indexed KNN execution path.  vec0 rejects ordinary WHERE/JOIN
        # constraints in a KNN query (``illegal WHERE constraint``), so the indexed read must be
        # a pure vector lookup.  Freshness/status/caller filters are reapplied immediately below
        # on the ordinary entities table before a row can enter the deduper.
        sql = """
            SELECT c.entity_id, c.distance, c.content_hash
            FROM entity_chunk_embeddings c
            WHERE c.embedding MATCH ? AND k = ?
        """
        rows = conn.execute(
            sql,
            [sqlite_vec.serialize_float32(query_vector), requested_rows],
        ).fetchall()
        entity_ids = list({row[0] for row in rows})
        entity_info: dict[str, tuple[str | None, str | None]] = {}
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
            entity_rows = conn.execute(
                f"SELECT e.id, e.content_hash, e.status FROM entities e "
                f"WHERE e.id IN ({placeholders}) AND {where_sql}",
                entity_ids + list(params),
            ).fetchall()
            entity_info = {row[0]: (row[1], row[2]) for row in entity_rows}
        best_distance: dict[str, float] = {}
        for entity_id, distance, chunk_hash in rows:
            current = entity_info.get(entity_id)
            if current is None or current[1] == "archived" or chunk_hash != current[0]:
                continue
            # sqlite_vec returns numeric distances, but keep a defensive finite check here so a
            # malformed extension row cannot poison sorting/RRF with NaN.
            try:
                distance_value = float(distance)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(distance_value):
                continue
            previous = best_distance.get(entity_id)
            if previous is None or distance_value < previous:
                best_distance[entity_id] = distance_value
        ranked = sorted(best_distance.items(), key=lambda item: (item[1], item[0]))[
            :candidate_window
        ]
        diagnostics = {
            "requested_chunk_rows": requested_rows,
            "candidate_window": candidate_window,
            "oversampling_multiplier": oversampling_multiplier,
            "raw_chunk_rows": len(rows),
            "unique_fresh_entities": len(best_distance),
            "returned_entities": len(ranked),
            "candidate_shortfall": max(0, candidate_window - len(ranked)),
        }
        return ranked, diagnostics
    finally:
        if conn:
            close_connection(conn)


def _batch_semantic_similarities(
    candidate_ids: list[str],
    query_vector: list[float],
    db_path: str,
) -> dict[str, float]:
    """Return {entity_id: cosine_similarity} for candidates with a ready precomputed vector.

    Looks up precomputed vectors for the given candidate IDs in a single SQL query instead
    of re-embedding each candidate's text. Opens its own dedicated connection and loads
    sqlite_vec on it, same reasoning as semantic_search(): must not conflict with a
    shared/FTS connection the caller may have passed in. Candidates whose embedding_status
    != 'ready' are simply absent from the result; callers should fall back to a lexical
    comparison for those.
    """
    if not candidate_ids:
        return {}
    conn = None
    try:
        import sqlite_vec

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        placeholders = ",".join("?" for _ in candidate_ids)
        sql = f"""
            SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            WHERE e.embedding_status = 'ready' AND e.id IN ({placeholders})
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector)] + list(candidate_ids)
        rows = conn.execute(sql, exec_params).fetchall()
        return {row[0]: 1.0 - row[1] for row in rows}
    except Exception as e:
        logger.warning("Batched semantic similarity lookup failed, falling back to lexical: %s", e)
        return {}
    finally:
        if conn:
            close_connection(conn)


def rerank_candidates_by_topic(
    query_text: str,
    candidate_ids: list[str],
    db_path: str,
) -> dict[str, dict]:
    """Return {entity_id: {"topic_score": float, "semantic_verdict": str}} for candidates
    scorable from PRECOMPUTED entity_chunk_embeddings rows (Phase 2 Part B -- see plans/ and
    SALTMDB memory `5c09effa`).

    Never re-chunks/re-embeds candidate content -- that's the deliberate, load-bearing deviation
    from the original Gemini-generated spec (which re-chunked/re-embedded candidates live on
    every search), reusing Foundation's precomputed chunk vectors instead. IDs with zero chunk
    rows are simply absent from the returned dict -- callers apply their own fallback (see
    search_memory's rerank_by_topic handling).

    Algorithm: chunk the query text the same way entity content is chunked (usually one chunk
    for a realistic search query, implemented generally), batch-embed all query chunks in one
    call, then for each query chunk vector run one SQL query that computes, per candidate,
    MIN(vec_distance_cosine(...)) grouped by entity_id -- the "max similarity over this
    candidate's chunks" half of Mean(Max(cosine_similarity)), since distance is 1 - similarity so
    MIN(distance) is exactly MAX(similarity). Accumulating (1.0 - min_distance) across every query
    chunk and dividing by the chunk count gives the "mean over query chunks" half. Same
    own-dedicated-connection / try-except-log-and-return-{} shape as _batch_semantic_similarities
    and semantic_search.

    Staleness guard (post-Codex-review fix): the SQL joins entity_chunk_embeddings to entities
    and requires `c.content_hash IS e.content_hash` plus `e.status != 'archived'`. Without this, a
    chunk row left behind by a failed/in-flight async refresh (Part A's write path) would still
    be readable and could influence topic_score until the next startup repair sweep -- the join
    makes staleness exclusion synchronous with every call instead. A candidate whose only chunk
    rows are stale is excluded from the returned dict exactly like a candidate with zero chunk
    rows, so it falls through to the caller's fallback tier (_batch_semantic_similarities) rather
    than silently vanishing.
    """
    if not candidate_ids:
        return {}
    conn = None
    try:
        import sqlite_vec

        from saltmdb.config import (
            CHUNK_SIZE_CHARS,
            CHUNK_OVERLAP_CHARS,
            RERANK_SAME_TOPIC_THRESHOLD,
            RERANK_BROAD_THEME_THRESHOLD,
        )
        from saltmdb.utils.chunking import chunk_text
        from saltmdb.domain.services import embedding_service

        query_chunks = chunk_text(query_text or "", CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        if not query_chunks:
            query_chunks = [{"text": query_text or ""}]
        query_vectors = embedding_service.embed_texts([c["text"] for c in query_chunks])
        if not query_vectors:
            return {}

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        placeholders = ",".join("?" for _ in candidate_ids)
        sql = f"""
            SELECT c.entity_id, MIN(vec_distance_cosine(c.embedding, ?)) AS min_distance
            FROM entity_chunk_embeddings c
            JOIN entities e ON e.id = c.entity_id
            WHERE c.entity_id IN ({placeholders})
              AND e.status != 'archived'
              AND c.content_hash IS e.content_hash
            GROUP BY c.entity_id
        """
        similarity_sums: dict[str, float] = {}
        for qv in query_vectors:
            exec_params = [sqlite_vec.serialize_float32(qv)] + list(candidate_ids)
            rows = conn.execute(sql, exec_params).fetchall()
            for entity_id, min_distance in rows:
                similarity_sums[entity_id] = similarity_sums.get(entity_id, 0.0) + (
                    1.0 - min_distance
                )

        num_query_chunks = len(query_vectors)
        results: dict[str, dict] = {}
        for entity_id, total in similarity_sums.items():
            topic_score = total / num_query_chunks
            if topic_score >= RERANK_SAME_TOPIC_THRESHOLD:
                verdict = "SAME_SPECIFIC_TOPIC"
            elif topic_score >= RERANK_BROAD_THEME_THRESHOLD:
                verdict = "BROADLY_RELATED_THEMES"
            else:
                verdict = "DIFFERENT_TOPICS"
            results[entity_id] = {"topic_score": topic_score, "semantic_verdict": verdict}
        return results
    except Exception as e:
        logger.warning("Cross-chunk topic reranking failed, falling back: %s", e)
        return {}
    finally:
        if conn:
            close_connection(conn)


def _score_topics_with_fallback(query_text: str, ids: list[str], db_path: str) -> dict[str, dict]:
    """Shared by rerank_by_topic's full-pool Stage-2 rerank and mode="strict"'s on-demand,
    FTS-less-candidates-only grounding lookup (Part B) -- factored out so both call sites use
    exactly one code path for "score these ids via chunk-level topic_score, falling back to
    entity-level cosine similarity (B4) for any id rerank_candidates_by_topic couldn't score (not
    yet chunk-embedded)". No candidate in `ids` is ever dropped: a fallback-tier id gets a
    BROADLY_RELATED_THEMES/DIFFERENT_TOPICS verdict from RERANK_BROAD_THEME_THRESHOLD instead of
    the primary tier's SAME_SPECIFIC_TOPIC/BROADLY_RELATED_THEMES/DIFFERENT_TOPICS three-way split.
    """
    if not ids:
        return {}
    topic_scores = rerank_candidates_by_topic(query_text, ids, db_path)
    missing_ids = [eid for eid in ids if eid not in topic_scores]
    if missing_ids:
        from saltmdb.config import RERANK_BROAD_THEME_THRESHOLD
        from saltmdb.domain.services import embedding_service

        fallback_vec = embedding_service.embed_text(query_text)
        fallback = _batch_semantic_similarities(missing_ids, fallback_vec, db_path)
        for eid in missing_ids:
            fscore = fallback.get(eid, 0.0)
            fverdict = (
                "BROADLY_RELATED_THEMES"
                if fscore >= RERANK_BROAD_THEME_THRESHOLD
                else "DIFFERENT_TOPICS"
            )
            topic_scores[eid] = {"topic_score": fscore, "semantic_verdict": fverdict}
    return topic_scores


def reciprocal_rank_fusion(
    fts_results: list,
    semantic_results: list[tuple[str, float]],
    limit: int,
    k: int = 60,
) -> dict[str, float]:
    """Merge two ranked lists by rank position (not raw score). Returns {entity_id: rrf_score}."""
    scores: dict[str, float] = {}
    for rank, row in enumerate(fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (entity_id, _distance) in enumerate(semantic_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return dict(ranked[:limit])


def weighted_reciprocal_rank_fusion(
    fts_results: list,
    semantic_results: list[tuple[str, float]],
    chunk_results: list[tuple[str, float]],
    limit: int,
    *,
    chunk_weight: float = 1.0,
    retrieval_fts_results: list[tuple[str, float]] | None = None,
    retrieval_vector_results: list[tuple[str, float]] | None = None,
    retrieval_fts_weight: float = 1.0,
    retrieval_vector_weight: float = 1.0,
    k: int = 60,
) -> dict[str, float]:
    """Fuse named FTS/entity-vector/chunk/retrieval-text ranked lists with weighted RRF.

    FTS and entity-vector channels are intentionally fixed at weight ``1``.  The chunk channel
    is the only experimental weight (0.5/1.0/1.5).  Ties retain first-seen channel/rank order,
    matching Python's stable sort and the exact legacy ``reciprocal_rank_fusion`` behavior when no
    chunk rows are supplied.
    """
    retrieval_fts_results = retrieval_fts_results or []
    retrieval_vector_results = retrieval_vector_results or []
    if not chunk_results and not retrieval_fts_results and not retrieval_vector_results:
        # Keep the old implementation as the disabled/empty-third-channel path rather than
        # duplicating its insertion-order/tie behavior in a second implementation.
        return reciprocal_rank_fusion(fts_results, semantic_results, limit, k=k)
    scores: dict[str, float] = {}
    for rank, row in enumerate(fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (entity_id, _distance) in enumerate(semantic_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (entity_id, _distance) in enumerate(chunk_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + float(chunk_weight) / (k + rank + 1)
    for rank, row in enumerate(retrieval_fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + float(retrieval_fts_weight) / (
            k + rank + 1
        )
    for rank, (entity_id, _distance) in enumerate(retrieval_vector_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + float(retrieval_vector_weight) / (
            k + rank + 1
        )
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return dict(ranked[:limit])
