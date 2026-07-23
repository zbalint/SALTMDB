import os
from datetime import datetime, UTC

def acquire_librarian_lock(conn) -> bool:
    """Attempts to acquire the atomic leader lock for the librarian process.
    Guarantees only one Librarian instance runs concurrently. Expire locks older than 10 mins.
    """
    pid = os.getpid()
    now = datetime.now(UTC).isoformat()
    with conn:
        cursor = conn.execute("""
            UPDATE _system_locks 
            SET locked_at = ?, locked_by_pid = ? 
            WHERE task_name = 'librarian_consolidation' 
              AND (locked_at IS NULL OR datetime(locked_at) < datetime('now', '-10 minutes'))
        """, (now, pid))
        return cursor.rowcount == 1

def release_librarian_lock(conn):
    """Releases the librarian leader lock and records the execution timestamp."""
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute("""
            UPDATE _system_locks 
            SET locked_at = NULL, locked_by_pid = NULL, last_run_at = ? 
            WHERE task_name = 'librarian_consolidation'
        """, (now,))
