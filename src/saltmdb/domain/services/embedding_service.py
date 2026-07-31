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
