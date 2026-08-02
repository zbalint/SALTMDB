import os
import sys
import subprocess
from datetime import datetime, UTC
from saltmdb.db.connection import write_transaction_retrying


def _pid_alive(pid: int) -> bool:
    """Checks whether a process with the given PID is currently alive on Linux or Windows."""
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout:
                pid_str = str(pid)
                for line in res.stdout.splitlines():
                    if pid_str in line.split():
                        return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def register_session(conn, port: int, pid: int | None = None) -> None:
    """Registers a session PID in the _viewer_sessions table after sweeping dead PIDs."""
    pid = pid or os.getpid()
    started_at = datetime.now(UTC).isoformat()

    count_live_sessions(conn, port)

    def _write(c):
        c.execute(
            """
            INSERT OR IGNORE INTO _viewer_sessions (port, session_pid, started_at)
            VALUES (?, ?, ?)
            """,
            (port, pid, started_at),
        )

    write_transaction_retrying(conn, _write)


def unregister_session(conn, port: int, pid: int | None = None) -> None:
    """Unregisters a session PID from the _viewer_sessions table."""
    pid = pid or os.getpid()

    def _write(c):
        c.execute(
            """
            DELETE FROM _viewer_sessions
            WHERE port = ? AND session_pid = ?
            """,
            (port, pid),
        )

    write_transaction_retrying(conn, _write)


def count_live_sessions(conn, port: int) -> int:
    """Counts live sessions for a given viewer port and cleans up dead session rows."""

    def _write(c):
        cursor = c.execute(
            "SELECT session_pid FROM _viewer_sessions WHERE port = ?",
            (port,),
        )
        rows = cursor.fetchall()
        live_count = 0
        for (pid,) in rows:
            if _pid_alive(pid):
                live_count += 1
            else:
                c.execute(
                    "DELETE FROM _viewer_sessions WHERE port = ? AND session_pid = ?",
                    (port, pid),
                )
        return live_count

    return write_transaction_retrying(conn, _write)
