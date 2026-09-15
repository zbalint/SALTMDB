# pyright: reportImportCycles=false
import logging
import os
import sqlite3
import uuid
from datetime import datetime, UTC
from typing import Any

from saltmdb.config import COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S, get_db_path
from saltmdb.db.connection import close_connection, get_connection, write_transaction_retrying
from saltmdb.db.vector_schema import try_load_vector_extension

logger = logging.getLogger(__name__)

_COMMUNITY_DETECTION_TASK_NAME = "community_detection"


def _compute_community_group(group, graph, node_ids, raw_vector):
    """Compute one partition group's representative and centroid.

    This internal seam keeps the singleton branch directly exercisable without pretending that
    ModularityVertexPartition reliably emits singleton groups for connected vertices.
    """
    import numpy as np

    top_ids = []
    member_ids = sorted(node_ids[i] for i in group)
    if len(member_ids) == 1:
        representative_id = member_ids[0]
        centroid_source = (
            {representative_id: raw_vector[representative_id]}
            if representative_id in raw_vector
            else {}
        )
        pagerank_by_id = {representative_id: 1.0}
    else:
        subgraph = graph.subgraph(group)
        pagerank_scores = subgraph.pagerank(directed=False)
        pagerank_by_id = {node_ids[group[i]]: pagerank_scores[i] for i in range(len(group))}
        max_score = max(pagerank_by_id.values())
        top_ids = sorted(eid for eid, score in pagerank_by_id.items() if score == max_score)
        representative_id = None
        centroid_source = {eid: raw_vector[eid] for eid in member_ids if eid in raw_vector}

    if not centroid_source:
        centroid_vec = None
    elif len(centroid_source) == 1 and len(member_ids) == 1:
        centroid_vec = _normalize_community_vector(next(iter(centroid_source.values())))
    else:
        total_weight = sum(pagerank_by_id[eid] for eid in centroid_source)
        weighted_sum = np.zeros(384, dtype=np.float64)
        for eid, vec in centroid_source.items():
            weight = (
                pagerank_by_id[eid] / total_weight
                if total_weight > 0
                else (1.0 / len(centroid_source))
            )
            weighted_sum += weight * _normalize_community_vector(vec.astype(np.float64))
        centroid_vec = _normalize_community_vector(weighted_sum)

    if len(member_ids) > 1:
        if len(top_ids) == 1:
            representative_id = top_ids[0]
        elif centroid_vec is not None:
            centroid_for_similarity = centroid_vec

            def _cosine_sim(eid: str) -> float:
                if eid not in raw_vector:
                    return -1.0
                return float(
                    np.dot(
                        _normalize_community_vector(raw_vector[eid].astype(np.float64)),
                        centroid_for_similarity,
                    )
                )

            best_sim = max(_cosine_sim(eid) for eid in top_ids)
            closest = sorted(eid for eid in top_ids if _cosine_sim(eid) == best_sim)
            representative_id = closest[0]
        else:
            representative_id = sorted(top_ids)[0]

    return member_ids, representative_id, centroid_vec


def _normalize_community_vector(vec):
    import numpy as np

    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def recompute_communities(  # noqa: C901, PLR0912, PLR0915
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Recompute the flat Leiden community index from the current relation graph."""
    should_close = False
    conn = db_connection
    if conn is None:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        vector_extension_loaded = try_load_vector_extension(conn)
        if not vector_extension_loaded:
            logger.warning(
                "recompute_communities: sqlite-vec extension unavailable on this connection -- "
                "community structure (communities/community_membership) will still be computed and "
                "written normally, but community_embeddings is left untouched this cycle (no read, no "
                "clear, no write against it)."
            )

        now = datetime.now(UTC).isoformat()
        rows = conn.execute(
            """
            SELECT r.source_id, r.target_id
            FROM relations r
            JOIN entities e1 ON r.source_id = e1.id
            JOIN entities e2 ON r.target_id = e2.id
            WHERE e1.status != 'archived' AND e2.status != 'archived'
              AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
            """,
            (now, now, now, now),
        ).fetchall()

        edge_set: set[frozenset[str]] = set()
        for source_id, target_id in rows:
            if source_id != target_id:
                edge_set.add(frozenset({source_id, target_id}))
        node_ids: list[str] = sorted({n for pair in edge_set for n in pair})

        if not edge_set:

            def _clear(c):
                c.execute("DELETE FROM community_membership")
                c.execute("DELETE FROM communities")
                if vector_extension_loaded:
                    c.execute("DELETE FROM community_embeddings")

            write_transaction_retrying(conn, _clear)
            return {"status": "no_edges", "communities_created": 0}

        import igraph as ig

        index_of = {entity_id: i for i, entity_id in enumerate(node_ids)}
        edges = [tuple(sorted(index_of[n] for n in pair)) for pair in edge_set]
        graph = ig.Graph(n=len(node_ids), edges=edges, directed=False)

        import leidenalg

        partition = leidenalg.find_partition(graph, leidenalg.ModularityVertexPartition)

        import numpy as np

        raw_vector: dict[str, np.ndarray] = {}
        if vector_extension_loaded:
            placeholders = ",".join("?" for _ in node_ids)
            embedding_rows = conn.execute(
                f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
                node_ids,
            ).fetchall()
            raw_vector = {
                entity_id: np.frombuffer(blob, dtype=np.float32)
                for entity_id, blob in embedding_rows
            }

        communities_to_insert = []
        membership_to_insert = []
        embeddings_to_insert = []

        for group in partition:
            member_ids, representative_id, centroid_vec = _compute_community_group(
                group, graph, node_ids, raw_vector
            )
            member_count = len(member_ids)
            community_id = str(uuid.uuid4())
            communities_to_insert.append((community_id, representative_id, member_count, 0, now))
            for entity_id in member_ids:
                membership_to_insert.append((entity_id, community_id, 0))
            if centroid_vec is not None:
                import sqlite_vec

                embeddings_to_insert.append(
                    (
                        community_id,
                        sqlite_vec.serialize_float32(centroid_vec.astype(np.float32).tolist()),
                    )
                )

        def _write(c):
            c.execute("DELETE FROM community_membership")
            c.execute("DELETE FROM communities")
            c.executemany(
                "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                communities_to_insert,
            )
            c.executemany(
                "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, ?)",
                membership_to_insert,
            )
            if vector_extension_loaded:
                c.execute("DELETE FROM community_embeddings")
                c.executemany(
                    "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
                    embeddings_to_insert,
                )

        write_transaction_retrying(conn, _write)
        return {
            "status": "recomputed",
            "communities_created": len(communities_to_insert),
            "nodes_clustered": len(node_ids),
            "communities_missing_embedding": len(communities_to_insert) - len(embeddings_to_insert),
        }
    finally:
        if should_close:
            close_connection(conn)


def trigger_community_detection(db_path: str | None = None, *, coordinator=None) -> None:
    """Fire-and-forget: schedules the cooldown check + recompute on the SAME single-worker
    _librarian_trigger_pool trigger_librarian uses (constraint 18's own named, accepted shared-pool
    tradeoff) -- never blocks the caller. `coordinator` branching mirrors
    `librarian_service.trigger_librarian` exactly (see Amendment 3) -- required for correctness
    under the real daemon's single-writer boundary, not optional polish."""
    if os.environ.get("SALTMDB_DISABLE_COMMUNITY_DETECTION") or os.environ.get("SALTMDB_TEST_MODE"):
        return
    db_path = db_path or get_db_path()
    from saltmdb.domain.services.librarian_service import _librarian_trigger_pool

    if coordinator is not None:
        _librarian_trigger_pool.submit(
            _run_community_detection_with_coordinator, db_path, coordinator
        )
    else:
        _librarian_trigger_pool.submit(_run_community_detection_pass_impl, db_path)


def _run_community_detection_with_coordinator(db_path: str, coordinator) -> str:
    """Mirrors `librarian_service.run_librarian_now`'s own coordinator branch (Amendment 3):
    runs on the trigger-pool's worker thread (the same thread `trigger_community_detection`
    above submitted onto), then hops onto the coordinator's own dedicated writer thread via
    `coordinator.submit`, where `connection.py`'s `_coordinator_connection` ContextVar is
    actually set for the duration of the call -- this hop is why a coordinator-aware branch
    exists at all: ContextVar values set on the coordinator's writer thread never propagate
    into a *different* ThreadPoolExecutor worker thread (this function's own caller's thread),
    so this function's job closure must explicitly receive the connection as an argument
    (`conn`, below) rather than relying on `get_connection()` to find it implicitly. `db_path`
    is accepted for signature symmetry with the no-coordinator branch but unused here --
    `coordinator.submit` supplies its own connection, already opened against the coordinator's
    own `db_path` at daemon startup."""
    return coordinator.submit(
        "community_detection_mutations",
        lambda conn: _run_community_detection_pass_on_connection(conn),
        priority="background",
    )


def _run_community_detection_pass_on_connection(conn) -> str:
    """Runs the coordinator-path pass body on the coordinator-owned connection."""
    now = datetime.now(UTC).isoformat()
    edge_count = conn.execute(
        """
        SELECT COUNT(*) FROM relations r
        JOIN entities e1 ON r.source_id = e1.id
        JOIN entities e2 ON r.target_id = e2.id
        WHERE e1.status != 'archived' AND e2.status != 'archived'
          AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
          AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
          AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
          AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
        """,
        (now, now, now, now),
    ).fetchone()[0]
    if edge_count == 0:
        recompute_communities(db_connection=conn)
        return "Skipped: no qualifying relation edges to cluster."
    claim_now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        f"""
        UPDATE _system_locks
        SET last_run_at = ?
        WHERE task_name = '{_COMMUNITY_DETECTION_TASK_NAME}'
          AND (last_run_at IS NULL OR datetime(last_run_at) < datetime('now', '-{COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S} seconds'))
        """,
        (claim_now,),
    )
    if cur.rowcount != 1:
        return "Skipped: cooldown not elapsed."
    result = recompute_communities(db_connection=conn)
    return f"Community detection pass complete: {result}"


def _run_community_detection_pass_impl(db_path: str) -> str:
    """Mirrors librarian_service._run_maintenance_pass_impl's exact shape: cheap precondition
    check (skip without claiming the cooldown), atomic cooldown-claim UPDATE, then the real pass --
    but the precondition here is 'no qualifying edges exist yet' (this pass's own §6.1 step 3/4
    query), not raw-entity count, since community structure depends on edges, not entity count."""
    try:
        conn = get_connection(db_path)
    except Exception as e:
        logger.debug("Could not open connection for community detection pass: %s", e)
        return f"Skipped: could not open database connection ({e})."
    try:
        now = datetime.now(UTC).isoformat()
        edge_count = conn.execute(
            """
            SELECT COUNT(*) FROM relations r
            JOIN entities e1 ON r.source_id = e1.id
            JOIN entities e2 ON r.target_id = e2.id
            WHERE e1.status != 'archived' AND e2.status != 'archived'
              AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
            """,
            (now, now, now, now),
        ).fetchone()[0]
        if edge_count == 0:
            recompute_communities(db_connection=conn)
            return "Skipped: no qualifying relation edges to cluster."

        def _claim_cooldown(c):
            claim_now = datetime.now(UTC).isoformat()
            cur = c.execute(
                f"""
                UPDATE _system_locks
                SET last_run_at = ?
                WHERE task_name = '{_COMMUNITY_DETECTION_TASK_NAME}'
                  AND (last_run_at IS NULL OR datetime(last_run_at) < datetime('now', '-{COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S} seconds'))
                """,
                (claim_now,),
            )
            return cur.rowcount == 1

        if not write_transaction_retrying(conn, _claim_cooldown):
            return "Skipped: cooldown not elapsed."

        result = recompute_communities(db_connection=conn)
        return f"Community detection pass complete: {result}"
    except Exception as e:
        logger.warning("Community detection pass failed: %s", e)
        return f"Failed: {e}"
    finally:
        close_connection(conn)
