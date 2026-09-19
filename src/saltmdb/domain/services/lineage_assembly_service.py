"""Lineage assembly for context retrieval."""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from saltmdb.config import (
    LINEAGE_HISTORICAL_CAP,
    SUPERSESSION_CHAIN_MAX_DEPTH,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.conflict_set_service import classify_contradicts_components
from saltmdb.domain.services.relation_service import get_lineage

logger = logging.getLogger(__name__)


def _sort_ancestor_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort nodes by depth, updated time descending, and id ascending."""
    by_id = sorted(nodes, key=lambda node: node["id"])
    by_updated_at = sorted(
        by_id,
        key=lambda node: node["updated_at"] or "",
        reverse=True,
    )
    return sorted(by_updated_at, key=lambda node: node["depth"])


def _add_contradiction_flags(
    entry: dict[str, Any], entity_id: str, contradiction_peers: dict[str, set[str]]
) -> dict[str, Any]:
    peers = contradiction_peers.get(entity_id)
    if peers:
        entry["was_flagged_contradiction"] = True
        entry["contradicted_with"] = sorted(peers)
    return entry


def assemble_lineage(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Assemble capped supersession history for every surfaced head candidate."""
    head_candidate_ids: set[str] = {hit["id"] for hit in primary_hits}
    head_candidate_ids.update(
        candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
    )
    if not head_candidate_ids:
        return {}

    should_close = False
    conn = db_connection
    if conn is None:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        pit = point_in_time or datetime.now(UTC).isoformat()
        components = classify_contradicts_components(
            expansion_result["contradicts_edges"],
            point_in_time=pit,
            db_connection=conn,
        )
        contradiction_peers: dict[str, set[str]] = {}
        for component in components:
            if component.get("resolved") is not True:
                continue
            for edge in component["edges"]:
                source_id = edge["source_id"]
                target_id = edge["target_id"]
                contradiction_peers.setdefault(source_id, set()).add(target_id)
                contradiction_peers.setdefault(target_id, set()).add(source_id)

        output: dict[str, dict[str, Any]] = {}
        for head_id in sorted(head_candidate_ids):
            ancestors_result = get_lineage(
                head_id,
                direction="ancestors",
                max_depth=SUPERSESSION_CHAIN_MAX_DEPTH,
                point_in_time=pit,
                db_connection=conn,
            )
            if "error" in ancestors_result:
                logger.warning(
                    "Could not assemble lineage for head %s: %s",
                    head_id,
                    ancestors_result["error"],
                )
                continue

            full_ancestor_nodes = [
                node for node in ancestors_result["nodes"] if node["id"] != head_id
            ]
            if not full_ancestor_nodes:
                continue

            full_ancestor_nodes = _sort_ancestor_nodes(full_ancestor_nodes)
            recency_kept = full_ancestor_nodes[:LINEAGE_HISTORICAL_CAP]
            recency_kept_ids = {node["id"] for node in recency_kept}
            force_included = [
                node
                for node in full_ancestor_nodes
                if node["id"] not in recency_kept_ids and contradiction_peers.get(node["id"])
            ]
            final_nodes = _sort_ancestor_nodes(recency_kept + force_included)
            historical_dropped_count = len(full_ancestor_nodes) - len(final_nodes)
            historical = []
            for node in final_nodes:
                entry = {
                    "id": node["id"],
                    "title": node["title"],
                    "archived_at": node["updated_at"],
                    "included_via": (
                        "recency" if node["id"] in recency_kept_ids else "contradicts_force_include"
                    ),
                }
                historical.append(_add_contradiction_flags(entry, node["id"], contradiction_peers))

            current = _add_contradiction_flags(
                {"id": head_id, "title": ancestors_result["root"]["title"]},
                head_id,
                contradiction_peers,
            )
            output[head_id] = {
                "current": current,
                "historical": historical,
                "historical_truncated": historical_dropped_count > 0,
                "historical_dropped_count": historical_dropped_count,
            }
        return output
    finally:
        if should_close:
            close_connection(conn)
