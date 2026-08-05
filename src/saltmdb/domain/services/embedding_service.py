import threading
import logging

logger = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model = None


import os  # noqa: E402


def _is_valid_local_model(model_dir: str) -> bool:
    """Verify that bundled model directory exists and contains non-pointer binary weights."""
    if not os.path.isdir(model_dir):
        return False
    onnx_file = os.path.join(model_dir, "model_optimized.onnx")
    if not os.path.isfile(onnx_file):
        return False
    try:
        # Check size > 10MB to avoid un-pulled Git LFS pointer files (~130 bytes)
        if os.path.getsize(onnx_file) < 10 * 1024 * 1024:
            logger.warning(
                "Bundled model file %s is too small (likely an un-fetched Git LFS pointer). Skipping local load.",
                onnx_file,
            )
            return False
    except OSError:
        return False
    return True


def get_model():
    """Lazily load the fastembed TextEmbedding model once per process."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding

                local_model_dir = os.path.abspath(
                    os.path.join(
                        os.path.dirname(__file__), "..", "..", "models", "bge-small-en-v1.5"
                    )
                )
                if _is_valid_local_model(local_model_dir):
                    logger.info("Loading bundled ONNX embedding model from %s", local_model_dir)
                    try:
                        _model = TextEmbedding(
                            model_name="BAAI/bge-small-en-v1.5",
                            cache_dir=os.path.dirname(local_model_dir),
                            local_files_only=True,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to load bundled model from %s: %s. Falling back to online model load.",
                            local_model_dir,
                            e,
                        )
                        _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
                else:
                    logger.info(
                        "Bundled model not present or invalid at %s. Falling back to online model load.",
                        local_model_dir,
                    )
                    _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _model


def embed_text(text: str) -> list[float]:
    """Encode text to a 384-dim normalized float vector using fastembed."""
    if not text or not text.strip():
        return [0.0] * 384
    model = get_model()
    embeddings = list(model.embed([text]))
    if not embeddings:
        return [0.0] * 384
    return embeddings[0].tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Encode a batch of texts to 384-dim vectors using fastembed's native batching.

    Every chunk-based caller (compute_entity_chunk_embeddings and, eventually, whichever later
    rework phase reranks/clusters on chunks) needs to embed many short strings per call --
    looping embed_text() one-at-a-time here would reintroduce the exact unbatched-embedding
    slowdown already fixed once elsewhere for store_memory's dedup path (a ~9x regression).
    fastembed's TextEmbedding.embed() batches internally when given a list, so this is a single
    model call regardless of len(texts).

    Preserves embed_text's empty/whitespace-string contract (returns [0.0]*384, never fed to the
    model -- the ONNX tokenizer's behavior on "" is untested territory here, not worth risking)
    per item, while keeping the returned list's index alignment with `texts` intact regardless of
    how many empty entries are interspersed among real ones.
    """
    if not texts:
        return []

    non_empty_idx = [i for i, t in enumerate(texts) if t and t.strip()]
    results: list[list[float]] = [[0.0] * 384 for _ in texts]

    if non_empty_idx:
        model = get_model()
        batch = [texts[i] for i in non_empty_idx]
        embeddings = list(model.embed(batch))
        for pos, idx in enumerate(non_empty_idx):
            results[idx] = embeddings[pos].tolist()

    return results


def embed_entity_async(entity_id: str, title: str, full_content: str, db_path: str) -> None:
    """Background thread target: generate and persist an embedding for one entity.

    Runs on memory_service._embed_pool, fed directly by every store_memory call -- a burst of
    stores queues a backlog of these that keeps draining in the background long after the
    triggering session goes quiet. Unlike every other write path in this codebase, this used
    to issue three separate raw autocommit statements (no BEGIN IMMEDIATE, no
    write_transaction_retrying) instead of one atomic, retried transaction. That was a real
    correctness gap (a crash between DELETE and INSERT could drop an embedding), but it also
    meant this was the one writer with no jittered backoff between attempts: a queued run of
    these instantly retries via SQLite's own busy_timeout, with zero pause between jobs, while
    every foreground caller backs off (write_transaction_retrying's exponential
    backoff+jitter) between its own bounded retries. Under SQLite's non-FIFO lock queue, a
    continuously-hammering non-backing-off writer systematically starves a writer that
    politely waits between attempts -- so a long enough embedding backlog on one session could
    make another session's store_memory calls fail with "database is locked" on every single
    attempt, for as long as the backlog keeps feeding new jobs. Routing this through the same
    write_transaction_retrying used everywhere else fixes both problems: atomic write, and
    the same backoff/retry fairness as every other writer.
    """
    import sqlite_vec
    from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection

    conn = get_connection(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        text = f"{title}\n\n{full_content}"
        vector = embed_text(text)

        def _write(c):
            c.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
            c.execute(
                "INSERT INTO entity_embeddings(entity_id, embedding) VALUES (?, ?)",
                (entity_id, sqlite_vec.serialize_float32(vector)),
            )
            c.execute("UPDATE entities SET embedding_status = 'ready' WHERE id = ?", (entity_id,))

        write_transaction_retrying(conn, _write)
        logger.debug("Embedding stored for entity %s", entity_id)
    except Exception as e:
        try:

            def _mark_failed(c):
                c.execute(
                    "UPDATE entities SET embedding_status = 'failed' WHERE id = ?", (entity_id,)
                )

            write_transaction_retrying(conn, _mark_failed)
        except Exception:
            pass
        logger.error("Embedding generation failed for %s: %s", entity_id, e)
    finally:
        close_connection(conn)


def backfill_pending_embeddings(db_path: str = None) -> int:
    """Scans for active entities where embedding_status = 'pending' or NULL and queues embedding generation."""
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection
    from saltmdb.domain.services.memory_service import _embed_pool

    db_path = db_path or get_db_path()
    try:
        conn = get_connection(db_path)
        rows = conn.execute(
            "SELECT id, title, full_content FROM entities "
            "WHERE (embedding_status = 'pending' OR embedding_status IS NULL OR embedding_status = '') "
            "AND status != 'archived'"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("Error fetching pending embeddings for backfill: %s", e)
        return 0

    for eid, title, content in rows:
        _embed_pool.submit(embed_entity_async, eid, title, content, db_path)
    return len(rows)


def compute_entity_chunk_embeddings(entity_id: str, full_content: str) -> list[dict]:
    """Chunk full_content and batch-embed each chunk. Pure, no DB I/O.

    Returns dicts ready for insertion into entity_chunk_embeddings: {id, entity_id, embedding,
    chunk_index, char_start, char_end}.

    Chunks full_content ALONE -- deliberately not title-prefixed the way embed_entity_async's
    entity-level text is (f"{title}\n\n{full_content}"). char_start/char_end are meant to be
    directly usable offsets into full_content with no prefix-length arithmetic required by any
    consumer. Whether/how title should factor into chunk-level retrieval is left to whichever
    later rework phase first consumes this table.
    """
    from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
    from saltmdb.utils.chunking import chunk_text

    chunks = chunk_text(full_content or "", CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
    if not chunks:
        return []

    vectors = embed_texts([c["text"] for c in chunks])
    return [
        {
            "id": f"{entity_id}::{i}",
            "entity_id": entity_id,
            "embedding": vec,
            "chunk_index": i,
            "char_start": c["char_start"],
            "char_end": c["char_end"],
        }
        for i, (c, vec) in enumerate(zip(chunks, vectors))
    ]


def write_entity_chunk_embeddings(
    entity_id: str,
    full_content: str,
    db_path: str,
    expected_content_hash: str | None = None,
) -> int:
    """Compute and persist chunk-level embeddings for one entity: DELETE existing rows for
    entity_id, then INSERT fresh ones, in one write_transaction_retrying call -- mirrors
    embed_entity_async's DELETE+INSERT atomicity on entity_embeddings. Does NOT touch
    entities.embedding_status -- that column tracks the existing entity-level embed path;
    chunk-embedding freshness tracking is a decision for whichever later phase first consumes
    this table.

    Stale-write guard: when expected_content_hash is given (as backfill_chunk_embeddings does,
    passing the hash it captured at selection time), the entity's CURRENT content_hash and
    status are re-read fresh INSIDE this write transaction and compared against it before
    writing anything. If the entity was edited (different content_hash) or archived since the
    caller read its content, this is a no-op (returns 0, no rows written, any existing chunk
    rows for this entity are left exactly as they were) -- closing the race window between
    "caller read this entity's content" and "this transaction actually commits chunks for it".
    Pass expected_content_hash=None (the default) to skip the guard entirely, e.g. for a caller
    that just wrote/read the entity itself inside the same logical operation and already knows
    it's current.

    Every row written carries a content_hash value (Part A0): expected_content_hash itself when
    the guard is active (it's already been verified fresh, inside this same transaction, to
    match entities.content_hash right above), or a freshly computed
    compute_content_hash(full_content) when the guard is skipped -- mirroring the same value
    store_memory/commit_consolidation already computed/committed for entities.content_hash at
    that point, so it's consistent by construction rather than recomputed independently. This is
    what lets the startup repair sweep (backfill_chunk_embeddings) tell "current" rows from
    "stale" ones instead of only "present" from "missing".

    Returns the number of chunk rows written (0 on a guard skip or on empty/unchunkable content).
    """
    import sqlite_vec
    from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
    from saltmdb.utils.text import compute_content_hash

    rows = compute_entity_chunk_embeddings(entity_id, full_content)
    row_content_hash = (
        expected_content_hash
        if expected_content_hash is not None
        else compute_content_hash(full_content or "")
    )

    conn = get_connection(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        def _write(c):
            if expected_content_hash is not None:
                current = c.execute(
                    "SELECT content_hash, status FROM entities WHERE id = ?", (entity_id,)
                ).fetchone()
                if not current or current[1] == "archived" or current[0] != expected_content_hash:
                    return 0

            c.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (entity_id,))
            if rows:
                c.executemany(
                    "INSERT INTO entity_chunk_embeddings"
                    "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            r["id"],
                            r["entity_id"],
                            sqlite_vec.serialize_float32(r["embedding"]),
                            r["chunk_index"],
                            r["char_start"],
                            r["char_end"],
                            row_content_hash,
                        )
                        for r in rows
                    ],
                )
            return len(rows)

        return write_transaction_retrying(conn, _write)
    finally:
        close_connection(conn)


def backfill_chunk_embeddings(db_path: str = None, limit: int = None) -> int:
    """Manually-invocable backfill/repair sweep: computes and stores chunk embeddings for active
    entities that have no chunk rows yet OR whose existing chunk rows are stale (Part A3 -- see
    plans/ and SALTMDB memory `5c09effa`).

    As of Phase 2 Part A, this table IS wired into the live write path: store_memory and
    commit_consolidation both trigger write_entity_chunk_embeddings on memory_service._embed_pool
    (fire-and-forget, same pool as the entity-level embed). This function is a separate,
    synchronous repair pass over that -- invoked explicitly on demand (e.g. via
    `python -m saltmdb --backfill-chunk-embeddings`) and unconditionally at normal server startup
    (see __main__.py, right after backfill_pending_embeddings()) -- catching anything an async
    job never completed or completed incorrectly: never-chunked entities, a failed/interrupted
    job, Foundation-era rows that predate this column, or (defense-in-depth) any stale write that
    somehow slipped past the hot-path guard. Runs synchronously in-process (not via
    memory_service._embed_pool), per user decision -- self-contained and easy to reason about for
    a startup sweep, at the cost of blocking startup for its duration.

    Captures each candidate's content_hash at selection time and passes it through to
    write_entity_chunk_embeddings as expected_content_hash, so the staleness guard there has
    something real to compare against for this function's inherent read-then-write-later pattern
    (a long backfill run scanning many entities gives real time for another writer to touch one
    of them mid-run). Rows with a NULL/empty content_hash (schema.py's migration should populate
    this for every active entity, but a caller running against a DB that hasn't been through a
    current-code init_db() yet could still see one) are skipped rather than written: forwarding
    None as expected_content_hash is write_entity_chunk_embeddings' explicit "skip the staleness
    guard entirely" signal, and a locally-computed fingerprint can't substitute for it here --
    the guard's write-time comparison reads the *stored* content_hash column fresh inside its own
    transaction, so a value we only hold locally would never match it, silently discarding every
    write. A skipped entity picks up a real content_hash (and becomes eligible again) on its next
    store_memory write, or the next init_db() run.

    Returns the count of entities actually written (excludes any skipped by the staleness guard
    or by a still-missing content_hash).
    """
    import sqlite_vec
    from saltmdb.config import get_db_path
    from saltmdb.db.connection import get_connection

    db_path = db_path or get_db_path()
    conn = get_connection(db_path)
    try:
        # The subquery below queries entity_chunk_embeddings, a vec0 virtual table -- querying
        # it (like any operation on it) requires sqlite_vec loaded on THIS connection object
        # specifically, not just imported in-process (see init_vector_schema's docstring for why
        # per-connection loading is a separate step from the module import).
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        # Selects entities with NO chunk rows OR STALE chunk rows (Part A3, post-A0's
        # content_hash column). The stale-row branch uses `c.content_hash IS NULL OR
        # c.content_hash IS NOT e.content_hash`, not a bare `!=`: SQL's `!=`/`<>` yields neither
        # true nor false when either side is NULL (three-valued logic), so
        # `c.content_hash != e.content_hash` would silently fail to match any row with a NULL
        # content_hash -- exactly the malformed/legacy-row case this branch exists to catch.
        # `IS NOT` is SQLite's NULL-safe inequality operator and handles this correctly on its
        # own; the explicit `IS NULL OR` is kept for readability/defensiveness rather than
        # relying on `IS NOT`'s NULL semantics being obvious to the next reader.
        query = (
            "SELECT e.id, e.full_content, e.content_hash FROM entities e "
            "WHERE e.status != 'archived' "
            "AND ("
            "NOT EXISTS (SELECT 1 FROM entity_chunk_embeddings c WHERE c.entity_id = e.id) "
            "OR EXISTS ("
            "SELECT 1 FROM entity_chunk_embeddings c WHERE c.entity_id = e.id "
            "AND (c.content_hash IS NULL OR c.content_hash IS NOT e.content_hash)"
            ")"
            ")"
        )
        if limit:
            rows = conn.execute(query + " LIMIT ?", (int(limit),)).fetchall()
        else:
            rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    written = 0
    for eid, content, content_hash in rows:
        if not content_hash:
            # See docstring: forwarding this as expected_content_hash=None would silently
            # disable the staleness guard for this entity (Codex re-review finding, Foundation
            # phase) -- skip instead of writing unguarded.
            logger.warning(
                "Skipping chunk backfill for entity %s: content_hash not yet populated "
                "(run init_db() to migrate legacy rows)",
                eid,
            )
            continue
        count = write_entity_chunk_embeddings(
            eid, content, db_path, expected_content_hash=content_hash
        )
        if count > 0:
            written += 1
    return written
