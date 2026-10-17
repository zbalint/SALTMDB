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
from saltmdb.domain.services.relation_service import _get_lineage_raw

logger = logging.getLogger(__name__)


def classify_contradicts_components(  # noqa: C901
    contradicts_edges: list[dict[str, Any]],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Group contradicts_edges into connected components via union-find and classify each as
    lifecycle-resolved or unresolved.

    Returns one entry per contradicts-connected component:
    {"member_ids": set[str], "edges": list[dict], "resolved": bool}

    An empty contradicts_edges list returns [] immediately without opening a connection.
    """
    if not contradicts_edges:
        return []

    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        pit = point_in_time or datetime.now(UTC).isoformat()

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
        placeholders = ",".join("?" for _ in member_ids)
        rows = conn.execute(
            f"SELECT id, status FROM entities WHERE id IN ({placeholders})",
            tuple(member_ids),
        ).fetchall()
        status_by_id = {row[0]: row[1] for row in rows}
        for entity_id in member_ids:
            status_by_id.setdefault(entity_id, "unknown")

        results: list[dict[str, Any]] = []
        for component in components_by_root.values():
            component_member_ids: set[str] = component["member_ids"]
            resolved = _component_lifecycle_resolved(component_member_ids, status_by_id, pit, conn)
            results.append(
                {
                    "member_ids": component_member_ids,
                    "edges": component["edges"],
                    "resolved": resolved,
                }
            )
        return results
    finally:
        if should_close:
            close_connection(conn)


def _component_lifecycle_resolved(
    member_ids: set[str],
    status_by_id: dict[str, str],
    point_in_time: str,
    conn: sqlite3.Connection,
) -> bool:
    """Check lifecycle connectivity with bounded multi-round BFS-to-closure."""
    anchor = min(member_ids)
    visited: set[str] = {anchor}
    frontier: set[str] = {anchor}
    for _ in range(SUPERSESSION_CHAIN_MAX_DEPTH):
        if not frontier:
            break
        newly_discovered: set[str] = set()
        for entity_id in frontier:
            ancestors_result = _get_lineage_raw(
                entity_id,
                direction="ancestors",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=point_in_time,
                db_connection=conn,
            )
            descendants_result = _get_lineage_raw(
                entity_id,
                direction="descendants",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=point_in_time,
                db_connection=conn,
            )
            if "error" in ancestors_result or "error" in descendants_result:
                logger.warning(
                    "Could not check lifecycle connectivity for entity %s in component %s: %s",
                    entity_id,
                    sorted(member_ids),
                    ancestors_result.get("error") or descendants_result.get("error"),
                )
                continue
            reached = {node["id"] for node in ancestors_result["nodes"]} | {
                node["id"] for node in descendants_result["nodes"]
            }
            newly_discovered |= reached - visited
        visited |= newly_discovered
        if member_ids <= visited:
            break
        frontier = newly_discovered
    if not (member_ids <= visited):
        return False

    non_archived = [m for m in member_ids if status_by_id.get(m, "unknown") != "archived"]
    return len(non_archived) == 1


def _build_unresolved_component(
    component: dict[str, Any],
    primary_hit_ids: set[str],
    primary_hit_score: dict[str, float],
    expansion_candidate_ids: set[str],
    entity_info: dict[str, dict[str, str | None]],
) -> dict[str, Any] | None:
    if component["resolved"]:
        return None
    member_ids: set[str] = component["member_ids"]
    members = []
    for entity_id in sorted(member_ids):
        if entity_id in primary_hit_ids:
            inclusion, title, status = "primary", None, None
        elif entity_id in expansion_candidate_ids:
            inclusion, title, status = "expansion", None, None
        else:
            inclusion = "conflict_only"
            title = entity_info[entity_id]["title"]
            status = entity_info[entity_id]["status"]
        members.append({"id": entity_id, "inclusion": inclusion, "title": title, "status": status})
    conflict_only_count = sum(
        entity_id not in primary_hit_ids and entity_id not in expansion_candidate_ids
        for entity_id in member_ids
    )
    tiebreak_score = max(
        primary_hit_score[entity_id] for entity_id in member_ids if entity_id in primary_hit_ids
    )
    return {
        "members": members,
        "edges": component["edges"],
        "conflict_only_count": conflict_only_count,
        "tiebreak_score": tiebreak_score,
        "member_sort_key": tuple(sorted(member_ids)),
    }


def _admit_conflict_components(
    unresolved_components: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    admitted = []
    admitted_count = 0
    dropped_count = 0
    for index, component in enumerate(unresolved_components):
        component_count = component["conflict_only_count"]
        if admitted_count + component_count > CONTEXT_EXPANSION_CONTRADICTS_CAP:
            dropped_count = len(unresolved_components) - index
            break
        admitted.append(component)
        admitted_count += component_count
    return admitted, dropped_count


def _open_conflict_connection(
    db_connection: sqlite3.Connection | None, db_path: str | None
) -> tuple[sqlite3.Connection, bool]:
    if db_connection:
        return db_connection, False
    return get_connection(db_path or get_db_path()), True


def _load_entity_info(
    conn: sqlite3.Connection, member_ids: set[str]
) -> dict[str, dict[str, str | None]]:
    if not member_ids:
        return {}
    placeholders = ",".join("?" for _ in member_ids)
    rows = conn.execute(
        f"SELECT id, title, status FROM entities WHERE id IN ({placeholders})",
        tuple(member_ids),
    ).fetchall()
    entity_info = {row[0]: {"title": row[1], "status": row[2]} for row in rows}
    for entity_id in member_ids:
        entity_info.setdefault(entity_id, {"title": "Unknown", "status": "unknown"})
    return entity_info


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

    conn, should_close = _open_conflict_connection(db_connection, db_path)

    try:
        pit = point_in_time or datetime.now(UTC).isoformat()
        primary_hit_ids = {hit["id"] for hit in primary_hits}
        primary_hit_score = {hit["id"]: hit["score"] for hit in primary_hits}
        expansion_candidate_ids = {
            candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
        }

        components = classify_contradicts_components(
            contradicts_edges, point_in_time=pit, db_connection=conn
        )
        member_ids = set().union(*(c["member_ids"] for c in components)) if components else set()
        entity_info = _load_entity_info(conn, member_ids)

        unresolved_components = [
            built
            for component in components
            if (
                built := _build_unresolved_component(
                    component,
                    primary_hit_ids,
                    primary_hit_score,
                    expansion_candidate_ids,
                    entity_info,
                )
            )
            is not None
        ]
        unresolved_components.sort(
            key=lambda component: (-component["tiebreak_score"], component["member_sort_key"])
        )
        admitted, dropped_count = _admit_conflict_components(unresolved_components)
        return {
            "conflict_sets": [
                {"members": component["members"], "edges": component["edges"]}
                for component in admitted
            ],
            "contradicts_cap": {
                "cap": CONTEXT_EXPANSION_CONTRADICTS_CAP,
                "eligible_count": len(unresolved_components),
                "truncated": dropped_count > 0,
                "dropped_count": dropped_count,
            },
        }
    finally:
        if should_close:
            close_connection(conn)
