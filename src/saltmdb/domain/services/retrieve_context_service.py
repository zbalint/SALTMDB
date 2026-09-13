"""Orchestration service for graph-aware, budget-bounded context retrieval."""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, Callable, cast

from saltmdb.config import get_db_path
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services import memory_service
from saltmdb.domain.services.conflict_set_service import assemble_conflict_sets
from saltmdb.domain.services.context_budget_service import pack_context_budget
from saltmdb.domain.services.context_expansion_service import (
    PrimaryHit,
    expand_context_candidates,
)
from saltmdb.domain.services.lineage_assembly_service import assemble_lineage

logger = logging.getLogger(__name__)


def assemble_retrieve_context(  # noqa: PLR0912, PLR0915
    query: str,
    owner_id: str | None,
    *,
    limit: int | None = None,
    budget_tokens: int | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Assemble local graph-aware context from one point-in-time and one database connection."""
    should_close = False
    conn = db_connection
    if conn is None:
        conn = get_connection(db_path or get_db_path())
        should_close = True
    effective_db_path: str | None
    if db_path is not None:
        effective_db_path = db_path
    elif db_connection is not None:
        row = cast(tuple[int, str, str], conn.execute("PRAGMA database_list").fetchone())
        effective_db_path = row[2] or None
    else:
        effective_db_path = db_path or get_db_path()

    try:
        pit = datetime.now(UTC).isoformat()
        search_fn = cast(
            Callable[..., list[dict[str, Any]] | dict[str, Any]],
            memory_service.search_memory,
        )
        search_result = search_fn(
            owner_id=owner_id,
            query_keywords=query,
            limit=limit if limit is not None else 5,
            context_id=None,
            agent_session_id=None,
            tags_filter=None,
            memory_type_filter=None,
            is_core=None,
            cursor=None,
            mode="strict",
            include_related=False,
            return_diagnostics=False,
            db_connection=conn,
            db_path=effective_db_path,
        )
        search_hits = cast(list[dict[str, Any]], search_result)
        if search_hits and "id" not in search_hits[0]:
            logger.warning(
                "search_memory reported an internal error for query %r: %s",
                query,
                search_hits[0].get("error"),
            )
            search_hits = []

        primary_hits = [{"id": hit["id"], "score": hit["score"]} for hit in search_hits]
        primary_meta = {
            hit["id"]: {"title": hit["title"], "memory_type": hit["memory_type"]}
            for hit in search_hits
        }
        original_rank = {hit["id"]: index + 1 for index, hit in enumerate(search_hits)}

        expansion_result = expand_context_candidates(
            cast(list[PrimaryHit], primary_hits),
            point_in_time=pit,
            db_connection=conn,
        )
        conflict_sets_result = assemble_conflict_sets(
            expansion_result,
            primary_hits,
            point_in_time=pit,
            db_connection=conn,
        )
        budget_result = pack_context_budget(
            expansion_result,
            primary_hits,
            conflict_sets_result,
            budget_tokens=budget_tokens,
            db_connection=conn,
        )
        lineage_result = assemble_lineage(
            expansion_result,
            primary_hits,
            point_in_time=pit,
            db_connection=conn,
        )

        member_to_conflict_set: dict[str, str] = {}
        for index, conflict_set in enumerate(conflict_sets_result["conflict_sets"]):
            conflict_set_id = f"cs-{index + 1}"
            for member in conflict_set["members"]:
                member_to_conflict_set[member["id"]] = conflict_set_id

        recovered_primary = [
            entity_id
            for entity_id in budget_result["dropped_entity_ids"]["primary"]
            if entity_id in member_to_conflict_set
        ]
        recovered_expansion = [
            entity_id
            for entity_id in budget_result["dropped_entity_ids"]["expansion"]
            if entity_id in member_to_conflict_set
        ]
        true_primary_dropped = [
            entity_id
            for entity_id in budget_result["dropped_entity_ids"]["primary"]
            if entity_id not in member_to_conflict_set
        ]
        true_expansion_dropped = [
            entity_id
            for entity_id in budget_result["dropped_entity_ids"]["expansion"]
            if entity_id not in member_to_conflict_set
        ]
        final_primary_ids = budget_result["packed_entity_ids"]["primary"] + recovered_primary
        final_expansion_ids = budget_result["packed_entity_ids"]["expansion"] + recovered_expansion
        recovered_tokens_used = sum(
            budget_result["token_counts"][entity_id]
            for entity_id in recovered_primary + recovered_expansion
        )

        needs_memory_type = set(final_expansion_ids) | set(
            budget_result["conflict_only_entity_ids"]
        )
        memory_type_by_id: dict[str, str] = {}
        if needs_memory_type:
            placeholders = ",".join("?" for _ in needs_memory_type)
            rows = conn.execute(
                f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})",
                tuple(needs_memory_type),
            ).fetchall()
            memory_type_by_id = {row[0]: row[1] for row in rows}

        conflict_only_meta = {
            member["id"]: {"title": member["title"], "status": member["status"]}
            for conflict_set in conflict_sets_result["conflict_sets"]
            for member in conflict_set["members"]
            if member["inclusion"] == "conflict_only"
        }

        memories: list[dict[str, Any]] = []
        for entity_id in final_primary_ids:
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": primary_meta[entity_id]["title"],
                    "memory_type": primary_meta[entity_id]["memory_type"],
                    "inclusion": "primary",
                    "retrieval_provenance": [
                        {"reason": "primary_search", "rank": original_rank[entity_id]}
                    ],
                }
            )

        candidates_by_id = {
            candidate["entity_id"]: candidate
            for candidate in expansion_result["expansion_candidates"]
        }
        for entity_id in final_expansion_ids:
            candidate = candidates_by_id[entity_id]
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": candidate["title"],
                    "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                    "inclusion": "expansion",
                    "retrieval_provenance": candidate["retrieval_provenance"],
                }
            )

        for entity_id in budget_result["conflict_only_entity_ids"]:
            meta = conflict_only_meta[entity_id]
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": meta["title"],
                    "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                    "inclusion": "conflict_only",
                    "retrieval_provenance": [
                        {
                            "reason": "contradicts_force_include",
                            "conflict_set_id": member_to_conflict_set[entity_id],
                        }
                    ],
                }
            )
        surfaced_ids = {memory["entity_id"] for memory in memories}
        lineage_result = {
            head_id: entry for head_id, entry in lineage_result.items() if head_id in surfaced_ids
        }

        edges = expansion_result["in_network_edges"]
        conflict_sets_output = [
            {
                "id": f"cs-{index + 1}",
                "members": conflict_set["members"],
                "edges": conflict_set["edges"],
            }
            for index, conflict_set in enumerate(conflict_sets_result["conflict_sets"])
        ]
        metadata = {
            "strategy": "local",
            "fan_out": {
                **expansion_result["fan_out"],
                "conflict_reserve": conflict_sets_result["contradicts_cap"],
            },
            "budget": {
                "unit": budget_result["budget"]["unit"],
                "limit": budget_result["budget"]["limit"],
                "used": budget_result["budget"]["used"] + recovered_tokens_used,
                "primary_truncated": len(true_primary_dropped) > 0,
                "primary_dropped_count": len(true_primary_dropped),
                "expansion_truncated": len(true_expansion_dropped) > 0,
                "expansion_dropped_count": len(true_expansion_dropped),
                "conflict_reserve_tokens_used": (
                    budget_result["budget"]["conflict_reserve_tokens_used"] + recovered_tokens_used
                ),
            },
        }

        return {
            "query": query,
            "memories": memories,
            "edges": edges,
            "lineage": lineage_result,
            "conflict_sets": conflict_sets_output,
            "metadata": metadata,
        }
    finally:
        if should_close:
            close_connection(conn)
