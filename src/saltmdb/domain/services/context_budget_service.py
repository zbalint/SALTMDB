"""Token-budget packing for graph-aware context retrieval."""

import logging
import sqlite3
from typing import Any

from saltmdb.config import (
    CONTEXT_BUDGET_DEFAULT_TOKENS,
    CONTEXT_BUDGET_MAX_TOKENS,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.embedding_service import get_model

logger = logging.getLogger(__name__)


def pack_context_budget(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    conflict_sets_result: dict[str, Any],
    *,
    budget_tokens: int | None = None,
    orphan_community_entity_ids: set[str] | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Pack primary and expansion candidates against a real-token budget."""
    effective_budget = min(
        budget_tokens if budget_tokens is not None else CONTEXT_BUDGET_DEFAULT_TOKENS,
        CONTEXT_BUDGET_MAX_TOKENS,
    )

    primary_ids = [hit["id"] for hit in primary_hits]
    expansion_ids = [
        candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
    ]
    conflict_only_ids = {
        member["id"]
        for conflict_set in conflict_sets_result["conflict_sets"]
        for member in conflict_set["members"]
        if member["inclusion"] == "conflict_only"
    }
    orphan_community_ids: set[str] = orphan_community_entity_ids or set()

    if not primary_ids and not expansion_ids and not conflict_only_ids and not orphan_community_ids:
        return {
            "packed_entity_ids": {"primary": [], "expansion": []},
            "dropped_entity_ids": {"primary": [], "expansion": []},
            "conflict_only_entity_ids": [],
            "token_counts": {},
            "budget": {
                "unit": "tokens",
                "limit": effective_budget,
                "used": 0,
                "primary_truncated": False,
                "primary_dropped_count": 0,
                "expansion_truncated": False,
                "expansion_dropped_count": 0,
                "conflict_reserve_tokens_used": 0,
                "orphan_community_reserve_tokens_used": 0,
            },
        }

    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        all_ids = set(primary_ids) | set(expansion_ids) | conflict_only_ids | orphan_community_ids
        placeholders = ",".join("?" for _ in all_ids)
        rows = conn.execute(
            f"SELECT id, full_content FROM entities WHERE id IN ({placeholders})", tuple(all_ids)
        ).fetchall()
        content_by_id = {row[0]: row[1] for row in rows}

        token_counts: dict[str, int] = {}
        model = get_model()
        for entity_id in all_ids:
            text = content_by_id.get(entity_id, "")
            token_counts[entity_id] = 0 if text == "" else model.token_count(text)

        used = 0
        primary_packed: list[str] = []
        primary_dropped: list[str] = []
        expansion_packed: list[str] = []
        expansion_dropped: list[str] = []
        for entity_id in primary_ids:
            cost = token_counts[entity_id]
            if used + cost <= effective_budget:
                primary_packed.append(entity_id)
                used += cost
            else:
                primary_dropped.append(entity_id)
        for entity_id in expansion_ids:
            cost = token_counts[entity_id]
            if used + cost <= effective_budget:
                expansion_packed.append(entity_id)
                used += cost
            else:
                expansion_dropped.append(entity_id)

        conflict_reserve_tokens_used = sum(
            token_counts[entity_id] for entity_id in conflict_only_ids
        )
        orphan_community_reserve_tokens_used = sum(
            token_counts[entity_id] for entity_id in orphan_community_ids
        )
        return {
            "packed_entity_ids": {"primary": primary_packed, "expansion": expansion_packed},
            "dropped_entity_ids": {"primary": primary_dropped, "expansion": expansion_dropped},
            "conflict_only_entity_ids": sorted(conflict_only_ids),
            "token_counts": token_counts,
            "budget": {
                "unit": "tokens",
                "limit": effective_budget,
                "used": used,
                "primary_truncated": len(primary_dropped) > 0,
                "primary_dropped_count": len(primary_dropped),
                "expansion_truncated": len(expansion_dropped) > 0,
                "expansion_dropped_count": len(expansion_dropped),
                "conflict_reserve_tokens_used": conflict_reserve_tokens_used,
                "orphan_community_reserve_tokens_used": orphan_community_reserve_tokens_used,
            },
        }
    finally:
        if should_close:
            close_connection(conn)
