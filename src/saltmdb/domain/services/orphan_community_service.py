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


def find_orphan_community_matches(  # noqa: C901, PLR0912, PLR0915
    primary_hits: list[dict[str, Any]],
    excluded_entity_ids: set[str],
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
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

        centroid_rows = conn.execute(
            "SELECT community_id, embedding FROM community_embeddings"
        ).fetchall()
        if not centroid_rows:
            return _zero_result()
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

        orphan_assignment: dict[str, str] = {}
        for orphan_id, vector in orphan_vectors.items():
            best_community_id: str | None = None
            best_similarity = -1.0
            for community_id, centroid in centroids.items():
                similarity = float(np.dot(vector, centroid))
                if (
                    best_community_id is None
                    or similarity > best_similarity
                    or (similarity == best_similarity and community_id < best_community_id)
                ):
                    best_community_id = community_id
                    best_similarity = similarity
            if (
                best_community_id is not None
                and best_similarity >= COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD
            ):
                orphan_assignment[orphan_id] = best_community_id
        if not orphan_assignment:
            return _zero_result()

        candidate_rows: list[tuple[str, str, str, float]] = []
        for orphan_id, community_id in orphan_assignment.items():
            member_rows = conn.execute(
                "SELECT entity_id FROM community_membership WHERE community_id = ?",
                (community_id,),
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
            orphan_vector = orphan_vectors[orphan_id]
            for member_id, blob in embedding_rows:
                member_vector = _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
                similarity = float(np.dot(orphan_vector, member_vector))
                candidate_rows.append((member_id, orphan_id, community_id, similarity))

        best_by_member: dict[str, tuple[str, str, float]] = {}
        for member_id, orphan_id, community_id, similarity in candidate_rows:
            current = best_by_member.get(member_id)
            if current is None or similarity > current[2]:
                best_by_member[member_id] = (orphan_id, community_id, similarity)

        ranked = sorted(
            best_by_member.items(),
            key=lambda item: (-item[1][2], item[0]),
        )
        eligible_count = len(ranked)
        admitted = ranked[:CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP]
        dropped_count = max(0, eligible_count - CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP)

        admitted_ids = [member_id for member_id, _ in admitted]
        title_by_id: dict[str, str] = {}
        if admitted_ids:
            placeholders = ",".join("?" for _ in admitted_ids)
            title_rows = conn.execute(
                f"SELECT id, title FROM entities WHERE id IN ({placeholders})",
                admitted_ids,
            ).fetchall()
            title_by_id = {
                entity_id: title if title is not None else "Unknown"
                for entity_id, title in title_rows
            }

        matches = [
            {
                "entity_id": member_id,
                "title": title_by_id.get(member_id, "Unknown"),
                "similarity": float(orphan_details[2]),
                "orphan_entity_id": orphan_details[0],
                "community_id": orphan_details[1],
            }
            for member_id, orphan_details in admitted
        ]
        return {
            "orphan_community_matches": matches,
            "orphan_community_cap": {
                "cap": CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
                "eligible_count": eligible_count,
                "truncated": dropped_count > 0,
                "dropped_count": dropped_count,
            },
        }
    finally:
        if should_close:
            close_connection(conn)
