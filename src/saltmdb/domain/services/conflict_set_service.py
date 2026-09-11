"""Contradiction conflict-set assembly for context retrieval."""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from saltmdb.config import (
    CONTEXT_EXPANSION_CONTRADICTS_CAP,
    SUPERSESSION_CHAIN_MAX_DEPTH,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.relation_service import get_lineage

logger = logging.getLogger(__name__)


def assemble_conflict_sets(  # noqa: C901, PLR0912, PLR0915
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Group, classify, and cap the contradicts edges surfaced by context expansion."""
    contradicts_edges = expansion_result["contradicts_edges"]
    if not contradicts_edges:
        return {
            "conflict_sets": [],
            "contradicts_cap": {
                "cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
                "eligible_count": 0,
                "truncated": False,
                "dropped_count": 0,
            },
        }

    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        pit = point_in_time or datetime.now(UTC).isoformat()
        primary_hit_ids = {hit["id"] for hit in primary_hits}
        primary_hit_score = {hit["id"]: hit["score"] for hit in primary_hits}
        expansion_candidate_ids = {
            candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
        }

        parent: dict[str, str] = {}

        def find(entity_id: str) -> str:
            if entity_id not in parent:
                parent[entity_id] = entity_id
            if parent[entity_id] != entity_id:
                parent[entity_id] = find(parent[entity_id])
            return parent[entity_id]

        def union(first: str, second: str) -> None:
            first_root = find(first)
            second_root = find(second)
            if first_root != second_root:
                parent[second_root] = first_root

        for edge in contradicts_edges:
            union(edge["source_id"], edge["target_id"])

        components_by_root: dict[str, dict[str, Any]] = {}
        for edge in contradicts_edges:
            source_id = edge["source_id"]
            target_id = edge["target_id"]
            root = find(source_id)
            component = components_by_root.setdefault(
                root,
                {"member_ids": set(), "edges": []},
            )
            component["member_ids"].update((source_id, target_id))
            component["edges"].append(edge)

        member_ids = set(parent)
        entity_info: dict[str, dict[str, str | None]] = {}
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            rows = conn.execute(
                f"SELECT id, title, status FROM entities WHERE id IN ({placeholders})",
                tuple(member_ids),
            ).fetchall()
            entity_info = {row[0]: {"title": row[1], "status": row[2]} for row in rows}
            for entity_id in member_ids:
                entity_info.setdefault(
                    entity_id,
                    {"title": "Unknown", "status": "unknown"},
                )

        unresolved_components: list[dict[str, Any]] = []
        for component in components_by_root.values():
            component_member_ids: set[str] = component["member_ids"]
            anchor = min(component_member_ids)
            ancestors_result = get_lineage(
                anchor,
                direction="ancestors",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=pit,
                db_connection=conn,
            )
            descendants_result = get_lineage(
                anchor,
                direction="descendants",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=pit,
                db_connection=conn,
            )

            if "error" in ancestors_result or "error" in descendants_result:
                error = ancestors_result.get("error") or descendants_result.get("error")
                logger.warning(
                    "Could not check lifecycle connectivity for conflict component %s: %s",
                    sorted(component_member_ids),
                    error,
                )
            else:
                anchor_lineage_ids = {node["id"] for node in ancestors_result["nodes"]} | {
                    node["id"] for node in descendants_result["nodes"]
                }
                if component_member_ids <= anchor_lineage_ids:
                    non_archived = [
                        entity_id
                        for entity_id in component_member_ids
                        if entity_info[entity_id]["status"] != "archived"
                    ]
                    if len(non_archived) == 1:
                        continue

            members: list[dict[str, Any]] = []
            for entity_id in sorted(component_member_ids):
                if entity_id in primary_hit_ids:
                    inclusion = "primary"
                    title = None
                    status = None
                elif entity_id in expansion_candidate_ids:
                    inclusion = "expansion"
                    title = None
                    status = None
                else:
                    inclusion = "conflict_only"
                    title = entity_info[entity_id]["title"]
                    status = entity_info[entity_id]["status"]
                members.append(
                    {
                        "id": entity_id,
                        "inclusion": inclusion,
                        "title": title,
                        "status": status,
                    }
                )

            conflict_only_count = sum(
                entity_id not in primary_hit_ids and entity_id not in expansion_candidate_ids
                for entity_id in component_member_ids
            )
            tiebreak_score = max(
                primary_hit_score[entity_id]
                for entity_id in component_member_ids
                if entity_id in primary_hit_ids
            )
            unresolved_components.append(
                {
                    "members": members,
                    "edges": component["edges"],
                    "conflict_only_count": conflict_only_count,
                    "tiebreak_score": tiebreak_score,
                    "member_sort_key": tuple(sorted(component_member_ids)),
                }
            )

        unresolved_components.sort(
            key=lambda component: (
                -component["tiebreak_score"],
                component["member_sort_key"],
            )
        )
        eligible_count = len(unresolved_components)
        admitted: list[dict[str, Any]] = []
        admitted_conflict_only_count = 0
        dropped_count = 0
        for index, component in enumerate(unresolved_components):
            component_count = component["conflict_only_count"]
            if admitted_conflict_only_count + component_count > CONTEXT_EXPANSION_CONTRADICTS_CAP:
                dropped_count = eligible_count - index
                break
            admitted.append(component)
            admitted_conflict_only_count += component_count

        conflict_sets = [
            {"members": component["members"], "edges": component["edges"]} for component in admitted
        ]
        return {
            "conflict_sets": conflict_sets,
            "contradicts_cap": {
                "cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
                "eligible_count": eligible_count,
                "truncated": dropped_count > 0,
                "dropped_count": dropped_count,
            },
        }
    finally:
        if should_close:
            close_connection(conn)
