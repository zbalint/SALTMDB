import logging
import random
import sqlite3
import time
from contextlib import contextmanager

from saltmdb.config import (
    RETRY_BASE_DELAY_S,
    RETRY_JITTER_S,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

# Module-level ephemeral in-memory connection (singleton)
EPHEMERAL_CONN = sqlite3.connect(":memory:", check_same_thread=False, timeout=10.0)

def init_ephemeral_db():
    with EPHEMERAL_CONN:
        EPHEMERAL_CONN.execute("""
        CREATE TABLE IF NOT EXISTS ephemeral_memories (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

# Initialize the ephemeral database immediately upon module load
init_ephemeral_db()

def get_connection(db_path: str) -> sqlite3.Connection:
    """Create a new per-request connection configured with optimized PRAGMAs."""
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=20.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA mmap_size=268435456;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Explicit default (1000 pages / ~4MB); do not lower until WAL-page-count logging
    # (see close_connection) shows growth is actually a problem
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """Context manager for an explicit write transaction using BEGIN IMMEDIATE.

    Requires the connection to have been opened with isolation_level=None
    (see get_connection) so sqlite3 does not implicitly manage its own
    deferred BEGIN. This matters because PRAGMA busy_timeout only protects a
    lock that is requested up front: a deferred (default) transaction starts
    with no lock at all and only tries to acquire the write lock later, at
    the moment of its first write, when it silently upgrades from a read
    lock. That upgrade attempt is NOT covered by busy_timeout in the way
    callers usually expect, so under write-write contention it can raise
    "database is locked" essentially immediately rather than waiting out the
    configured timeout. Issuing BEGIN IMMEDIATE acquires the write lock
    up front, so busy_timeout actually applies to the wait for that lock.
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


@contextmanager
def write_transaction_retrying(conn: sqlite3.Connection):
    """Context manager wrapping write_transaction with retry/backoff on lock contention.

    Only retries sqlite3.OperationalError whose message contains
    "database is locked" (case-insensitive) -- any other OperationalError
    (or any other exception) propagates immediately. Retries up to
    RETRY_MAX_ATTEMPTS times beyond the first attempt, with exponential
    backoff plus jitter between attempts, so total attempts = 1 + RETRY_MAX_ATTEMPTS.

    The retry loop lives OUTSIDE the `with write_transaction(conn):` block so
    that each retry re-issues a fresh BEGIN IMMEDIATE (re-executing the
    caller's body from scratch) rather than resuming a stale transaction.
    """
    attempt = 0
    while True:
        try:
            with write_transaction(conn) as c:
                yield c
                return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() or attempt >= RETRY_MAX_ATTEMPTS:
                raise
            delay = RETRY_BASE_DELAY_S * (2 ** attempt) + random.uniform(0, RETRY_JITTER_S)
            logger.warning(
                "Write transaction hit 'database is locked' on attempt %d/%d; retrying in %.3fs",
                attempt + 1,
                RETRY_MAX_ATTEMPTS + 1,
                delay,
            )
            time.sleep(delay)
            attempt += 1


def _log_wal_checkpoint_state(conn: sqlite3.Connection) -> None:
    """Best-effort debug log of WAL checkpoint state; never raises."""
    cursor = conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
    row = cursor.fetchone()
    if row:
        busy, log_pages, checkpointed_pages = row
        logger.debug(
            "WAL checkpoint state on close: busy=%d, wal_pages=%d, checkpointed_pages=%d",
            busy,
            log_pages,
            checkpointed_pages,
        )


def close_connection(conn: sqlite3.Connection) -> None:
    """Close a connection, running best-effort cleanup PRAGMAs first.

    Never raises -- this is meant to be safe to call from cleanup/finally
    paths (including while another exception is already being handled),
    where raising here would mask the original error.
    """
    try:
        conn.execute("PRAGMA optimize;")
    except Exception as e:
        logger.debug("PRAGMA optimize failed during close_connection: %s", e)
    try:
        _log_wal_checkpoint_state(conn)
    except Exception as e:
        logger.debug("WAL checkpoint state logging failed during close_connection: %s", e)
    conn.close()


@contextmanager
def managed_connection(db_connection=None, db_path=None):
    """Context manager that acquires a connection if not provided, and closes it on exit."""
    from saltmdb.config import get_db_path as _get_db_path
    should_close = db_connection is None
    conn = db_connection if db_connection is not None else get_connection(db_path or _get_db_path())
    try:
        yield conn
    finally:
        if should_close:
            close_connection(conn)
