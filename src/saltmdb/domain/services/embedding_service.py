import threading
import logging
import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

EMBEDDING_RETRY_DELAYS_S = (1, 5, 30, 120, 600)
EMBEDDING_LEASE_S = 120


def entity_source_hash(title: str, content: str) -> str:
    """Stable source fingerprint for an entity-level embedding."""
    return hashlib.sha256(f"{title or ''}\0{content or ''}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def enqueue_embedding_jobs_for_entity(
    conn: sqlite3.Connection, entity_id: str, title: str, content: str, content_hash: str, *, force: bool = True
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
        conn.execute("UPDATE entities SET embedding_status='pending' WHERE id=? AND status != 'archived'", (entity_id,))
    return changed


def cancel_embedding_jobs_for_entity(conn: sqlite3.Connection, entity_id: str) -> None:
    now = _now()
    conn.execute(
        "UPDATE embedding_jobs SET state='cancelled', updated_at=?, completed_at=? "
        "WHERE entity_id=? AND state IN ('queued','running','retry_wait')",
        (now, now, entity_id),
    )


def reconcile_embedding_jobs(conn: sqlite3.Connection, *, limit: int = 100, after_id: str | None = None) -> list[str]:
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
            enqueue_embedding_jobs_for_entity(conn, entity_id, title, content, content_hash, force=False)
    return [r[0] for r in rows]


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
            self._thread = threading.Thread(target=self._run, daemon=True, name="saltmdb-embed-scheduler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            # Never lease more durable jobs than the inference executor can
            # actually run.  Otherwise queued executor work can expire and be
            # claimed a second time before its first inference starts.
            if not self._capacity.acquire(blocking=False):
                continue
            try:
                snapshot = self._coordinator.submit("claim_embedding_job", _claim_embedding_job, priority="background")
            except Exception:
                logger.exception("embedding job claim failed")
                self._capacity.release()
                continue
            if snapshot:
                try:
                    self._pool.submit(self._infer_and_persist, snapshot)
                except Exception as exc:
                    self._mark_failure(snapshot, exc, release_capacity=True)
            else:
                self._capacity.release()

    def _infer_and_persist(self, snapshot: dict[str, Any]) -> None:
        try:
            if snapshot["job_kind"] == "entity":
                payload: Any = embed_text(f"{snapshot['title']}\n\n{snapshot['content']}")
            else:
                payload = compute_entity_chunk_embeddings(snapshot["entity_id"], snapshot["content"])
            future = self._coordinator.submit(
                f"persist_{snapshot['job_kind']}_embedding",
                lambda conn: _persist_embedding_if_current(conn, snapshot, payload),
                priority="background", wait=False,
            )
            future.add_done_callback(lambda done: self._persist_done(snapshot, done))
        except Exception as exc:
            self._mark_failure(snapshot, exc, release_capacity=True)

    def _persist_done(self, snapshot: dict[str, Any], future) -> None:
        try:
            if not future.cancelled():
                exc = future.exception()
                if exc is not None:
                    self._mark_failure(snapshot, exc)
        finally:
            self._capacity.release()

    def _mark_failure(self, snapshot: dict[str, Any], exc: BaseException, *, release_capacity: bool = False) -> None:
        try:
            future = self._coordinator.submit(
                "retry_embedding_job",
                lambda conn: _retry_embedding_job(conn, snapshot["id"], str(exc)),
                priority="background", wait=False,
            )
            if release_capacity:
                future.add_done_callback(lambda _done: self._capacity.release())
        except Exception:
            # Leave the running lease intact; a later scheduler restart/claim recovers it.
            logger.exception("could not record embedding failure for job %s", snapshot["id"])
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
        "WHERE state='running' AND lease_expires_at < ?", (now, now, now)
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
    return {"id": job_id, "entity_id": entity_id, "job_kind": kind, "source_hash": source_hash,
            "attempt_count": attempts + 1, "title": title, "content": content, "content_hash": content_hash}


def _persist_embedding_if_current(conn: sqlite3.Connection, snapshot: dict[str, Any], payload: Any) -> None:
    row = conn.execute("SELECT state,source_hash FROM embedding_jobs WHERE id=?", (snapshot["id"],)).fetchone()
    entity = conn.execute("SELECT title,full_content,content_hash,status FROM entities WHERE id=?", (snapshot["entity_id"],)).fetchone()
    if not row or row[0] != "running" or row[1] != snapshot["source_hash"] or not entity or entity[3] == "archived":
        return
    current_hash = entity_source_hash(entity[0], entity[1]) if snapshot["job_kind"] == "entity" else entity[2]
    if current_hash != snapshot["source_hash"]:
        now = _now()
        conn.execute("UPDATE embedding_jobs SET state='cancelled',updated_at=?,completed_at=?,lease_expires_at=NULL WHERE id=? AND state='running'", (now, now, snapshot["id"]))
        return
    import sqlite_vec
    if snapshot["job_kind"] == "entity":
        conn.execute("DELETE FROM entity_embeddings WHERE entity_id=?", (snapshot["entity_id"],))
        conn.execute("INSERT INTO entity_embeddings(entity_id,embedding) VALUES (?,?)", (snapshot["entity_id"], sqlite_vec.serialize_float32(payload)))
        conn.execute("UPDATE entities SET embedding_status='ready' WHERE id=?", (snapshot["entity_id"],))
    else:
        conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id=?", (snapshot["entity_id"],))
        if payload:
            conn.executemany(
                "INSERT INTO entity_chunk_embeddings (id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) VALUES (?,?,?,?,?,?,?)",
                [(r["id"], r["entity_id"], sqlite_vec.serialize_float32(r["embedding"]), r["chunk_index"], r["char_start"], r["char_end"], snapshot["source_hash"]) for r in payload],
            )
    now = _now()
    conn.execute("UPDATE embedding_jobs SET state='succeeded',updated_at=?,completed_at=?,lease_expires_at=NULL,last_error=NULL WHERE id=?", (now, now, snapshot["id"]))


def _retry_embedding_job(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    row = conn.execute("SELECT attempt_count,state FROM embedding_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row[1] != "running":
        return
    now = _now()
    if row[0] >= len(EMBEDDING_RETRY_DELAYS_S):
        conn.execute("UPDATE embedding_jobs SET state='failed',last_error=?,updated_at=?,completed_at=?,lease_expires_at=NULL WHERE id=?", (error[:2000], now, now, job_id))
        return
    due = (datetime.now(UTC) + timedelta(seconds=EMBEDDING_RETRY_DELAYS_S[row[0] - 1])).isoformat()
    conn.execute("UPDATE embedding_jobs SET state='retry_wait',next_attempt_at=?,last_error=?,updated_at=?,lease_expires_at=NULL WHERE id=?", (due, error[:2000], now, job_id))

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
    """Scans for active entities where embedding_status = 'pending' or NULL and queues embedding
    generation. Fired from the daemon startup sweep and separately from a viewer/routes.py
    maintenance endpoint -- NOT from the manual --backfill-chunk-embeddings CLI flag /
    run_backfill_chunk_embeddings_now RPC, which only invokes backfill_chunk_embeddings (the
    chunk-level sweep) instead."""
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
    (see `daemon/server.py`'s startup sweep, right after backfill_pending_embeddings() -- corrected
    from a stale `__main__.py` reference; `__main__.py` only forwards manual
    `--backfill-chunk-embeddings` CLI invocations to the running daemon over RPC, it is not itself
    the startup call site) -- catching anything an async job never completed or completed
    incorrectly: never-chunked entities, a failed/interrupted job, Foundation-era rows that predate
    this column, or (defense-in-depth) any stale write that somehow slipped past the hot-path
    guard. Runs synchronously in-process (not via memory_service._embed_pool), per user decision --
    self-contained and easy to reason about for a startup sweep, at the cost of blocking startup
    for its duration.

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

    Per-row isolation (H2 fix, memory `ed7cc8d5`): each row's write_entity_chunk_embeddings call is
    wrapped in its own `except Exception` (not `BaseException`) -- a mid-loop exception no longer
    silently aborts every entity after the one that raised. Safe by construction: this function's
    own selection connection is already closed before the loop starts, and each
    write_entity_chunk_embeddings call opens its own connection and uses one retried atomic
    transaction, so a per-row failure cannot leave that entity's chunk rows partially written. The
    OUTER try/except at this function's own call site (daemon startup sweep) is unchanged and still
    protects against selection-query/extension-load failures, which per-row isolation inside this
    loop cannot address.

    Structured six-bucket count logging, always emitted at the end of the sweep (not gated on
    nonzero): `selected` (total rows returned by the selection query, the denominator) and five
    mutually-exclusive per-row outcomes that sum to it -- `written`, `skipped_missing_hash`
    (existing NULL-content_hash branch), `skipped_empty_or_unchunkable` (new precheck below, since
    write_entity_chunk_embeddings() returning 0 is otherwise ambiguous between this and a stale-
    guard skip), `skipped_stale_guard` (a real-content row whose write call returned 0 after the
    empty-content precheck already ruled that out), and `failed` (from the per-row isolation
    above). `selected != written` is NOT inherently anomalous -- stale-guard and missing-hash skips
    are deliberate, expected behavior. A per-row `INFO`-level log line (the daemon's actual default
    level; `DEBUG` would be silently invisible in the deployed configuration this is meant to help
    diagnose) fires at the start of each row's processing, so a hang's last-logged entity id
    identifies where the sweep stopped even though a hang -- as opposed to a raised exception, which
    per-row isolation above already handles -- still prevents this function from ever reaching its
    own final aggregate log line.

    Root cause of any *historical* chunk-embedding escape (why an earlier unconditional sweep found
    and should have fixed a given entity but apparently didn't) stays formally UNRESOLVED by this
    fix -- it makes the failure mode visible and non-silent going forward, it does not retroactively
    explain what happened before.

    Returns the count of entities actually written (`written` bucket -- excludes entities skipped
    by the staleness guard, a missing content_hash, empty/unchunkable content, or a per-row write
    failure).
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

    selected = len(rows)
    written = 0
    skipped_missing_hash = 0
    skipped_empty_or_unchunkable = 0
    skipped_stale_guard = 0
    failed = 0

    for i, (eid, content, content_hash) in enumerate(rows, start=1):
        # Visibility (H2 fix): logged at the start of every row's processing, at INFO (the
        # daemon's actual default level) -- so a hang's last-logged entity id identifies where
        # the sweep stopped, even though a hang itself (as opposed to a raised exception, isolated
        # below) still prevents this function from ever reaching its final aggregate log line.
        logger.info("Backfilling chunk embeddings for entity %s (%d/%d)", eid, i, selected)

        if not content_hash:
            # See docstring: forwarding this as expected_content_hash=None would silently
            # disable the staleness guard for this entity (Codex re-review finding, Foundation
            # phase) -- skip instead of writing unguarded.
            logger.warning(
                "Skipping chunk backfill for entity %s: content_hash not yet populated "
                "(run init_db() to migrate legacy rows)",
                eid,
            )
            skipped_missing_hash += 1
            continue

        if not content or not content.strip():
            # Precheck (H2 fix): write_entity_chunk_embeddings() returns bare 0 for BOTH this case
            # and a stale-guard skip -- disambiguate here, before calling it, rather than changing
            # that function's own return contract (which has another caller, store_memory's
            # fire-and-forget path, this fix deliberately doesn't need to touch).
            skipped_empty_or_unchunkable += 1
            continue

        try:
            count = write_entity_chunk_embeddings(
                eid, content, db_path, expected_content_hash=content_hash
            )
        except Exception:
            # Per-row isolation (H2 fix): logger.exception (not plain .error) captures the
            # traceback; continuing to the next row is what actually closes the sweep-escape gap
            # -- one bad row no longer silently aborts everything after it.
            logger.exception("Chunk-embedding backfill failed for entity %s", eid)
            failed += 1
            continue

        if count > 0:
            written += 1
        else:
            # The empty-content precheck above already ruled out that explanation for a 0 return
            # here -- the remaining explanation is write_entity_chunk_embeddings' own stale-write
            # guard (the entity was edited/archived since this sweep read its content_hash).
            skipped_stale_guard += 1

    logger.info(
        "backfill_chunk_embeddings complete: selected=%d written=%d skipped_missing_hash=%d "
        "skipped_empty_or_unchunkable=%d skipped_stale_guard=%d failed=%d",
        selected,
        written,
        skipped_missing_hash,
        skipped_empty_or_unchunkable,
        skipped_stale_guard,
        failed,
    )
    return written
