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


def _record_provenance(
    edge: DependencyEdge,
    primary_ids: set[str],
    provenance: dict[str, ExpansionNode],
) -> None:
    source_id, target_id = edge["source_id"], edge["target_id"]
    if (source_id in primary_ids) == (target_id in primary_ids):
        return
    origin = source_id if source_id in primary_ids else target_id
    entity_id = target_id if origin == source_id else source_id
    direction = "outbound" if origin == source_id else "inbound"
    title = edge["target_title"] if direction == "outbound" else edge["source_title"]
    provenance.setdefault(entity_id, {"title": title, "provenance": set()})["provenance"].add(
        (origin, edge["predicate"], direction)
    )


def _record_edge(
    edge: DependencyEdge,
    primary_ids: set[str],
    in_network: dict[str, RawEdge],
    contradicts: dict[str, RawEdge],
    provenance: dict[str, ExpansionNode],
) -> None:
    predicate = edge["predicate"]
    if predicate == "contradicts":
        contradicts[edge["relation_id"]] = {
            "relation_id": edge["relation_id"],
            "source_id": edge["source_id"],
            "target_id": edge["target_id"],
            "predicate": edge["predicate"],
        }
        return
    if predicate not in DEFAULT_CONTEXT_EXPANSION_PREDICATES:
        return
    source_id, target_id = edge["source_id"], edge["target_id"]
    if source_id in primary_ids and target_id in primary_ids:
        in_network[edge["relation_id"]] = {
            "relation_id": edge["relation_id"],
            "source_id": edge["source_id"],
            "target_id": edge["target_id"],
            "predicate": edge["predicate"],
        }
        return
    _record_provenance(edge, primary_ids, provenance)


def _collect_edges(
    hit_id: str,
    *,
    primary_ids: set[str],
    pit: str,
    conn: sqlite3.Connection,
    agent_id: str | None,
    in_network: dict[str, RawEdge],
    contradicts: dict[str, RawEdge],
    provenance: dict[str, ExpansionNode],
) -> None:
    traversal = cast(
        dict[str, object],
        analyze_dependencies(
            root_entity_id=hit_id,
            max_depth=1,
            direction="both",
            point_in_time=pit,
            db_connection=conn,
            agent_id=agent_id,
        ),
    )
    if "error" in traversal:
        logger.warning(
            "Skipping context expansion for primary hit %s: %s", hit_id, traversal["error"]
        )
        return
    for edge in cast(list[DependencyEdge], traversal["edges"]):
        _record_edge(edge, primary_ids, in_network, contradicts, provenance)


def _rank_expansion_nodes(
    provenance: dict[str, ExpansionNode], scores: dict[str, float]
) -> list[RankedNode]:
    ranked = [
        cast(
            RankedNode,
            cast(
                object,
                {
                    "entity_id": entity_id,
                    "title": node["title"],
                    "mutual_neighbor_count": len({p[0] for p in node["provenance"]}),
                    "tiebreak_score": max(scores[p[0]] for p in node["provenance"]),
                    "provenance": node["provenance"],
                },
            ),
        )
        for entity_id, node in provenance.items()
    ]
    ranked.sort(
        key=lambda node: (
            -node["mutual_neighbor_count"],
            -node["tiebreak_score"],
            node["entity_id"],
        )
    )
    return ranked


def _render_expansion_nodes(nodes: list[RankedNode]) -> list[dict[str, object]]:
    return [
        {
            "entity_id": node["entity_id"],
            "title": node["title"],
            "mutual_neighbor_count": node["mutual_neighbor_count"],
            "tiebreak_score": node["tiebreak_score"],
            "retrieval_provenance": [
                {
                    "reason": "graph_expansion",
                    "origin_hit_id": origin,
                    "predicate": predicate,
                    "direction": direction,
                    "hop_depth": 1,
                }
                for origin, predicate, direction in sorted(node["provenance"])
            ],
        }
        for node in nodes
    ]


def expand_context_candidates(
    primary_hits: list[PrimaryHit],
    *,
    point_in_time: str | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Expand primary hits through one allowed graph hop for later context assembly."""
    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True
    primary_ids = {hit["id"] for hit in primary_hits}
    primary_scores = {hit["id"]: hit["score"] for hit in primary_hits}
    pit = point_in_time or datetime.now(UTC).isoformat()
    in_network: dict[str, RawEdge] = {}
    contradicts: dict[str, RawEdge] = {}
    provenance: dict[str, ExpansionNode] = {}
    try:
        for hit_id in primary_ids:
            _collect_edges(
                hit_id,
                primary_ids=primary_ids,
                pit=pit,
                conn=conn,
                agent_id=agent_id,
                in_network=in_network,
                contradicts=contradicts,
                provenance=provenance,
            )
        ranked = _rank_expansion_nodes(provenance, primary_scores)
        cap = CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS * len(primary_hits)
        survivors = ranked[:cap]
        return {
            "in_network_edges": [in_network[key] for key in sorted(in_network)],
            "expansion_candidates": _render_expansion_nodes(survivors),
            "contradicts_edges": [contradicts[key] for key in sorted(contradicts)],
            "fan_out": {
                "cap": cap,
                "eligible_count": len(ranked),
                "truncated": len(ranked) > cap,
                "dropped_count": max(0, len(ranked) - cap),
            },
        }
    finally:
        if should_close:
            close_connection(conn)
