"""Duplicate detection, memory scanning, and bulk archive for memory_service.

Pure code-motion extraction (see refactor plan).
"""

import sqlite3
import math
from typing import Any

from saltmdb.config import (
    CROSS_ENCODER_MAX_CHARS,
    CROSS_ENCODER_MAX_QUERY_CHARS,
    DEDUP_CROSS_ENCODER_MAX_CANDIDATES,
    DEDUP_CROSS_ENCODER_MODEL,
    DEDUP_CROSS_ENCODER_THRESHOLD,
    DEDUP_LEXICAL_THRESHOLD,
    DEDUP_SUPERSESSION_THRESHOLD,
    get_db_path,
)
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.domain.services import reranker_service
from saltmdb.utils.nlp import word_sim

from . import lifecycle, search_primitives
from ._shared import logger


def check_duplicate_memories(  # noqa: C901, PLR0912, PLR0915
    title: str = None,
    content: str = None,
    owner_id: str = None,
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
        where = ["status != 'archived'"]
        fts_where_clauses = ["e.status != 'archived'"]
        params = []

        if exclude_ids:
            clean_excludes = [str(x) for x in exclude_ids if str(x)]
            if clean_excludes:
                placeholders = ",".join("?" for _ in clean_excludes)
                where.append(f"id NOT IN ({placeholders})")
                fts_where_clauses.append(f"e.id NOT IN ({placeholders})")
                params.extend(clean_excludes)

        if owner_id:
            where.append("(owner_id = ? OR owner_id IS NULL OR scope = 'shared')")
            fts_where_clauses.append("(e.owner_id = ? OR e.owner_id IS NULL OR e.scope = 'shared')")
            params.append(owner_id)

        if context_id:
            where.append("(context_id IS NULL OR context_id = ?)")
            fts_where_clauses.append("(e.context_id IS NULL OR e.context_id = ?)")
            params.append(context_id)

        from saltmdb.utils.text import sanitize_fts_query

        input_text = f"{title or ''} {content or ''}"
        duplicates = []

        # Pre-filter using FTS5 to reduce candidates from O(N) to ~30 max
        fts_candidates = []
        search_terms = sanitize_fts_query(input_text)
        if search_terms:
            try:
                fts_where = " AND ".join(fts_where_clauses) if fts_where_clauses else "1=1"
                fts_rows = conn.execute(
                    f"SELECT e.id, e.title, e.full_content, e.owner_id, e.scope FROM entities_fts fts "
                    f"JOIN entities e ON fts.id = e.id "
                    f"WHERE entities_fts MATCH ? AND {fts_where} LIMIT 30",
                    [search_terms] + params,
                ).fetchall()
                fts_candidates = fts_rows
            except sqlite3.Error as exc:
                logger.warning(
                    "FTS duplicate pre-filter unavailable; using scalar fallback: %s", exc
                )

        # Fallback to full scan only if FTS returned nothing
        if not fts_candidates:
            cursor = conn.execute(
                f"SELECT id, title, full_content, owner_id, scope FROM entities "
                f"WHERE {' AND '.join(where) if where else '1=1'} LIMIT 30",
                params,
            )
            fts_candidates = cursor.fetchall()

        try:
            capped_query = input_text[:CROSS_ENCODER_MAX_QUERY_CHARS]
            capped_candidate_texts = [
                f"{etitle} {econtent}"[:CROSS_ENCODER_MAX_CHARS]
                for _, etitle, econtent, _, _ in fts_candidates[:DEDUP_CROSS_ENCODER_MAX_CANDIDATES]
            ]
            model = reranker_service.get_model(DEDUP_CROSS_ENCODER_MODEL)
            ce_scores = list(model.rerank(capped_query, capped_candidate_texts))
            if len(ce_scores) != len(capped_candidate_texts) or not all(
                isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(score)
                for score in ce_scores
            ):
                raise ValueError("cross-encoder returned malformed scores")

            for (eid, etitle, _, eowner, escope), score in zip(
                fts_candidates[:DEDUP_CROSS_ENCODER_MAX_CANDIDATES], ce_scores
            ):
                if score >= DEDUP_CROSS_ENCODER_THRESHOLD:
                    duplicates.append(
                        {
                            "id": eid,
                            "title": etitle,
                            "owner_id": eowner,
                            "scope": escope,
                            "similarity_score": round(score, 3),
                        }
                    )
        except Exception as ex:
            logger.warning(
                "Cross-encoder duplicate judging failed (model=%s); falling back to the "
                "existing cosine/lexical logic: %s",
                DEDUP_CROSS_ENCODER_MODEL,
                ex,
            )

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
            if use_semantic and query_vector is not None and fts_candidates:
                semantic_sims = search_primitives._batch_semantic_similarities(
                    [row[0] for row in fts_candidates], query_vector, effective_db_path
                )

            for eid, etitle, econtent, eowner, escope in fts_candidates:
                existing_text = f"{etitle} {econtent}"
                if eid in semantic_sims:
                    sim = semantic_sims[eid]
                    min_threshold = DEDUP_SUPERSESSION_THRESHOLD
                elif use_semantic and query_vector is not None:
                    try:
                        from saltmdb.domain.services import embedding_service
                        import numpy as np

                        cand_vec = embedding_service.embed_text(existing_text)
                        dot_product = np.dot(query_vector, cand_vec)
                        norm_a = np.linalg.norm(query_vector)
                        norm_b = np.linalg.norm(cand_vec)
                        sim = (
                            float(dot_product / (norm_a * norm_b))
                            if norm_a > 0 and norm_b > 0
                            else 0.0
                        )
                        min_threshold = DEDUP_SUPERSESSION_THRESHOLD
                    except Exception:
                        sim = word_sim(input_text, existing_text)
                        min_threshold = DEDUP_LEXICAL_THRESHOLD
                else:
                    sim = word_sim(input_text, existing_text)
                    min_threshold = DEDUP_LEXICAL_THRESHOLD

                if sim >= min_threshold:
                    duplicates.append(
                        {
                            "id": eid,
                            "title": etitle,
                            "owner_id": eowner,
                            "scope": escope,
                            "similarity_score": round(sim, 3),
                        }
                    )

        duplicates.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {"duplicate_found": len(duplicates) > 0, "potential_duplicates": duplicates}
    except Exception as e:
        logger.error("Error checking duplicate memories: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


def scan_memories(
    owner_id: str = None,
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
        if owner_id:
            where.append("(owner_id = ? OR scope = 'shared')")
            params.append(owner_id)

        if status_filter:
            if status_filter == "active":
                where.append("status != 'archived'")
            else:
                where.append("status = ?")
                params.append(status_filter)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cursor_obj = conn.execute(
            f"""
            SELECT id, title, owner_id, status, weight, is_core, updated_at, memory_type
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
                "owner_id": r[2],
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

        def _write(c):
            results.clear()
            for req in archive_requests:
                eid = req if isinstance(req, str) else req.get("entity_id")
                owner = req.get("owner_id") if isinstance(req, dict) else None
                res = lifecycle.archive_memory(
                    entity_id=eid, owner_id=owner, db_connection=conn, _in_transaction=True
                )
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk archive aborted (all-or-nothing): {res}")
                results.append({"status": "success", "entity_id": eid, "result": res})

        write_transaction_retrying(conn, _write)
        return results
    except Exception as e:
        logger.error("Error in bulk archive memory (batch rolled back, no items archived): %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
