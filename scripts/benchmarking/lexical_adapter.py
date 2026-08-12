"""Raw production-BM25 and lifecycle-head adapters for frozen bakeoff corpora."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from saltmdb.domain.services.memory_service import _run_fts_search


class LexicalAdapterError(ValueError):
    """The frozen lexical channel returned malformed or ambiguous evidence."""


@dataclass(frozen=True, slots=True)
class LexicalHit:
    entity_id: str
    raw_bm25_score: float
    rank: int
    used_or_fallback: bool


def bm25_search(conn: Any, query: str, *, limit: int = 20) -> list[LexicalHit]:
    """Execute the production FTS query while retaining its raw BM25 score and rank."""
    sanitized = " ".join(query.split())
    if not sanitized or limit <= 0:
        raise LexicalAdapterError("BM25 query must be non-empty and limit positive")
    rows, used_fallback = _run_fts_search(
        conn,
        sanitized,
        ["e.status != 'archived'"],
        [],
        limit,
        0,
        return_fallback_flag=True,
    )
    hits = []
    for rank, row in enumerate(rows, 1):
        entity_id = row[0]
        raw_score = float(row[5])
        if not isinstance(entity_id, str) or not entity_id or not math.isfinite(raw_score):
            raise LexicalAdapterError("BM25 returned a malformed row")
        hits.append(LexicalHit(entity_id, raw_score, rank, used_fallback))
    return hits


def _live_successors(conn: Any, entity_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT r.source_id
        FROM relations r
        JOIN entities e ON e.id = r.source_id
        WHERE r.target_id = ? AND r.predicate = 'supersedes'
          AND e.status != 'archived'
          AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime('now'))
          AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
          AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime('now'))
          AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime('now'))
        ORDER BY r.source_id
        """,
        (entity_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def resolve_current_head(conn: Any, entity_id: str, *, max_hops: int = 100) -> str | None:
    """Resolve one non-forking supersedes chain; ambiguity/cycles fail closed."""
    if not entity_id or max_hops <= 0:
        raise LexicalAdapterError("lifecycle entity/max_hops is invalid")
    current = entity_id
    visited = {current}
    for _ in range(max_hops):
        successors = _live_successors(conn, current)
        if not successors:
            row = conn.execute(
                "SELECT 1 FROM entities WHERE id = ? AND status != 'archived'", (current,)
            ).fetchone()
            return current if row else None
        if len(successors) != 1 or successors[0] in visited:
            return None
        current = successors[0]
        visited.add(current)
    return None


def include_current_heads(
    conn: Any, ordered_ids: Sequence[str], *, limit: int | None = None
) -> list[str]:
    """Stable candidate inclusion: keep each match, then its distinct current head."""
    result: list[str] = []
    seen: set[str] = set()
    for entity_id in ordered_ids:
        for candidate in (entity_id, resolve_current_head(conn, entity_id)):
            if candidate and candidate not in seen:
                result.append(candidate)
                seen.add(candidate)
                if limit is not None and len(result) >= limit:
                    return result
    return result
