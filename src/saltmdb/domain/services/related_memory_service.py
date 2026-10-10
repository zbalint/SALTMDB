"""Answer-side related-memory retrieval for the read-only CLI RPC."""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, cast

from saltmdb.config import (
    CROSS_ENCODER_MAX_QUERY_CHARS,
    DEDUP_CROSS_ENCODER_MODEL,
    DEDUP_FTS_MAX_TERMS,
    RELATED_MEMORIES_DEFAULT_LIMIT,
    RELATED_MEMORIES_FTS_SHARE,
    RELATED_MEMORIES_MAX_SEGMENTS,
    RELATED_MEMORIES_MIN_SCORE,
    RELATED_MEMORIES_MIN_SEGMENT_CHARS,
    RELATED_MEMORIES_POOL_PER_SEGMENT,
    RELATED_MEMORIES_SEMANTIC_MAX_REQUEST,
)
from saltmdb.domain.services import reranker_service
from saltmdb.domain.services.memory_service import ranking, search_primitives

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def split_segments(text: str) -> list[str]:
    """Remove non-prose noise and select bounded answer segments deterministically."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)

    cleaned = _UUID_RE.sub("", "\n".join(lines))
    paragraphs: list[tuple[int, str]] = []
    for position, paragraph in enumerate(re.split(r"\n\s*\n", cleaned)):
        normalized = " ".join(paragraph.split())
        if len(normalized) < RELATED_MEMORIES_MIN_SEGMENT_CHARS:
            continue
        tokens = normalized.split()
        if tokens and all(token.startswith(("http://", "https://")) for token in tokens):
            continue
        paragraphs.append((position, normalized))

    if len(paragraphs) <= RELATED_MEMORIES_MAX_SEGMENTS:
        selected = paragraphs
    else:
        tail = paragraphs[-2:]
        tail_positions = {position for position, _ in tail}
        longest = sorted(
            (item for item in paragraphs[:-2] if item[0] not in tail_positions),
            key=lambda item: (-len(item[1]), item[0]),
        )[: RELATED_MEMORIES_MAX_SEGMENTS - 2]
        selected_positions = {position for position, _ in (*tail, *longest)}
        selected = [item for item in paragraphs if item[0] in selected_positions]

    return [segment[:CROSS_ENCODER_MAX_QUERY_CHARS] for _, segment in selected]


def _where_clauses(exclude_ids: list[str], agent_id: str | None) -> tuple[list[str], list[str]]:
    clauses = ["e.status != 'archived'"]
    params: list[str] = []
    if exclude_ids:
        placeholders = ",".join("?" for _ in exclude_ids)
        clauses.append(f"e.id NOT IN ({placeholders})")
        params.extend(exclude_ids)
    if agent_id:
        clauses.append("(e.agent_id = ? OR e.scope = 'shared')")
        params.append(agent_id)
    else:
        clauses.append("e.scope = 'shared'")
    return clauses, params


def _fts_candidates(
    conn: sqlite3.Connection,
    segment: str,
    where_clauses: list[str],
    params: list[str],
) -> list[tuple[str, int, float | None]]:
    terms = search_primitives.build_fts_terms(segment, DEDUP_FTS_MAX_TERMS)
    if not terms:
        return []
    try:
        rows = cast(
            list[Any],
            search_primitives._run_fts_search(
                conn,
                " ".join(terms),
                where_clauses,
                params,
                RELATED_MEMORIES_FTS_SHARE,
                0,
                or_only=True,
            ),
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.warning("Related-memory FTS search unavailable: %s", exc)
        return []
    return [(row[0], index, None) for index, row in enumerate(rows, start=1)]


def _semantic_candidates(
    segment: str,
    where_clauses: list[str],
    params: list[str],
    db_path: str,
    exclude_count: int,
) -> list[tuple[str, float]]:
    request_size = min(
        RELATED_MEMORIES_POOL_PER_SEGMENT + exclude_count,
        RELATED_MEMORIES_SEMANTIC_MAX_REQUEST,
    )
    try:
        return search_primitives.semantic_search(
            segment,
            where_clauses,
            params,
            request_size,
            db_path,
            0,
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        logger.warning("Related-memory semantic search unavailable: %s", exc)
        return []


def _fetch_pool(
    conn: sqlite3.Connection,
    ordered_ids: list[str],
    metadata: dict[str, dict[str, Any]],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    if not ordered_ids:
        return []
    superseded_ids = ranking._compute_superseded_ids_bitemporal(ordered_ids, conn)
    visible_ids = [entity_id for entity_id in ordered_ids if entity_id not in superseded_ids]
    if not visible_ids:
        return []
    placeholders = ",".join("?" for _ in visible_ids)
    rows = conn.execute(
        f"SELECT id, title, full_content FROM entities WHERE id IN ({placeholders})", visible_ids
    ).fetchall()
    rows_by_id = {row[0]: row for row in rows}
    return [
        (entity_id, rows_by_id[entity_id][1], rows_by_id[entity_id][2], metadata[entity_id])
        for entity_id in visible_ids
        if entity_id in rows_by_id
    ]


def find_related_memories(  # noqa: C901
    conn: sqlite3.Connection,
    db_path: str,
    text: str,
    *,
    limit: int | None = None,
    min_score: float | None = None,
    exclude_ids: list[str] | None = None,
    agent_id: str | None = None,
    with_all: bool = False,
) -> list[dict[str, Any]]:
    """Return cross-encoder-scored memories related to answer text."""
    effective_limit = RELATED_MEMORIES_DEFAULT_LIMIT if limit is None else limit
    effective_min_score = RELATED_MEMORIES_MIN_SCORE if min_score is None else min_score
    clean_excludes = [str(entity_id) for entity_id in (exclude_ids or []) if str(entity_id)]
    segments = split_segments(text)
    if not segments:
        return []

    merged: dict[str, dict[str, Any]] = {}
    for segment_index, segment in enumerate(segments):
        where_clauses, params = _where_clauses(clean_excludes, agent_id)
        fts_rows = _fts_candidates(conn, segment, where_clauses, params)
        semantic_rows = _semantic_candidates(
            segment, where_clauses, params, db_path, len(clean_excludes)
        )
        candidate_ids: list[str] = []
        metadata: dict[str, dict[str, Any]] = {}
        for entity_id, fts_rank, _ in fts_rows:
            if entity_id in metadata:
                continue
            candidate_ids.append(entity_id)
            metadata[entity_id] = {
                "fts_rank": fts_rank,
                "semantic_distance": None,
            }
        for entity_id, distance in semantic_rows:
            if entity_id in metadata:
                metadata[entity_id]["semantic_distance"] = float(distance)
                continue
            if len(candidate_ids) >= RELATED_MEMORIES_POOL_PER_SEGMENT:
                break
            candidate_ids.append(entity_id)
            metadata[entity_id] = {"fts_rank": None, "semantic_distance": float(distance)}
        pool = _fetch_pool(conn, candidate_ids, metadata)
        if not pool:
            continue
        candidate_texts = [f"{title} {content}" for _, title, content, _ in pool]
        scores = reranker_service.score_pairs(
            segment,
            candidate_texts,
            model_name=DEDUP_CROSS_ENCODER_MODEL,
            candidate_cap=RELATED_MEMORIES_POOL_PER_SEGMENT,
        )
        if scores is None:
            continue
        for (entity_id, title, _content, candidate_metadata), score in zip(pool, scores):
            current = merged.get(entity_id)
            if current is None or score > current["score"]:
                merged[entity_id] = {
                    "id": entity_id,
                    "title": title,
                    "score": float(score),
                    "segment": segment_index,
                    **candidate_metadata,
                }

    ordered = sorted(merged.values(), key=lambda item: (-item["score"], item["id"]))
    if with_all:
        return ordered
    filtered = [item for item in ordered if item["score"] >= effective_min_score]
    return filtered[:effective_limit]
