import logging
import random
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from saltmdb.config import (
    RETRY_BASE_DELAY_S,
    RETRY_JITTER_S,
    RETRY_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

# Set only by DbWriteCoordinator's writer thread.  It is deliberately a
# ContextVar rather than a process global so request/model threads can never
# accidentally borrow the SQLite object.
_coordinator_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
    "saltmdb_coordinator_connection", default=None
)
_daemon_boundary_enabled = False


def enable_daemon_connection_boundary() -> None:
    """After bootstrap, non-writer daemon threads may open read-only DB handles only."""
    global _daemon_boundary_enabled
    _daemon_boundary_enabled = True


def is_coordinator_connection(conn: sqlite3.Connection) -> bool:
    return conn is _coordinator_connection.get()


def _enter_coordinator_connection(conn: sqlite3.Connection):
    return _coordinator_connection.set(conn)


def _leave_coordinator_connection(token) -> None:
    _coordinator_connection.reset(token)

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


def open_read_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-only SQLite connection.

    Daemon request handlers must use this for normal reads.  ``query_only`` is
    intentionally set in addition to URI ``mode=ro``: the former makes an
    accidental write fail loudly even if a caller later attaches another DB.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    conn.execute("PRAGMA query_only=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def open_writer_connection(db_path: str) -> sqlite3.Connection:
    """Open a writer connection for schema bootstrap or DbWriteCoordinator only."""
    conn = sqlite3.connect(db_path, timeout=20.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-8000;")
    conn.execute("PRAGMA mmap_size=67108864;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # Explicit default (1000 pages / ~4MB); do not lower until WAL-page-count logging
    # (see close_connection) shows growth is actually a problem
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


# Compatibility name retained for direct-mode tests and legacy callers.  New
# daemon code must use ``open_read_connection`` or DbWriteCoordinator instead.
def get_connection(db_path: str) -> sqlite3.Connection:
    active = _coordinator_connection.get()
    if active is not None:
        return active
    if _daemon_boundary_enabled:
        return open_read_connection(db_path)
    # Legacy direct-mode compatibility only.  The daemon never calls this
    # outside coordinator scope; its historical cross-thread setting remains
    # for unit tests and standalone migration helpers.
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=20.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-8000;")
    conn.execute("PRAGMA mmap_size=67108864;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA foreign_keys=ON;")
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


def write_transaction_retrying(conn: sqlite3.Connection, fn):
    """Executes a callable inside write_transaction with retry/backoff on lock contention.

    Only retries sqlite3.OperationalError whose message contains
    "database is locked" or "database is busy" (case-insensitive) -- any other OperationalError
    (or any other exception) propagates immediately. Retries up to
    RETRY_MAX_ATTEMPTS times beyond the first attempt, with exponential
    backoff plus jitter between attempts, so total attempts = 1 + RETRY_MAX_ATTEMPTS.

    Requires conn to have isolation_level=None so BEGIN IMMEDIATE front-loads
    lock acquisition. Retries the entire transaction (fresh BEGIN IMMEDIATE +
    re-invoking fn(c)) on lock contention.

    Note: fn may be invoked more than once if lock contention occurs, so it must
    be safe to re-run (its writes roll back automatically via write_transaction on
    failure, so this is safe as long as fn has no side effects beyond writing to conn).

    Returns:
        The return value of fn(c).
    """
    # Service helpers retain their historical transaction wrapper.  When a
    # daemon dispatch already owns the outer coordinator transaction, nesting
    # must reuse it rather than issuing a second BEGIN IMMEDIATE.
    if conn is _coordinator_connection.get():
        return fn(conn)

    attempt = 0
    while True:
        try:
            with write_transaction(conn) as c:
                return fn(c)
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            if (
                not any(m in err_msg for m in ("database is locked", "database is busy"))
                or attempt >= RETRY_MAX_ATTEMPTS
            ):
                raise
            delay = RETRY_BASE_DELAY_S * (2**attempt) + random.uniform(0, RETRY_JITTER_S)
            logger.warning(
                "Write transaction hit lock contention on attempt %d/%d; retrying in %.3fs",
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
    """Close a connection without hidden maintenance writes.

    Checkpointing and optimisation are explicit coordinator jobs; doing either
    while closing an otherwise read-only connection violates the daemon's
    single-writer boundary.
    """
    if conn is _coordinator_connection.get():
        return
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
