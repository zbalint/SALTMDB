import threading
import logging

logger = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model = None


def get_model():
    """Lazily load the fastembed TextEmbedding model once per process."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from fastembed import TextEmbedding
                _model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _model


def embed_text(text: str) -> list[float]:
    """Encode text to a 384-dim normalized float vector using fastembed."""
    model = get_model()
    return list(model.embed([text]))[0].tolist()


def embed_entity_async(entity_id: str, title: str, full_content: str, db_path: str) -> None:
    """Background thread target: generate and persist an embedding for one entity."""
    import sqlite_vec
    from saltmdb.db.connection import get_connection

    conn = get_connection(db_path)
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        text = f"{title}\n\n{full_content}"
        vector = embed_text(text)
        conn.execute(
            "INSERT OR REPLACE INTO entity_embeddings(entity_id, embedding) VALUES (?, ?)",
            (entity_id, sqlite_vec.serialize_float32(vector))
        )
        conn.execute(
            "UPDATE entities SET embedding_status = 'ready' WHERE id = ?",
            (entity_id,)
        )
        conn.commit()
        logger.debug("Embedding stored for entity %s", entity_id)
    except Exception as e:
        try:
            conn.execute(
                "UPDATE entities SET embedding_status = 'failed' WHERE id = ?",
                (entity_id,)
            )
            conn.commit()
        except Exception:
            pass
        logger.error("Embedding generation failed for %s: %s", entity_id, e)
    finally:
        conn.close()
