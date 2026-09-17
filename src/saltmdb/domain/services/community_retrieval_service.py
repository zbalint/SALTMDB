"""Global retrieval over seeded leaf communities and their members."""

import logging
import sqlite3
from typing import Any, cast

import numpy as np

from saltmdb.config import (
    CONTEXT_GLOBAL_MEMBER_POOL_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
    CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
    get_db_path,
)
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.db.vector_schema import try_load_vector_extension
from saltmdb.domain.services.community_detection_service import fetch_leaf_community_centroids
from saltmdb.domain.services.embedding_service import embed_text

logger = logging.getLogger(__name__)


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 0 else vector


def _empty_result() -> dict[str, Any]:
    zero_cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
    return {
        "representative_matches": [],
        "member_matches": [],
        "seed_cap": {"cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **zero_cap},
        "representative_reserve": {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            **zero_cap,
        },
        "member_pool": {"cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP, **zero_cap},
    }


def seed_and_rank_communities(  # noqa: C901, PLR0912, PLR0915
    query: str,
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Seed and rank leaf communities and their members for global retrieval.

    The query is embedded once and compared with every leaf community centroid to pick which
    communities to enter. Within each entered community, every member (including its fixed,
    clustering-time representative_entity_id) is then scored against the same query; the single
    best per-query match becomes that community's representative for this response, and the rest
    fall through to one shared member pool ranked by member-to-query similarity. The fixed
    representative_entity_id is never surfaced as-is -- it is only one candidate among its
    community's members for this per-query selection.
    """
    should_close = db_connection is None
    conn = db_connection if db_connection is not None else get_connection(db_path or get_db_path())

    try:
        if not query.strip():
            return _empty_result()

        query_vector = _normalize(np.array(embed_text(query), dtype=np.float64))
        if isinstance(conn, sqlite3.Connection) and not try_load_vector_extension(conn):
            logger.warning(
                "seed_and_rank_communities: sqlite-vec extension unavailable, skipping global community retrieval for this call",
            )
            return _empty_result()
        centroid_rows = fetch_leaf_community_centroids(conn)
        if not centroid_rows:
            return _empty_result()

        ranked_communities = sorted(
            (
                (
                    community_id,
                    float(
                        np.dot(
                            query_vector,
                            _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64)),
                        )
                    ),
                )
                for community_id, blob in centroid_rows
            ),
            key=lambda item: (-item[1], item[0]),
        )
        eligible_count = len(ranked_communities)
        seeded = ranked_communities[:CONTEXT_GLOBAL_TOP_K_COMMUNITIES]
        seed_cap = {
            "cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "eligible_count": eligible_count,
            "truncated": eligible_count > CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "dropped_count": max(0, eligible_count - CONTEXT_GLOBAL_TOP_K_COMMUNITIES),
        }
        if not seeded:
            return _empty_result()

        seeded_ids = [community_id for community_id, _ in seeded]
        placeholders = ",".join("?" for _ in seeded_ids)
        membership_rows = cast(
            list[tuple[str, str]],
            conn.execute(
                f"SELECT entity_id, community_id FROM community_membership WHERE community_id IN ({placeholders})",
                seeded_ids,
            ).fetchall(),
        )
        members_by_community: dict[str, list[str]] = {
            community_id: [] for community_id in seeded_ids
        }
        for entity_id, community_id in membership_rows:
            if community_id in members_by_community:
                members_by_community[community_id].append(entity_id)

        # Every community member -- including the fixed, clustering-time representative_entity_id
        # -- is scored against this query: the representative slot below is picked per query, not
        # read off the fixed id, so it must compete on equal footing with the rest of the community.
        all_member_ids = [
            entity_id
            for community_id in seeded_ids
            for entity_id in members_by_community[community_id]
        ]
        scored_by_community: dict[str, list[tuple[str, float]]] = {
            community_id: [] for community_id in seeded_ids
        }
        if all_member_ids:
            member_placeholders = ",".join("?" for _ in all_member_ids)
            embedding_rows = cast(
                list[tuple[str, bytes]],
                conn.execute(
                    f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({member_placeholders})",
                    all_member_ids,
                ).fetchall(),
            )
            community_by_member = {
                entity_id: community_id
                for community_id in seeded_ids
                for entity_id in members_by_community[community_id]
            }
            for entity_id, blob in embedding_rows:
                member_vector = _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
                similarity = float(np.dot(query_vector, member_vector))
                scored_by_community[community_by_member[entity_id]].append((entity_id, similarity))

        # For each seeded community (in centroid-seed rank order), the single best per-query match
        # becomes that community's representative for this response; every other scored member of
        # that community falls through to the shared member pool below. A community contributes
        # nothing to either list if none of its members have an embedding row.
        representative_candidates: list[tuple[str, str, float]] = []
        member_candidates: list[tuple[str, str, float]] = []
        for community_id, _centroid_similarity in seeded:
            scored = scored_by_community.get(community_id, [])
            if not scored:
                continue
            scored.sort(key=lambda item: (-item[1], item[0]))
            top_entity_id, top_similarity = scored[0]
            representative_candidates.append((top_entity_id, community_id, top_similarity))
            member_candidates.extend(
                (entity_id, community_id, similarity) for entity_id, similarity in scored[1:]
            )

        representative_eligible_count = len(representative_candidates)
        representative_admitted = representative_candidates[
            :CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
        ]
        representative_reserve = {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            "eligible_count": representative_eligible_count,
            "truncated": representative_eligible_count > CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            "dropped_count": max(
                0, representative_eligible_count - CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
            ),
        }

        member_candidates.sort(key=lambda item: (-item[2], item[0]))
        member_eligible_count = len(member_candidates)
        admitted_members = member_candidates[:CONTEXT_GLOBAL_MEMBER_POOL_CAP]
        member_pool = {
            "cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP,
            "eligible_count": member_eligible_count,
            "truncated": member_eligible_count > CONTEXT_GLOBAL_MEMBER_POOL_CAP,
            "dropped_count": max(0, member_eligible_count - CONTEXT_GLOBAL_MEMBER_POOL_CAP),
        }

        admitted_ids = [candidate[0] for candidate in representative_admitted]
        admitted_ids.extend(candidate[0] for candidate in admitted_members)
        title_by_id: dict[str, str] = {}
        if admitted_ids:
            unique_admitted_ids = list(dict.fromkeys(admitted_ids))
            title_placeholders = ",".join("?" for _ in unique_admitted_ids)
            title_rows = cast(
                list[tuple[str, str | None]],
                conn.execute(
                    f"SELECT id, title FROM entities WHERE id IN ({title_placeholders})",
                    unique_admitted_ids,
                ).fetchall(),
            )
            title_by_id = {
                entity_id: title if title is not None else "Unknown"
                for entity_id, title in title_rows
            }

        return {
            "representative_matches": [
                {
                    "entity_id": entity_id,
                    "title": title_by_id.get(entity_id, "Unknown"),
                    "community_id": community_id,
                    "seed_similarity": seed_similarity,
                }
                for entity_id, community_id, seed_similarity in representative_admitted
            ],
            "member_matches": [
                {
                    "entity_id": entity_id,
                    "title": title_by_id.get(entity_id, "Unknown"),
                    "community_id": community_id,
                    "similarity": similarity,
                }
                for entity_id, community_id, similarity in admitted_members
            ],
            "seed_cap": seed_cap,
            "representative_reserve": representative_reserve,
            "member_pool": member_pool,
        }
    finally:
        if should_close:
            close_connection(conn)
