import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from saltmdb.config import CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS, get_db_path
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.relation_service import analyze_dependencies

logger = logging.getLogger(__name__)

# Default-include predicates for Milestone A context expansion (wayfinder ticket G3, memory
# 0ebfb511). `contradicts` is deliberately NOT in this set -- it is force-included but handled
# entirely by slice A2 (conflict-set assembly), never by this slice's general allowlist/cap path.
# Reserved lifecycle predicates (supersedes/consolidated_from/revises) and legacy similar_to are
# likewise deliberately absent -- they are excluded, not merely unlisted, per G3's locked policy.
DEFAULT_CONTEXT_EXPANSION_PREDICATES: frozenset[str] = frozenset(
    {"elaborates_on", "depends_on", "corrects", "derived_from", "resolves"}
)

Provenance = tuple[str, str, str]


class PrimaryHit(TypedDict):
    id: str
    score: float


class RawEdge(TypedDict):
    relation_id: str
    source_id: str
    target_id: str
    predicate: str


class DependencyEdge(RawEdge):
    source_title: str
    target_title: str
    depth: int


class ExpansionNode(TypedDict):
    title: str
    provenance: set[Provenance]


class RankedNode(ExpansionNode):
    entity_id: str
    mutual_neighbor_count: int
    tiebreak_score: float


def expand_context_candidates(
    primary_hits: list[PrimaryHit],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Expand primary hits through one allowed graph hop for later context assembly."""
    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    primary_hit_ids = {hit["id"] for hit in primary_hits}
    primary_hit_score = {hit["id"]: hit["score"] for hit in primary_hits}
    pit = point_in_time or datetime.now(UTC).isoformat()
    in_network_edges: dict[str, RawEdge] = {}
    contradicts_edges: dict[str, RawEdge] = {}
    provenance_by_node: dict[str, ExpansionNode] = {}

    try:
        for hit_id in primary_hit_ids:
            traversal = cast(
                dict[str, object],
                analyze_dependencies(
                    root_entity_id=hit_id,
                    max_depth=1,
                    direction="both",
                    point_in_time=pit,
                    db_connection=conn,
                    owner_id=owner_id,
                ),
            )
            if "error" in traversal:
                logger.warning(
                    "Skipping context expansion for primary hit %s: %s", hit_id, traversal["error"]
                )
                continue

            for edge in cast(list[DependencyEdge], traversal["edges"]):
                predicate = edge["predicate"]
                if predicate == "contradicts":
                    contradicts_edges[edge["relation_id"]] = {
                        "relation_id": edge["relation_id"],
                        "source_id": edge["source_id"],
                        "target_id": edge["target_id"],
                        "predicate": predicate,
                    }
                    continue
                if predicate not in DEFAULT_CONTEXT_EXPANSION_PREDICATES:
                    continue

                source_id = edge["source_id"]
                target_id = edge["target_id"]
                if source_id in primary_hit_ids and target_id in primary_hit_ids:
                    in_network_edges[edge["relation_id"]] = {
                        "relation_id": edge["relation_id"],
                        "source_id": source_id,
                        "target_id": target_id,
                        "predicate": predicate,
                    }
                    continue
                if (source_id in primary_hit_ids) == (target_id in primary_hit_ids):
                    continue

                origin_hit_id = source_id if source_id in primary_hit_ids else target_id
                entity_id = target_id if origin_hit_id == source_id else source_id
                direction = "outbound" if origin_hit_id == source_id else "inbound"
                title = edge["target_title"] if direction == "outbound" else edge["source_title"]
                provenance: Provenance = (origin_hit_id, predicate, direction)
                node = provenance_by_node.setdefault(
                    entity_id, {"title": title, "provenance": set()}
                )
                node["provenance"].add(provenance)

        ranked_nodes: list[RankedNode] = []
        for entity_id, node in provenance_by_node.items():
            origin_hit_ids = {provenance[0] for provenance in node["provenance"]}
            ranked_nodes.append(
                {
                    "entity_id": entity_id,
                    "title": node["title"],
                    "mutual_neighbor_count": len(origin_hit_ids),
                    "tiebreak_score": max(primary_hit_score[hit_id] for hit_id in origin_hit_ids),
                    "provenance": node["provenance"],
                }
            )
        ranked_nodes.sort(
            key=lambda node: (
                -node["mutual_neighbor_count"],
                -node["tiebreak_score"],
                node["entity_id"],
            )
        )

        cap = CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits)
        eligible_count = len(ranked_nodes)
        survivors = ranked_nodes[:cap]
        dropped_count = max(0, eligible_count - cap)
        expansion_candidates: list[dict[str, object]] = [
            {
                "entity_id": node["entity_id"],
                "title": node["title"],
                "mutual_neighbor_count": node["mutual_neighbor_count"],
                "tiebreak_score": node["tiebreak_score"],
                "retrieval_provenance": [
                    {
                        "reason": "graph_expansion",
                        "origin_hit_id": origin_hit_id,
                        "predicate": predicate,
                        "direction": direction,
                        "hop_depth": 1,
                    }
                    for origin_hit_id, predicate, direction in sorted(node["provenance"])
                ],
            }
            for node in survivors
        ]

        return {
            "in_network_edges": [
                in_network_edges[relation_id] for relation_id in sorted(in_network_edges)
            ],
            "expansion_candidates": expansion_candidates,
            "contradicts_edges": [
                contradicts_edges[relation_id] for relation_id in sorted(contradicts_edges)
            ],
            "fan_out": {
                "cap": cap,
                "eligible_count": eligible_count,
                "truncated": dropped_count > 0,
                "dropped_count": dropped_count,
            },
        }
    finally:
        if should_close:
            close_connection(conn)
