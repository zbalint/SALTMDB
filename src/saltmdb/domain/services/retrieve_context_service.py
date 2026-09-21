"""Orchestration service for graph-aware, budget-bounded context retrieval."""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

from saltmdb.config import get_db_path
from saltmdb.db.connection import close_connection, get_connection

# Required by string-resolved mock.patch targets.
from saltmdb.domain.services import memory_service  # noqa: F401
from saltmdb.domain.services.conflict_set_service import assemble_conflict_sets
from saltmdb.domain.services.context_budget_service import pack_context_budget
from saltmdb.domain.services.orphan_community_service import find_orphan_community_matches
from saltmdb.domain.services.community_retrieval_service import seed_and_rank_communities
from saltmdb.domain.services.context_expansion_service import (
    PrimaryHit,
    expand_context_candidates,
)
from saltmdb.domain.services.lineage_assembly_service import assemble_lineage
from saltmdb.utils.envelope import error, rejected
from saltmdb.utils.text import resolve_entity_ref

logger = logging.getLogger(__name__)


def assemble_retrieve_context(  # noqa: C901, PLR0912, PLR0915
    entity_ids: list[str] | None = None,
    query: str | None = None,
    *,
    budget_tokens: int | None = None,
    strategy: str = "local",
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Assemble local graph-aware context from one point-in-time and one database connection.

    strategy="local" (default): entity_ids is the required anchor set -- one or more memory IDs
    the caller already has. strategy="global": query is the required anchor (unchanged; see
    _assemble_global_context). Both parameters default to None at the signature level only so
    one function can serve both strategies -- exactly one is semantically required depending on
    strategy, enforced by the explicit validation block below, not by the type signature itself.
    """
    if strategy == "global":
        if not query or not isinstance(query, str):
            return rejected(
                [error("VALIDATION_ERROR", 'query is required for strategy="global"', "query")]
            )
    else:
        if not entity_ids or not isinstance(entity_ids, list):
            return rejected(
                [
                    error(
                        "VALIDATION_ERROR",
                        "entity_ids is required and must be a non-empty list",
                        "entity_ids",
                    )
                ]
            )
        if not all(isinstance(item, str) and item for item in entity_ids):
            return rejected(
                [
                    error(
                        "VALIDATION_ERROR",
                        "entity_ids must be a list of non-empty strings",
                        "entity_ids",
                    )
                ]
            )

    should_close = False
    conn = db_connection
    if conn is None:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        if strategy == "global":
            assert query is not None  # narrowed by the validation block above
            return _assemble_global_context(query, budget_tokens, conn)
        pit = datetime.now(UTC).isoformat()

        resolved_hits: list[dict[str, Any]] = []
        unresolved_errors: list[dict[str, Any]] = []
        for raw_id in cast(list[str], entity_ids):
            resolved_id, candidates, truncated = resolve_entity_ref(conn, raw_id)
            if candidates:
                item = error(
                    "AMBIGUOUS_ID_PREFIX",
                    f"ID prefix '{raw_id}' matches multiple memories; provide a longer prefix or full UUID.",
                    "entity_ids",
                )
                item["candidates"] = candidates
                if truncated:
                    item["candidates_truncated"] = True
                unresolved_errors.append(item)
                continue
            if not resolved_id:
                unresolved_errors.append(
                    error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{raw_id}'.",
                        "entity_ids",
                    )
                )
                continue
            row = conn.execute(
                "SELECT title, memory_type FROM entities WHERE id = ?", (resolved_id,)
            ).fetchone()
            if row is None:
                unresolved_errors.append(
                    error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{raw_id}'.",
                        "entity_ids",
                    )
                )
                continue
            resolved_hits.append({"id": resolved_id, "title": row[0], "memory_type": row[1]})

        if unresolved_errors:
            return rejected(unresolved_errors)

        # No ranking exists for a caller-supplied anchor. A uniform placeholder score makes the
        # tiebreak term in context_expansion_service.py and conflict_set_service.py's own
        # max(...) comparisons constant across every candidate, so both sorts fall through
        # unchanged to their next already-deterministic key -- see spec Why section.
        primary_hits = [{"id": hit["id"], "score": 1.0} for hit in resolved_hits]
        primary_meta = {
            hit["id"]: {"title": hit["title"], "memory_type": hit["memory_type"]}
            for hit in resolved_hits
        }
        original_rank = {hit["id"]: index + 1 for index, hit in enumerate(resolved_hits)}
        primary_hit_ids_set = {hit["id"] for hit in primary_hits}

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
        expansion_candidate_ids = {
            candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
        }
        conflict_only_ids = {
            member["id"]
            for conflict_set in conflict_sets_result["conflict_sets"]
            for member in conflict_set["members"]
            if member["inclusion"] == "conflict_only"
        }
        excluded_for_orphan_lookup = (
            set(primary_hit_ids_set) | expansion_candidate_ids | conflict_only_ids
        )
        orphan_result = find_orphan_community_matches(
            primary_hits,
            excluded_for_orphan_lookup,
            db_connection=conn,
        )
        orphan_community_entity_ids = {
            match["entity_id"] for match in orphan_result["orphan_community_matches"]
        }
        budget_result = pack_context_budget(
            expansion_result,
            primary_hits,
            conflict_sets_result,
            budget_tokens=budget_tokens,
            orphan_community_entity_ids=orphan_community_entity_ids,
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

        needs_memory_type = (
            set(final_expansion_ids)
            | set(budget_result["conflict_only_entity_ids"])
            | orphan_community_entity_ids
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
                        {"reason": "caller_supplied_anchor", "rank": original_rank[entity_id]}
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
        orphan_title_by_id = {
            match["entity_id"]: match["title"]
            for match in orphan_result["orphan_community_matches"]
        }
        orphan_provenance_by_id = {
            match["entity_id"]: {
                "reason": "orphan_community_assignment",
                "orphan_entity_id": match["orphan_entity_id"],
                "community_id": match["community_id"],
                "similarity": match["similarity"],
            }
            for match in orphan_result["orphan_community_matches"]
        }
        for entity_id in orphan_community_entity_ids:
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": orphan_title_by_id[entity_id],
                    "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                    "inclusion": "orphan_community",
                    "retrieval_provenance": [orphan_provenance_by_id[entity_id]],
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
                "orphan_community_reserve": orphan_result["orphan_community_cap"],
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
                "orphan_community_reserve_tokens_used": budget_result["budget"][
                    "orphan_community_reserve_tokens_used"
                ],
            },
        }

        return {
            "entity_ids": entity_ids,
            "memories": memories,
            "edges": edges,
            "lineage": lineage_result,
            "conflict_sets": conflict_sets_output,
            "metadata": metadata,
        }
    finally:
        if should_close:
            close_connection(conn)


def _assemble_global_context(
    query: str, budget_tokens: int | None, conn: sqlite3.Connection
) -> dict[str, Any]:
    seed_result = seed_and_rank_communities(query, db_connection=conn)
    representative_entity_ids = {
        match["entity_id"] for match in seed_result["representative_matches"]
    }
    member_entity_ids = [match["entity_id"] for match in seed_result["member_matches"]]

    budget_result = pack_context_budget(
        {"expansion_candidates": []},
        [],
        {"conflict_sets": []},
        budget_tokens=budget_tokens,
        community_member_ids=member_entity_ids,
        community_representative_ids=representative_entity_ids,
        db_connection=conn,
    )

    title_by_id = {
        match["entity_id"]: match["title"]
        for match in seed_result["representative_matches"] + seed_result["member_matches"]
    }
    community_id_by_id = {
        match["entity_id"]: match["community_id"]
        for match in seed_result["representative_matches"] + seed_result["member_matches"]
    }
    similarity_by_id = {
        match["entity_id"]: match["seed_similarity"]
        for match in seed_result["representative_matches"]
    } | {match["entity_id"]: match["similarity"] for match in seed_result["member_matches"]}

    surfaced_ids = set(budget_result["community_representative_entity_ids"]) | set(
        budget_result["packed_entity_ids"]["community_member"]
    )
    memory_type_by_id: dict[str, str] = {}
    if surfaced_ids:
        placeholders = ",".join("?" for _ in surfaced_ids)
        rows = conn.execute(
            f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})",
            tuple(surfaced_ids),
        ).fetchall()
        memory_type_by_id = {row[0]: row[1] for row in rows}

    memories: list[dict[str, Any]] = []
    for entity_id in budget_result["community_representative_entity_ids"]:
        memories.append(
            {
                "entity_id": entity_id,
                "title": title_by_id[entity_id],
                "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                "inclusion": "community_representative",
                "retrieval_provenance": [
                    {
                        "reason": "community_representative",
                        "community_id": community_id_by_id[entity_id],
                        "seed_similarity": similarity_by_id[entity_id],
                    }
                ],
            }
        )
    for entity_id in budget_result["packed_entity_ids"]["community_member"]:
        memories.append(
            {
                "entity_id": entity_id,
                "title": title_by_id[entity_id],
                "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                "inclusion": "community_member",
                "retrieval_provenance": [
                    {
                        "reason": "community_member",
                        "community_id": community_id_by_id[entity_id],
                        "similarity": similarity_by_id[entity_id],
                    }
                ],
            }
        )

    return {
        "query": query,
        "memories": memories,
        "edges": [],
        "lineage": {},
        "conflict_sets": [],
        "metadata": {
            "strategy": "global",
            "community": {
                "seed_top_k": seed_result["seed_cap"],
                "representative_reserve": seed_result["representative_reserve"],
                "member_pool": seed_result["member_pool"],
            },
            "budget": budget_result["budget"],
        },
    }
