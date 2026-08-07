import logging
import sqlite3

from saltmdb.db.vector_schema import try_load_vector_extension

logger = logging.getLogger(__name__)


def _centroid(vectors: list) -> list[float]:
    """L2-normalize each vector, mean them, renormalize the mean -> a unit-length centroid.

    Shared by both the fresh-join and fallback paths of get_fresh_entity_centroids so the two
    code paths produce directly comparable centroids regardless of which one supplied a given
    entity's vectors.
    """
    import numpy as np

    matrix = np.vstack(vectors).astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1e-10
    normalized = matrix / norms
    mean_vec = normalized.mean(axis=0)
    mean_norm = np.linalg.norm(mean_vec)
    if mean_norm == 0:
        mean_norm = 1e-10
    return (mean_vec / mean_norm).tolist()


def compute_adhoc_centroid(content: str) -> list[float] | None:
    """Chunk-embeds and centroids a piece of content that has no entity id yet -- pure, no DB I/O.

    Used by disposition_service.py's consolidated-node integrity check (Track A, see
    scratch/plans/track_a_disposition_detailed.md §2.2): the incoming `store_memory` content needs
    a centroid to compare against a candidate target's, before it has ever been persisted as an
    entity (and, for a `"distinct"`-resolved candidate, may never be). Returns None if the content
    has no embeddable chunks (mirrors get_fresh_entity_centroids' own "no embeddable content"
    unresolved case).
    """
    from saltmdb.domain.services.embedding_service import compute_entity_chunk_embeddings

    chunk_rows = compute_entity_chunk_embeddings("__adhoc__", content or "")
    if not chunk_rows:
        return None
    vectors = [row["embedding"] for row in chunk_rows]
    return _centroid(vectors)


def get_fresh_entity_centroids(  # noqa: C901, PLR0912, PLR0915
    entity_ids: list[str],
    conn: sqlite3.Connection,
    db_path: str,
) -> tuple[dict[str, list[float]], dict[str, str], dict[str, tuple[str, str]]]:
    """Returns (centroids, unresolved, observed_state).

    unresolved: {entity_id: reason} for every requested id that could not be given a usable
    centroid (no content, archived status, embedding model unavailable, or any other fallback
    failure). A dict, not a bare list, so callers can log *why*, not just *which id*.

    observed_state: {entity_id: (content_hash, status)} -- captured ATOMICALLY, in the same
    read that produced (or failed to produce) that entity's centroid. This is NOT a separately-
    later-taken snapshot: the fresh-join path selects e.content_hash/e.status in the exact same
    query that supplies the chunk vectors (the join's `c.content_hash IS e.content_hash`
    predicate already guarantees these were consistent at SELECT time); the fallback path
    selects full_content, content_hash, status together in one query before computing an
    on-demand embedding, and records observed_state only once that on-demand centroid actually
    succeeds (an archived parent, or any other fallback failure, lands in `unresolved` with no
    observed_state entry -- see the eligibility rule below). Every entity_id that got a
    centroid gets a matching observed_state entry; callers (relation_service's TOCTOU
    revalidation) must treat a MISSING observed_state entry for a resolved parent as an
    unconditional revalidation failure, exactly like a parent that's still unresolved.
    """
    centroids: dict[str, list[float]] = {}
    unresolved: dict[str, str] = {}
    observed_state: dict[str, tuple[str, str]] = {}

    if not entity_ids:
        return centroids, unresolved, observed_state

    import numpy as np

    # P2 (Codex review, bf4qtkp7j): the fresh-join path below queries entity_chunk_embeddings,
    # a `vec0` virtual table -- it requires the sqlite-vec extension loaded on THIS connection.
    # try_load_vector_extension's return value must actually be checked: if the load failed, the
    # query would raise (uncaught, outside any try/except here) instead of degrading. Treat a
    # failed load as a guarded miss for every id -- skip the vec0 query entirely and let every
    # id fall through to the per-entity fallback path below, which needs no vec0 access.
    vector_extension_loaded = try_load_vector_extension(conn)

    # De-dup while preserving order -- callers may pass the same id more than once (e.g. a
    # cross-item union in bulk_commit_consolidation).
    unique_ids = list(dict.fromkeys(entity_ids))

    # Fresh-join path: pull chunk vectors + content_hash/status for entities with fresh,
    # non-archived persisted rows. Mirrors memory_service.py's canonical chunk-query join
    # (rerank_candidates_by_topic) -- `status != 'archived'` (never "raw-only": commit_consolidation
    # parents legitimately include already-'consolidated' entities in the refresh workflow) and
    # `c.content_hash IS e.content_hash` for staleness exclusion.
    rows = []
    if vector_extension_loaded:
        placeholders = ",".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT c.entity_id, c.embedding, e.content_hash, e.status
            FROM entity_chunk_embeddings c
            JOIN entities e ON e.id = c.entity_id
            WHERE c.entity_id IN ({placeholders})
              AND e.status != 'archived'
              AND c.content_hash IS e.content_hash
            """,
            unique_ids,
        ).fetchall()
    else:
        logger.warning(
            "get_fresh_entity_centroids: sqlite-vec extension unavailable on this connection, "
            "falling back to per-entity on-demand embedding for all %d requested id(s)",
            len(unique_ids),
        )

    grouped_vectors: dict[str, list] = {}
    for entity_id, embedding_blob, content_hash, status in rows:
        grouped_vectors.setdefault(entity_id, []).append(embedding_blob)
        # All rows for one entity share identical content_hash/status by construction (the
        # join predicate ties every row to entities' current committed values) -- record once.
        if entity_id not in observed_state:
            observed_state[entity_id] = (content_hash, status)

    for entity_id, blobs in grouped_vectors.items():
        try:
            vectors = [np.frombuffer(b, dtype=np.float32) for b in blobs]
            centroids[entity_id] = _centroid(vectors)
        except Exception as e:
            logger.warning(
                "get_fresh_entity_centroids: centroid computation failed for %s (fresh path): %s",
                entity_id,
                e,
            )
            unresolved[entity_id] = f"centroid computation failed: {e}"
            observed_state.pop(entity_id, None)

    # Fallback path, wrapped in try/except (mirrors memory_service._batch_semantic_similarities'
    # degradation convention): for any id with zero fresh rows, one query for its current
    # full_content/content_hash/status, then an on-demand embedding.
    from saltmdb.domain.services.embedding_service import compute_entity_chunk_embeddings

    remaining = [eid for eid in unique_ids if eid not in centroids and eid not in unresolved]
    for entity_id in remaining:
        # P1 (Codex review, bf4qtkp7j / 7a5eba85): row_content_hash/row_status are captured
        # OUTSIDE the try body's happy path so the except handler below can still recover them
        # -- a successful entity-row read (content_hash, status) is real observed state even if
        # the embedding computation that follows it fails or produces nothing. Dropping that
        # state made a valid override_justification unreachable for an active-but-unscorable
        # parent: commit's TOCTOU revalidation hard-rejects any resolved parent with no
        # observed_state entry at all, so a parent that legitimately has "no usable content"
        # could never clear revalidation even with an explicit override. An archived or
        # never-found parent must NOT get an observed_state entry -- those stay hard-rejected,
        # not overrideable, which is exactly what leaving row_status None/"archived" preserves.
        row_content_hash = None
        row_status = None
        try:
            row = conn.execute(
                "SELECT full_content, content_hash, status FROM entities WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if not row:
                unresolved[entity_id] = "entity not found"
                continue

            full_content, content_hash, status = row
            if status == "archived":
                unresolved[entity_id] = (
                    "entity is archived, not eligible as a consolidation/cluster candidate"
                )
                continue
            row_content_hash, row_status = content_hash, status

            chunk_rows = compute_entity_chunk_embeddings(entity_id, full_content or "")
            if not chunk_rows:
                unresolved[entity_id] = "no embeddable content"
                observed_state[entity_id] = (content_hash, status)
                continue

            vectors = [np.array(r["embedding"], dtype=np.float32) for r in chunk_rows]
            centroids[entity_id] = _centroid(vectors)
            observed_state[entity_id] = (content_hash, status)
        except Exception as e:
            logger.warning("get_fresh_entity_centroids: fallback failed for %s: %s", entity_id, e)
            unresolved[entity_id] = f"fallback embedding failed: {e}"
            if row_status is not None:
                observed_state[entity_id] = (row_content_hash, row_status)

    return centroids, unresolved, observed_state


def min_pairwise_cohesion(
    centroids: dict[str, list[float]],
) -> tuple[float, tuple[str, str] | None]:
    """Pure numpy. Returns (min_pairwise_cosine_similarity, offending_pair).

    len(centroids) < 2 -> (1.0, None) (trivial pass, nothing to compare). Otherwise stacks the
    centroids, L2-normalizes, computes the full pairwise cosine similarity matrix, and returns
    the MINIMUM off-diagonal value plus the (entity_id_a, entity_id_b) pair that produced it --
    MIN, not MEAN, so one diluted outlier in an otherwise-cohesive set still fails the gate.
    """
    import numpy as np

    if len(centroids) < 2:
        return 1.0, None

    ids = list(centroids.keys())
    matrix = np.vstack([centroids[i] for i in ids]).astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1e-10
    normalized = matrix / norms

    sim_matrix = np.dot(normalized, normalized.T)
    k = len(ids)
    mask = ~np.eye(k, dtype=bool)
    min_val = float(np.min(sim_matrix[mask]))

    masked = np.where(mask, sim_matrix, np.inf)
    i, j = np.unravel_index(np.argmin(masked), masked.shape)
    return min_val, (ids[i], ids[j])
