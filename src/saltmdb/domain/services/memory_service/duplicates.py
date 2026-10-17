"""Duplicate detection, memory scanning, and bulk archive for memory_service.

Pure code-motion extraction (see refactor plan).
"""

import sqlite3
import math
from typing import Any, cast

from saltmdb.config import (
    CROSS_ENCODER_MAX_CHARS,
    CROSS_ENCODER_MAX_QUERY_CHARS,
    DEDUP_CROSS_ENCODER_MAX_CANDIDATES,
    DEDUP_CROSS_ENCODER_MODEL,
    DEDUP_CROSS_ENCODER_THRESHOLD,
    DEDUP_FTS_MAX_TERMS,
    DEDUP_LEXICAL_THRESHOLD,
    DEDUP_SUPERSESSION_THRESHOLD,
    get_db_path,
)
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.domain.services import reranker_service
from saltmdb.utils.nlp import word_sim
from saltmdb.utils.envelope import is_rejected

from . import lifecycle, search_primitives
from ._shared import logger


def _duplicate_filters(
    agent_id: str | None, context_id: str | None, exclude_ids: list | None
) -> tuple[list[str], list[str], list]:
    where = ["status != 'archived'"]
    fts_where = ["e.status != 'archived'"]
    params = []
    if exclude_ids:
        clean_excludes = [str(value) for value in exclude_ids if str(value)]
        if clean_excludes:
            placeholders = ",".join("?" for _ in clean_excludes)
            where.append(f"id NOT IN ({placeholders})")
            fts_where.append(f"e.id NOT IN ({placeholders})")
            params.extend(clean_excludes)
    if agent_id:
        where.append("(agent_id = ? OR agent_id IS NULL OR scope = 'shared')")
        fts_where.append("(e.agent_id = ? OR e.agent_id IS NULL OR e.scope = 'shared')")
        params.append(agent_id)
    if context_id:
        where.append("(context_id IS NULL OR context_id = ?)")
        fts_where.append("(e.context_id IS NULL OR e.context_id = ?)")
        params.append(context_id)
    return where, fts_where, params


def _duplicate_fts_candidates(
    conn, input_text: str, where: list[str], fts_where: list[str], params: list
) -> tuple[list, bool]:
    # Pre-filter with the OR-only BM25-ranked FTS5 search; SQLite errors retain the
    # bounded scalar fallback as an explicitly degraded path.
    capped_terms = search_primitives.build_fts_terms(input_text, DEDUP_FTS_MAX_TERMS)
    if not capped_terms:
        return [], False
    try:
        fts_rows = cast(
            list[Any],
            search_primitives._run_fts_search(
                conn,
                " ".join(capped_terms),
                fts_where,
                params,
                DEDUP_CROSS_ENCODER_MAX_CANDIDATES,
                0,
                or_only=True,
            ),
        )
        return [(row[0], row[1], row[2], row[8], row[9]) for row in fts_rows], False
    except sqlite3.Error as exc:
        logger.warning("FTS duplicate pre-filter unavailable; using scalar fallback: %s", exc)
        cursor = conn.execute(
            f"SELECT id, title, full_content, agent_id, scope FROM entities "
            f"WHERE {' AND '.join(where) if where else '1=1'} LIMIT 30",
            params,
        )
        return cursor.fetchall(), True


def _duplicate_record(
    eid: str, title: str, agent_id: str | None, scope: str | None, score: float
) -> dict:
    return {
        "id": eid,
        "title": title,
        "agent_id": agent_id,
        "scope": scope,
        "similarity_score": round(score, 3),
    }


def _judge_duplicate_candidates(input_text: str, candidates: list) -> list[dict]:
    capped_query = input_text[:CROSS_ENCODER_MAX_QUERY_CHARS]
    capped_texts = [
        f"{row[1]} {row[2]}"[:CROSS_ENCODER_MAX_CHARS]
        for row in candidates[:DEDUP_CROSS_ENCODER_MAX_CANDIDATES]
    ]
    model = reranker_service.get_model(DEDUP_CROSS_ENCODER_MODEL)
    scores = list(model.rerank(capped_query, capped_texts))
    if len(scores) != len(capped_texts) or not all(
        isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
        for score in scores
    ):
        raise ValueError("cross-encoder returned malformed scores")
    duplicates = []
    for row, score in zip(candidates[:DEDUP_CROSS_ENCODER_MAX_CANDIDATES], scores):
        if score >= DEDUP_CROSS_ENCODER_THRESHOLD:
            duplicates.append(_duplicate_record(row[0], row[1], row[3], row[4], score))
    return duplicates


def _fallback_duplicate_score(
    input_text: str,
    row: tuple,
    semantic_sims: dict,
    use_semantic: bool,
    query_vector,
) -> tuple[float, float]:
    eid, etitle, econtent = row[:3]
    existing_text = f"{etitle} {econtent}"
    if eid in semantic_sims:
        return semantic_sims[eid], DEDUP_SUPERSESSION_THRESHOLD
    if use_semantic and query_vector is not None:
        return _uncached_duplicate_similarity(input_text, existing_text, query_vector)
    return word_sim(input_text, existing_text), DEDUP_LEXICAL_THRESHOLD


def _fallback_duplicate_candidates(
    input_text: str, candidates: list, effective_db_path: str
) -> list[dict]:
    from saltmdb.config import is_semantic_search_enabled

    use_semantic = is_semantic_search_enabled()
    query_vector = None
    if use_semantic:
        try:
            from saltmdb.domain.services import embedding_service

            query_vector = embedding_service.embed_text(input_text)
        except Exception as ex:
            logger.warning("Could not generate query embedding for duplicate check: %s", ex)
    semantic_sims: dict = {}
    if use_semantic and query_vector is not None and candidates:
        semantic_sims = search_primitives._batch_semantic_similarities(
            [row[0] for row in candidates], query_vector, effective_db_path
        )
    duplicates = []
    for row in candidates:
        sim, threshold = _fallback_duplicate_score(
            input_text, row, semantic_sims, use_semantic, query_vector
        )
        if sim >= threshold:
            eid, etitle, eagent_id, escope = row[0], row[1], row[3], row[4]
            duplicates.append(_duplicate_record(eid, etitle, eagent_id, escope, sim))
    return duplicates


def _uncached_duplicate_similarity(
    input_text: str, existing_text: str, query_vector
) -> tuple[float, float]:
    try:
        from saltmdb.domain.services import embedding_service
        import numpy as np

        candidate_vector = embedding_service.embed_text(existing_text)
        dot_product = np.dot(query_vector, candidate_vector)
        norm_a = np.linalg.norm(query_vector)
        norm_b = np.linalg.norm(candidate_vector)
        sim = float(dot_product / (norm_a * norm_b)) if norm_a > 0 and norm_b > 0 else 0.0
        return sim, DEDUP_SUPERSESSION_THRESHOLD
    except Exception:
        return word_sim(input_text, existing_text), DEDUP_LEXICAL_THRESHOLD


def check_duplicate_memories(
    title: str = None,
    content: str = None,
    agent_id: str = None,
    tags: list = None,
    context_id: str = None,
    exclude_ids: list = None,
    db_connection=None,
    db_path: str = None,
) -> dict:
    """Checks the database for potential near-duplicates of a proposed memory."""
    if not title and not content:
        return {"error": "Either title or content is required"}
    effective_db_path = db_path or get_db_path()
    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(effective_db_path)
        should_close = True
    try:
        where, fts_where, params = _duplicate_filters(agent_id, context_id, exclude_ids)
        input_text = f"{title or ''} {content or ''}"
        fts_candidates, fts_error = _duplicate_fts_candidates(
            conn, input_text, where, fts_where, params
        )
        if not fts_candidates and not fts_error:
            return {"duplicate_found": False, "potential_duplicates": []}
        try:
            duplicates = _judge_duplicate_candidates(input_text, fts_candidates)
        except Exception as ex:
            logger.warning(
                "Cross-encoder duplicate judging failed (model=%s); falling back to the "
                "existing cosine/lexical logic: %s",
                DEDUP_CROSS_ENCODER_MODEL,
                ex,
            )
            duplicates = _fallback_duplicate_candidates(
                input_text, fts_candidates, effective_db_path
            )
        duplicates.sort(key=lambda item: item["similarity_score"], reverse=True)
        return {"duplicate_found": len(duplicates) > 0, "potential_duplicates": duplicates}
    except Exception as ex:
        logger.error("Error checking duplicate memories: %s", ex)
        return {"error": str(ex)}
    finally:
        if should_close:
            close_connection(conn)


def scan_memories(
    agent_id: str = None,
    status_filter: str = None,
    limit: int = 20,
    offset: int = 0,
    cursor: str = None,
    db_connection=None,
    db_path: str = None,
) -> list:
    """Scans and inspects lists/contents of memories for audits."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    if cursor and cursor.startswith("offset:"):
        try:
            offset = int(cursor.split(":")[1])
        except ValueError:
            pass

    try:
        where = []
        params = []
        if agent_id:
            where.append("(agent_id = ? OR scope = 'shared')")
            params.append(agent_id)

        if status_filter:
            if status_filter == "active":
                where.append("status != 'archived'")
            else:
                where.append("status = ?")
                params.append(status_filter)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cursor_obj = conn.execute(
            f"""
            SELECT id, title, agent_id, status, weight, is_core, updated_at, memory_type
            FROM entities
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """,
            params + [limit, offset],
        )

        rows = cursor_obj.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "agent_id": r[2],
                "status": r[3],
                "weight": r[4],
                "is_core": bool(r[5]),
                "updated_at": r[6],
                "memory_type": r[7],
                "cursor": f"offset:{offset + limit}",
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Error scanning memories: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def _archive_batch_write(conn, archive_requests: list, results: list[Any]) -> None:
    results.clear()
    for req in archive_requests:
        eid = req if isinstance(req, str) else req.get("entity_id")
        owner = req.get("agent_id") if isinstance(req, dict) else None
        res = lifecycle.archive_memory(
            entity_id=eid, agent_id=owner, db_connection=conn, _in_transaction=True
        )
        if is_rejected(res):
            raise RuntimeError(
                f"Bulk archive aborted (all-or-nothing): {res['errors'][0]['message']}"
            )
        results.append({"status": "success", "entity_id": eid, "result": res["data"]["message"]})


def bulk_archive_memory(archive_requests: list, db_connection=None, db_path: str = None) -> list:
    """Bulk archives memories atomically -- all-or-nothing.

    The entire batch runs inside a single write_transaction_retrying transaction. If any
    item raises (or would otherwise be reported as an error), the whole transaction rolls
    back, so no partial set of archives is ever left committed. Because a single failure
    unwinds every prior "successful" item in the same batch, a mixed per-item success/error
    list would misrepresent the outcome -- so on failure this returns a single top-level
    error result instead of claiming any individual items succeeded.
    """
    if not archive_requests or not isinstance(archive_requests, list):
        return [{"status": "error", "error": "archive_requests must be a non-empty list"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
    results: list[Any] = []
    try:
        write_transaction_retrying(
            conn,
            lambda transaction_conn: _archive_batch_write(
                transaction_conn, archive_requests, results
            ),
        )
        return results
    except Exception as e:
        logger.error("Error in bulk archive memory (batch rolled back, no items archived): %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
