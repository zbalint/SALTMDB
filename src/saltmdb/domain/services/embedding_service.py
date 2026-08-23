import threading
import logging
import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

from saltmdb.config import CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS
from saltmdb.utils.chunking import chunk_text

logger = logging.getLogger(__name__)

EMBEDDING_RETRY_DELAYS_S = (1, 5, 30, 120, 600)
EMBEDDING_LEASE_S = 120
RETRIEVAL_EMBEDDING_RETRY_DELAYS_S = EMBEDDING_RETRY_DELAYS_S
RETRIEVAL_EMBEDDING_LEASE_S = EMBEDDING_LEASE_S


def entity_source_hash(title: str, content: str) -> str:
    """Stable source fingerprint for an entity-level embedding."""
    return hashlib.sha256(f"{title or ''}\0{content or ''}".encode("utf-8")).hexdigest()


def retrieval_text_source_hash(retrieval_text: str) -> str:
    """Stable independent fingerprint for caller-supplied retrieval text."""
    return hashlib.sha256((retrieval_text or "").encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def enqueue_embedding_jobs_for_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    title: str,
    content: str,
    content_hash: str,
    *,
    force: bool = True,
) -> bool:
    """Atomically replace active work with jobs for an entity's committed source."""
    current = {"entity": entity_source_hash(title, content), "chunk": content_hash}
    now = _now()
    changed = False
    for kind, source_hash in current.items():
        cancelled = conn.execute(
            "UPDATE embedding_jobs SET state='cancelled', updated_at=?, completed_at=? "
            "WHERE entity_id=? AND job_kind=? AND source_hash != ? "
            "AND state IN ('queued','running','retry_wait')",
            (now, now, entity_id, kind, source_hash),
        )
        changed = changed or cancelled.rowcount > 0
        if force:
            conn.execute(
                "INSERT INTO embedding_jobs (id,entity_id,job_kind,source_hash,state,attempt_count,next_attempt_at,created_at,updated_at) "
                "VALUES (?,?,?,?, 'queued',0,?,?,?) "
                "ON CONFLICT(entity_id,job_kind,source_hash) DO UPDATE SET "
                "state='queued',attempt_count=0,next_attempt_at=excluded.next_attempt_at,lease_expires_at=NULL,"
                "last_error=NULL,updated_at=excluded.updated_at,completed_at=NULL",
                (str(uuid.uuid4()), entity_id, kind, source_hash, now, now, now),
            )
            changed = True
        else:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO embedding_jobs (id,entity_id,job_kind,source_hash,state,attempt_count,next_attempt_at,created_at,updated_at) "
                "VALUES (?,?,?,?, 'queued',0,?,?,?)",
                (str(uuid.uuid4()), entity_id, kind, source_hash, now, now, now),
            )
            changed = changed or inserted.rowcount > 0
    if changed:
        conn.execute(
            "UPDATE entities SET embedding_status='pending' WHERE id=? AND status != 'archived'",
            (entity_id,),
        )
    return changed


def cancel_retrieval_embedding_jobs_for_entity(
    conn: sqlite3.Connection, entity_id: str, *, clear_vector: bool = True
) -> None:
    """Cancel active retrieval-text work and optionally remove its vector row.

    The helper is intentionally independent of ``cancel_embedding_jobs_for_entity``: clearing or
    replacing retrieval text must not touch authoritative entity/chunk jobs or embedding_status.
    """
    now = _now()
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='cancelled',updated_at=?,completed_at=?,"
        "lease_expires_at=NULL WHERE entity_id=? AND state IN ('queued','running','retry_wait')",
        (now, now, entity_id),
    )
    if clear_vector:
        try:
            conn.execute("DELETE FROM retrieval_embeddings WHERE entity_id=?", (entity_id,))
        except sqlite3.Error:
            # Vector support is optional at startup; the durable job state still records the
            # cancellation and ordinary FTS/browse paths remain usable.
            logger.debug("retrieval vector cleanup unavailable for %s", entity_id)


def enqueue_retrieval_embedding_job_for_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    retrieval_text: str | None,
    retrieval_text_hash: str | None,
    *,
    force: bool = True,
) -> bool:
    """Queue retrieval-text embedding work for the committed independent source.

    ``None``/empty text clears the vector and cancels active work.  For a replacement, old active
    work and its vector are removed before the new source is queued, making stale exclusion
    synchronous even if inference fails or a process crashes before persistence.
    """
    if not retrieval_text or not retrieval_text_hash:
        before = conn.execute(
            "SELECT COUNT(*) FROM retrieval_embedding_jobs WHERE entity_id=? "
            "AND state IN ('queued','running','retry_wait')",
            (entity_id,),
        ).fetchone()[0]
        cancel_retrieval_embedding_jobs_for_entity(conn, entity_id, clear_vector=True)
        return bool(before)

    now = _now()
    changed = False
    preserved_succeeded = False
    if not force:
        preserved_succeeded = bool(
            conn.execute(
                "SELECT 1 FROM retrieval_embedding_jobs WHERE entity_id=? AND source_hash=? "
                "AND state='succeeded' LIMIT 1",
                (entity_id, retrieval_text_hash),
            ).fetchone()
        )
    changed_rows = conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='cancelled',updated_at=?,completed_at=?,"
        "lease_expires_at=NULL WHERE entity_id=? AND source_hash != ? "
        "AND state IN ('queued','running','retry_wait')",
        (now, now, entity_id, retrieval_text_hash),
    )
    changed = changed or changed_rows.rowcount > 0
    # Remove the old vector before queueing replacement work.  The current-hash + succeeded-job
    # checks in retrieval_vector_search provide defense in depth for legacy rows, but deleting
    # eagerly makes the no-stale-results guarantee immediate.
    if force or not preserved_succeeded:
        try:
            conn.execute("DELETE FROM retrieval_embeddings WHERE entity_id=?", (entity_id,))
        except sqlite3.Error:
            logger.debug("retrieval vector replacement cleanup unavailable for %s", entity_id)
    if force:
        conn.execute(
            "INSERT INTO retrieval_embedding_jobs "
            "(id,entity_id,source_hash,state,attempt_count,next_attempt_at,created_at,updated_at) "
            "VALUES (?,?,?,?,0,?,?,?) "
            "ON CONFLICT(entity_id,source_hash) DO UPDATE SET "
            "state='queued',attempt_count=0,next_attempt_at=excluded.next_attempt_at,"
            "lease_expires_at=NULL,last_error=NULL,updated_at=excluded.updated_at,completed_at=NULL",
            (str(uuid.uuid4()), entity_id, retrieval_text_hash, "queued", now, now, now),
        )
        changed = True
    else:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO retrieval_embedding_jobs "
            "(id,entity_id,source_hash,state,attempt_count,next_attempt_at,created_at,updated_at) "
            "VALUES (?,?,?,?,0,?,?,?)",
            (str(uuid.uuid4()), entity_id, retrieval_text_hash, "queued", now, now, now),
        )
        changed = changed or inserted.rowcount > 0
    return changed


def cancel_embedding_jobs_for_entity(conn: sqlite3.Connection, entity_id: str) -> None:
    now = _now()
    conn.execute(
        "UPDATE embedding_jobs SET state='cancelled', updated_at=?, completed_at=? "
        "WHERE entity_id=? AND state IN ('queued','running','retry_wait')",
        (now, now, entity_id),
    )


def clear_embedding_vectors_for_entity(
    conn: sqlite3.Connection, entity_id: str, *, strict: bool = False
) -> None:
    """Remove entity and chunk vectors once an entity stops being searchable.

    Vector tables are optional when sqlite-vec cannot be loaded. Runtime archive paths may
    therefore degrade to job cancellation, while migrations use ``strict=True`` so their
    version marker is not advanced past cleanup that could not actually run.
    """
    from saltmdb.db.vector_schema import try_load_vector_extension

    if not try_load_vector_extension(conn):
        if strict:
            raise sqlite3.OperationalError("sqlite-vec extension unavailable for vector cleanup")
        logger.debug("vector cleanup unavailable for %s", entity_id)
        return
    for table in ("entity_embeddings", "entity_chunk_embeddings"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE entity_id=?", (entity_id,))
        except sqlite3.Error:
            if strict:
                raise
            logger.debug("%s cleanup unavailable for %s", table, entity_id)


def reconcile_embedding_jobs(
    conn: sqlite3.Connection, *, limit: int = 100, after_id: str | None = None
) -> list[str]:
    """Queue one conservative recovery generation for a bounded active-entity page."""
    sql = "SELECT id,title,full_content,content_hash FROM entities WHERE status != 'archived'"
    params: list[Any] = []
    if after_id:
        sql += " AND id > ?"
        params.append(after_id)
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    for entity_id, title, content, content_hash in rows:
        if content_hash:
            # Startup recovery fills gaps and cancels obsolete source work, but
            # must preserve completed/failed diagnostics on every later daemon
            # restart.  New stores explicitly pass force=True.
            enqueue_embedding_jobs_for_entity(
                conn, entity_id, title, content, content_hash, force=False
            )
    return [r[0] for r in rows]


def reconcile_retrieval_embedding_jobs(
    conn: sqlite3.Connection, *, limit: int = 100, after_id: str | None = None
) -> list[str]:
    """Repair missing retrieval-text jobs without disturbing completed diagnostics."""
    sql = (
        "SELECT id,retrieval_text,retrieval_text_hash FROM entities "
        "WHERE status != 'archived' AND retrieval_text IS NOT NULL "
        "AND retrieval_text_hash IS NOT NULL"
    )
    params: list[Any] = []
    if after_id:
        sql += " AND id > ?"
        params.append(after_id)
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    for entity_id, text, text_hash in rows:
        enqueue_retrieval_embedding_job_for_entity(conn, entity_id, text, text_hash, force=False)
    return [row[0] for row in rows]


class EmbedJobScheduler:
    """Claim durable jobs, infer outside SQLite, and persist results through the coordinator."""

    def __init__(self, coordinator, *, poll_interval_s: float = 0.1):
        self._coordinator = coordinator
        self._poll_interval_s = poll_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saltmdb-embed")
        self._capacity = threading.Semaphore(2)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="saltmdb-embed-scheduler"
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            current_poll_interval = self._poll_interval_s
            max_poll_interval = 5.0

            while not self._stop.is_set():
                if not self._capacity.acquire(blocking=False):
                    self._stop.wait(current_poll_interval)
                    continue
                try:
                    snapshot = self._coordinator.submit(
                        "claim_embedding_job", _claim_any_embedding_job, priority="background"
                    )
                except Exception as submit_exc:
                    if "DAEMON_SHUTTING_DOWN" not in str(submit_exc):
                        logger.exception("embedding job claim failed")
                    self._capacity.release()
                    self._stop.wait(current_poll_interval)
                    continue
                except BaseException:
                    self._capacity.release()
                    raise
                if snapshot:
                    current_poll_interval = self._poll_interval_s
                    try:
                        future = self._pool.submit(self._infer_and_persist, snapshot)

                        def _release_if_cancelled(f):
                            if f.cancelled():
                                self._capacity.release()

                        future.add_done_callback(_release_if_cancelled)
                    except Exception as exc:
                        self._mark_failure(snapshot, exc, release_capacity=True)
                else:
                    self._capacity.release()
                    self._stop.wait(current_poll_interval)
                    current_poll_interval = min(current_poll_interval * 2.0, max_poll_interval)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _infer_and_persist(self, snapshot: dict[str, Any]) -> None:
        try:
            if snapshot["job_kind"] == "entity":
                payload: Any = embed_text(f"{snapshot['title']}\n\n{snapshot['content']}")
            elif snapshot["job_kind"] == "retrieval":
                payload = embed_text(snapshot["retrieval_text"])
            else:
                payload = compute_entity_chunk_embeddings(
                    snapshot["entity_id"], snapshot["content"]
                )
            future = self._coordinator.submit(
                f"persist_{snapshot['job_kind']}_embedding",
                lambda conn: (
                    _persist_retrieval_embedding_if_current(conn, snapshot, payload)
                    if snapshot["job_kind"] == "retrieval"
                    else _persist_embedding_if_current(conn, snapshot, payload)
                ),
                priority="background",
                wait=False,
            )
            future.add_done_callback(lambda done: self._persist_done(snapshot, done))
        except Exception as exc:
            self._mark_failure(snapshot, exc, release_capacity=True)
        except BaseException:
            self._capacity.release()
            raise

    def _persist_done(self, snapshot: dict[str, Any], future) -> None:
        try:
            future.result()
            self._capacity.release()
        except Exception as exc:
            self._mark_failure(snapshot, exc, release_capacity=True)
        except BaseException:
            self._capacity.release()
            raise

    def _mark_failure(
        self, snapshot: dict[str, Any], exc: BaseException, *, release_capacity: bool = False
    ) -> None:
        try:
            future = self._coordinator.submit(
                "retry_embedding_job",
                lambda conn: (
                    _retry_retrieval_embedding_job(conn, snapshot["id"], str(exc))
                    if snapshot.get("job_kind") == "retrieval"
                    else _retry_embedding_job(conn, snapshot["id"], str(exc))
                ),
                priority="background",
                wait=False,
            )
            if release_capacity:
                future.add_done_callback(lambda _: self._capacity.release())
        except Exception as submit_exc:
            if "DAEMON_SHUTTING_DOWN" not in str(submit_exc):
                logger.exception(f"could not record embedding failure for job {snapshot['id']}")
            if release_capacity:
                self._capacity.release()


def _claim_embedding_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    now = _now()
    # A crashed worker already consumed an attempt.  Never let lease recovery
    # turn a fifth crash into unbounded sixth/seventh claims.
    conn.execute(
        "UPDATE embedding_jobs SET state='failed',last_error='embedding lease expired after retry limit',updated_at=?,completed_at=?,lease_expires_at=NULL "
        "WHERE state='running' AND lease_expires_at < ? AND attempt_count >= ?",
        (now, now, now, len(EMBEDDING_RETRY_DELAYS_S)),
    )
    conn.execute(
        "UPDATE embedding_jobs SET state='retry_wait', next_attempt_at=?, lease_expires_at=NULL, updated_at=? "
        "WHERE state='running' AND lease_expires_at < ?",
        (now, now, now),
    )
    row = conn.execute(
        "SELECT j.id,j.entity_id,j.job_kind,j.source_hash,j.attempt_count,e.title,e.full_content,e.content_hash "
        "FROM embedding_jobs j JOIN entities e ON e.id=j.entity_id "
        "WHERE j.state IN ('queued','retry_wait') AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?) "
        "AND e.status != 'archived' ORDER BY COALESCE(j.next_attempt_at,j.created_at),j.created_at,j.id LIMIT 1",
        (now,),
    ).fetchone()
    if not row:
        return None
    job_id, entity_id, kind, source_hash, attempts, title, content, content_hash = row
    lease = (datetime.now(UTC) + timedelta(seconds=EMBEDDING_LEASE_S)).isoformat()
    conn.execute(
        "UPDATE embedding_jobs SET state='running',attempt_count=attempt_count+1,lease_expires_at=?,updated_at=? WHERE id=?",
        (lease, now, job_id),
    )
    return {
        "id": job_id,
        "entity_id": entity_id,
        "job_kind": kind,
        "source_hash": source_hash,
        "attempt_count": attempts + 1,
        "title": title,
        "content": content,
        "content_hash": content_hash,
    }


def _persist_embedding_if_current(
    conn: sqlite3.Connection, snapshot: dict[str, Any], payload: Any
) -> None:
    row = conn.execute(
        "SELECT state,source_hash FROM embedding_jobs WHERE id=?", (snapshot["id"],)
    ).fetchone()
    entity = conn.execute(
        "SELECT title,full_content,content_hash,status FROM entities WHERE id=?",
        (snapshot["entity_id"],),
    ).fetchone()
    if (
        not row
        or row[0] != "running"
        or row[1] != snapshot["source_hash"]
        or not entity
        or entity[3] == "archived"
    ):
        return
    current_hash = (
        entity_source_hash(entity[0], entity[1]) if snapshot["job_kind"] == "entity" else entity[2]
    )
    if current_hash != snapshot["source_hash"]:
        now = _now()
        conn.execute(
            "UPDATE embedding_jobs SET state='cancelled',updated_at=?,completed_at=?,lease_expires_at=NULL WHERE id=? AND state='running'",
            (now, now, snapshot["id"]),
        )
        return
    if sqlite_vec is None:
        return
    if snapshot["job_kind"] == "entity":
        conn.execute("DELETE FROM entity_embeddings WHERE entity_id=?", (snapshot["entity_id"],))
        conn.execute(
            "INSERT INTO entity_embeddings(entity_id,embedding) VALUES (?,?)",
            (snapshot["entity_id"], sqlite_vec.serialize_float32(payload)),
        )
        conn.execute(
            "UPDATE entities SET embedding_status='ready' WHERE id=?", (snapshot["entity_id"],)
        )
    else:
        conn.execute(
            "DELETE FROM entity_chunk_embeddings WHERE entity_id=?", (snapshot["entity_id"],)
        )
        if payload:
            conn.executemany(
                "INSERT INTO entity_chunk_embeddings (id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        r["id"],
                        r["entity_id"],
                        sqlite_vec.serialize_float32(r["embedding"]),
                        r["chunk_index"],
                        r["char_start"],
                        r["char_end"],
                        snapshot["source_hash"],
                    )
                    for r in payload
                ],
            )
    now = _now()
    conn.execute(
        "UPDATE embedding_jobs SET state='succeeded',updated_at=?,completed_at=?,lease_expires_at=NULL,last_error=NULL WHERE id=?",
        (now, now, snapshot["id"]),
    )


def _retry_embedding_job(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    row = conn.execute(
        "SELECT attempt_count,state FROM embedding_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row or row[1] != "running":
        return
    now = _now()
    if row[0] >= len(EMBEDDING_RETRY_DELAYS_S):
        conn.execute(
            "UPDATE embedding_jobs SET state='failed',last_error=?,updated_at=?,completed_at=?,lease_expires_at=NULL WHERE id=?",
            (error[:2000], now, now, job_id),
        )
        return
    due = (datetime.now(UTC) + timedelta(seconds=EMBEDDING_RETRY_DELAYS_S[row[0] - 1])).isoformat()
    conn.execute(
        "UPDATE embedding_jobs SET state='retry_wait',next_attempt_at=?,last_error=?,updated_at=?,lease_expires_at=NULL WHERE id=?",
        (due, error[:2000], now, job_id),
    )


def _claim_retrieval_embedding_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Claim one due retrieval-text embedding with the same lease/retry semantics as base jobs."""
    now = _now()
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='failed',last_error='embedding lease expired after retry limit',"
        "updated_at=?,completed_at=?,lease_expires_at=NULL WHERE state='running' "
        "AND lease_expires_at < ? AND attempt_count >= ?",
        (now, now, now, len(RETRIEVAL_EMBEDDING_RETRY_DELAYS_S)),
    )
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='retry_wait',next_attempt_at=?,"
        "lease_expires_at=NULL,updated_at=? WHERE state='running' AND lease_expires_at < ?",
        (now, now, now),
    )
    row = conn.execute(
        "SELECT j.id,j.entity_id,j.source_hash,j.attempt_count,e.retrieval_text,"
        "e.retrieval_text_hash FROM retrieval_embedding_jobs j JOIN entities e ON e.id=j.entity_id "
        "WHERE j.state IN ('queued','retry_wait') AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?) "
        "AND e.status != 'archived' AND e.retrieval_text IS NOT NULL "
        "AND e.retrieval_text_hash = j.source_hash "
        "ORDER BY COALESCE(j.next_attempt_at,j.created_at),j.created_at,j.id LIMIT 1",
        (now,),
    ).fetchone()
    if not row:
        return None
    job_id, entity_id, source_hash, attempts, retrieval_text, current_hash = row
    lease = (datetime.now(UTC) + timedelta(seconds=RETRIEVAL_EMBEDDING_LEASE_S)).isoformat()
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='running',attempt_count=attempt_count+1,"
        "lease_expires_at=?,updated_at=? WHERE id=?",
        (lease, now, job_id),
    )
    return {
        "id": job_id,
        "entity_id": entity_id,
        "job_kind": "retrieval",
        "source_hash": source_hash,
        "attempt_count": attempts + 1,
        "retrieval_text": retrieval_text,
        "retrieval_text_hash": current_hash,
    }


def _persist_retrieval_embedding_if_current(
    conn: sqlite3.Connection, snapshot: dict[str, Any], payload: list[float]
) -> None:
    """Persist only a currently leased, hash-matching retrieval vector."""
    row = conn.execute(
        "SELECT state,source_hash FROM retrieval_embedding_jobs WHERE id=?", (snapshot["id"],)
    ).fetchone()
    entity = conn.execute(
        "SELECT retrieval_text,retrieval_text_hash,status FROM entities WHERE id=?",
        (snapshot["entity_id"],),
    ).fetchone()
    if (
        not row
        or row[0] != "running"
        or row[1] != snapshot["source_hash"]
        or not entity
        or entity[2] == "archived"
        or not entity[0]
        or entity[1] != snapshot["source_hash"]
    ):
        return
    if sqlite_vec is None:
        raise RuntimeError("sqlite_vec is unavailable for retrieval embedding persistence")
    conn.execute("DELETE FROM retrieval_embeddings WHERE entity_id=?", (snapshot["entity_id"],))
    conn.execute(
        "INSERT INTO retrieval_embeddings(entity_id,embedding,source_hash) VALUES (?,?,?)",
        (
            snapshot["entity_id"],
            sqlite_vec.serialize_float32(payload),
            snapshot["source_hash"],
        ),
    )
    now = _now()
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='succeeded',updated_at=?,completed_at=?,"
        "lease_expires_at=NULL,last_error=NULL WHERE id=?",
        (now, now, snapshot["id"]),
    )


def _retry_retrieval_embedding_job(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    row = conn.execute(
        "SELECT attempt_count,state FROM retrieval_embedding_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not row or row[1] != "running":
        return
    now = _now()
    if row[0] >= len(RETRIEVAL_EMBEDDING_RETRY_DELAYS_S):
        conn.execute(
            "UPDATE retrieval_embedding_jobs SET state='failed',last_error=?,updated_at=?,"
            "completed_at=?,lease_expires_at=NULL WHERE id=?",
            (error[:2000], now, now, job_id),
        )
        return
    due = (
        datetime.now(UTC) + timedelta(seconds=RETRIEVAL_EMBEDDING_RETRY_DELAYS_S[row[0] - 1])
    ).isoformat()
    conn.execute(
        "UPDATE retrieval_embedding_jobs SET state='retry_wait',next_attempt_at=?,last_error=?,"
        "updated_at=?,lease_expires_at=NULL WHERE id=?",
        (due, error[:2000], now, job_id),
    )


def _claim_any_embedding_job(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Claim base work first, then optional retrieval-text work for the shared scheduler."""
    snapshot = _claim_embedding_job(conn)
    return snapshot if snapshot is not None else _claim_retrieval_embedding_job(conn)


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


# BGE (BAAI/bge-small-en-v1.5, the model get_model() loads) is an asymmetric embedding model:
# it was trained with a query-side instruction prefix that document-side text never gets, and
# omitting it on the query side leaves retrieval quality on the table. embed_text/embed_texts
# above are used for BOTH document- and query-side embedding today; embed_query_text/
# embed_query_texts exist so every QUERY-side call site can opt into the prefix while every
# document-side call site (embed_entity_async, compute_entity_chunk_embeddings) stays on the
# plain functions unchanged. See candidate/search-bge-query-prefix's design record for the
# retest rationale (memory 254c28f8 / f73cf633).
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def embed_query_text(text: str) -> list[float]:
    """Encode a QUERY string, prefixed for BGE's asymmetric query/document training.

    Preserves embed_text's empty/whitespace-string contract (returns [0.0]*384 without ever
    prefixing or embedding an empty query) so callers can't observe a behavior difference on
    that edge case between embed_text and this function.
    """
    if not text or not text.strip():
        return [0.0] * 384
    return embed_text(_BGE_QUERY_PREFIX + text)


def embed_query_texts(texts: list[str]) -> list[list[float]]:
    """Batch form of embed_query_text -- see embed_texts for the batching/empty-slot contract.

    Only non-empty entries get the BGE query prefix prepended before batching; empty/whitespace
    entries are left as "" so embed_texts' own empty-slot handling still returns [0.0]*384 for
    them without ever prefixing an empty string.
    """
    prefixed = [_BGE_QUERY_PREFIX + t if t and t.strip() else t for t in texts]
    return embed_texts(prefixed)


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


def process_embedding_jobs_sync(conn) -> None:
    """Test utility to synchronously process all pending embedding jobs on the caller thread."""
    from saltmdb.db.connection import write_transaction

    while True:
        with write_transaction(conn):
            snapshot = _claim_any_embedding_job(conn)
        if not snapshot:
            break
        try:
            if snapshot["job_kind"] == "entity":
                payload: list[float] | list[dict[str, Any]] = embed_text(
                    f"{snapshot['title']}\n\n{snapshot['content']}"
                )
            elif snapshot["job_kind"] == "retrieval":
                payload = embed_text(snapshot["retrieval_text"])
            else:
                payload = compute_entity_chunk_embeddings(
                    snapshot["entity_id"], snapshot["content"]
                )
        except Exception as exc:
            with write_transaction(conn):
                if snapshot.get("job_kind") == "retrieval":
                    _retry_retrieval_embedding_job(conn, snapshot["id"], str(exc))
                else:
                    _retry_embedding_job(conn, snapshot["id"], str(exc))
            continue
        with write_transaction(conn):
            if snapshot.get("job_kind") == "retrieval":
                _persist_retrieval_embedding_if_current(conn, snapshot, cast(list[float], payload))
            else:
                _persist_embedding_if_current(conn, snapshot, payload)
