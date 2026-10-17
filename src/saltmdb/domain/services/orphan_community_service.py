"""Query-time assignment of zero-edge primary hits to community members."""

import logging
import sqlite3
from typing import Any

import numpy as np

from saltmdb.config import (
    COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD,
    CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.db.vector_schema import try_load_vector_extension

logger = logging.getLogger(__name__)


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 0 else vector


def _zero_result() -> dict[str, Any]:
    return {
        "orphan_community_matches": [],
        "orphan_community_cap": {
            "cap": CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
            "eligible_count": 0,
            "truncated": False,
            "dropped_count": 0,
        },
    }


def _load_orphan_assignments(conn, orphan_ids, agent_id, np):
    from saltmdb.domain.services.community_detection_service import fetch_leaf_community_centroids

    centroid_rows = fetch_leaf_community_centroids(conn)
    if agent_id is not None:
        visible = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT cm.community_id FROM community_membership cm "
                "JOIN entities e ON e.id = cm.entity_id "
                "WHERE e.agent_id = ? OR e.scope = 'shared'",
                (agent_id,),
            )
        }
        centroid_rows = [row for row in centroid_rows if row[0] in visible]
    if not centroid_rows:
        return None, None
    centroids = {
        community_id: _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
        for community_id, blob in centroid_rows
    }
    placeholders = ",".join("?" for _ in orphan_ids)
    orphan_rows = conn.execute(
        f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
        orphan_ids,
    ).fetchall()
    orphan_vectors = {
        entity_id: _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
        for entity_id, blob in orphan_rows
    }
    return orphan_vectors, _assign_orphans(orphan_vectors, centroids, np)


def _rank_orphan_candidates(candidate_rows):
    best_by_member: dict[str, tuple[str, str, float]] = {}
    for member_id, orphan_id, community_id, similarity in candidate_rows:
        current = best_by_member.get(member_id)
        if current is None or similarity > current[2]:
            best_by_member[member_id] = (orphan_id, community_id, similarity)
    ranked = sorted(best_by_member.items(), key=lambda item: (-item[1][2], item[0]))
    return ranked, len(ranked)


def _load_orphan_titles(conn, admitted_ids):
    if not admitted_ids:
        return {}
    placeholders = ",".join("?" for _ in admitted_ids)
    title_rows = conn.execute(
        f"SELECT id, title FROM entities WHERE id IN ({placeholders})", admitted_ids
    ).fetchall()
    return {entity_id: title if title is not None else "Unknown" for entity_id, title in title_rows}


def _build_orphan_result(conn, candidate_rows) -> dict[str, Any]:
    ranked, eligible_count = _rank_orphan_candidates(candidate_rows)
    admitted = ranked[:CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP]
    dropped_count = max(0, eligible_count - CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP)
    title_by_id = _load_orphan_titles(conn, [member_id for member_id, _ in admitted])
    return {
        "orphan_community_matches": [
            {
                "entity_id": member_id,
                "title": title_by_id.get(member_id, "Unknown"),
                "similarity": float(details[2]),
                "orphan_entity_id": details[0],
                "community_id": details[1],
            }
            for member_id, details in admitted
        ],
        "orphan_community_cap": {
            "cap": CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
            "eligible_count": eligible_count,
            "truncated": dropped_count > 0,
            "dropped_count": dropped_count,
        },
    }


def _assign_orphans(orphan_vectors, centroids, np) -> dict[str, str]:
    assignments = {}
    for orphan_id, vector in orphan_vectors.items():
        candidates = [
            (float(np.dot(vector, centroid)), community_id)
            for community_id, centroid in centroids.items()
        ]
        if not candidates:
            continue
        best_similarity, best_community_id = sorted(
            candidates, key=lambda item: (-item[0], item[1])
        )[0]
        if best_similarity >= COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD:
            assignments[orphan_id] = best_community_id
    return assignments


def _candidate_rows(
    conn, assignments, orphan_vectors, excluded_entity_ids, agent_id, np
) -> list[tuple[str, str, str, float]]:
    rows = []
    for orphan_id, community_id in assignments.items():
        member_rows = conn.execute(
            "SELECT cm.entity_id FROM community_membership cm JOIN entities e ON e.id = cm.entity_id "
            "WHERE cm.community_id = ?"
            + (" AND (e.agent_id = ? OR e.scope = 'shared')" if agent_id is not None else ""),
            (community_id, agent_id) if agent_id is not None else (community_id,),
        ).fetchall()
        member_ids = [
            row[0]
            for row in member_rows
            if row[0] != orphan_id and row[0] not in excluded_entity_ids
        ]
        if not member_ids:
            continue
        placeholders = ",".join("?" for _ in member_ids)
        embedding_rows = conn.execute(
            f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
            member_ids,
        ).fetchall()
        for member_id, blob in embedding_rows:
            member_vector = _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
            rows.append(
                (
                    member_id,
                    orphan_id,
                    community_id,
                    float(np.dot(orphan_vectors[orphan_id], member_vector)),
                )
            )
    return rows


def find_orphan_community_matches(  # noqa: C901, PLR0912, PLR0915
    primary_hits: list[dict[str, Any]],
    excluded_entity_ids: set[str],
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Find community members to force-include for zero-edge primary hits."""
    if not primary_hits:
        return _zero_result()

    should_close = db_connection is None
    conn = db_connection if db_connection is not None else get_connection(db_path or get_db_path())

    try:
        primary_hit_ids = [hit["id"] for hit in primary_hits]
        placeholders = ",".join("?" for _ in primary_hit_ids)
        member_rows = conn.execute(
            f"SELECT entity_id FROM community_membership WHERE entity_id IN ({placeholders})",
            primary_hit_ids,
        ).fetchall()
        has_membership = {row[0] for row in member_rows}
        orphan_ids = [hit_id for hit_id in primary_hit_ids if hit_id not in has_membership]
        if not orphan_ids:
            return _zero_result()

        if not try_load_vector_extension(conn):
            logger.warning(
                "find_orphan_community_matches: sqlite-vec extension unavailable, skipping %s",
                "orphan community assignment for this call",
            )
            return _zero_result()

        orphan_vectors, orphan_assignment = _load_orphan_assignments(conn, orphan_ids, agent_id, np)
        if not orphan_assignment:
            return _zero_result()
        candidate_rows = _candidate_rows(
            conn, orphan_assignment, orphan_vectors, excluded_entity_ids, agent_id, np
        )

        return _build_orphan_result(conn, candidate_rows)
    finally:
        if should_close:
            close_connection(conn)
