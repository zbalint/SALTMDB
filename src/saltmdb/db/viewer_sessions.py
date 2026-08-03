import os
import sys
import subprocess
import logging
from datetime import datetime, UTC
from saltmdb.db.connection import write_transaction_retrying

logger = logging.getLogger(__name__)

kernel32 = None
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # WaitForSingleObject is preferred over GetExitCodeProcess: it avoids the
    # STILL_ACTIVE (259) ambiguity where a process that legitimately exits with
    # code 259 would be falsely reported as alive.
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def _pid_alive(pid: int) -> bool:
    """Checks whether a process with the given PID is currently alive on Linux or Windows."""
    if sys.platform == "win32":
        import ctypes
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    # WaitForSingleObject(handle, 0): non-blocking wait.
                    # WAIT_TIMEOUT (0x102) means the process is still running.
                    # WAIT_OBJECT_0 (0x0) means the process has exited.
                    result = kernel32.WaitForSingleObject(handle, 0)
                    return result == 0x00000102  # WAIT_TIMEOUT => still alive
                finally:
                    kernel32.CloseHandle(handle)
            else:
                error = ctypes.get_last_error()
                # ERROR_ACCESS_DENIED (5): process exists but we lack permission.
                if error == 5:
                    return True
                return False
        except Exception as e:
            logger.warning("Failed to check PID liveness on Windows for PID %s: %s", pid, e, exc_info=True)
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
    if pid is None:
        pid = os.getpid()
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
    if pid is None:
        pid = os.getpid()

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
