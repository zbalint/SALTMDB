import os
from datetime import datetime, UTC
from saltmdb.config import LIBRARIAN_LOCK_STALE_MINUTES
from saltmdb.db.connection import write_transaction_retrying

def acquire_librarian_lock(conn) -> bool:
    """Attempts to acquire the atomic leader lock for the librarian process.
    Guarantees only one Librarian instance runs concurrently. Expires locks older than
    LIBRARIAN_LOCK_STALE_MINUTES (config.py).
    """
    pid = os.getpid()
    now = datetime.now(UTC).isoformat()
    def _write(c):
        cursor = c.execute(f"""
            UPDATE _system_locks
            SET locked_at = ?, locked_by_pid = ?
            WHERE task_name = 'librarian_consolidation'
              AND (locked_at IS NULL OR datetime(locked_at) < datetime('now', '-{LIBRARIAN_LOCK_STALE_MINUTES} minutes'))
        """, (now, pid))
        return cursor.rowcount == 1
    return write_transaction_retrying(conn, _write)

def release_librarian_lock(conn):
    """Releases the librarian leader lock (only if still owned by this process) and records the execution timestamp."""
    pid = os.getpid()
    now = datetime.now(UTC).isoformat()
    def _write(c):
        c.execute("""
            UPDATE _system_locks
            SET locked_at = NULL, locked_by_pid = NULL, last_run_at = ?
            WHERE task_name = 'librarian_consolidation' AND locked_by_pid = ?
        """, (now, pid))
    write_transaction_retrying(conn, _write)
