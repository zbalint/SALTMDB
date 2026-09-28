import os
import sqlite3
import sys
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from saltmdb.db.schema import init_db
from saltmdb.db.viewer_sessions import (
    _pid_alive,
    count_live_sessions,
    register_session,
    unregister_session,
)
from saltmdb.viewer.routes.sessions import _load_sessions


class TestViewerSessions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_register_and_count_live_sessions_current_pid(self):
        port = 8080
        register_session(self.conn, port)
        count = count_live_sessions(self.conn, port)
        self.assertEqual(count, 1)

    def test_two_live_sessions_and_unregister(self):
        port = 8080
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            register_session(self.conn, port, pid=os.getpid())
            register_session(self.conn, port, pid=proc.pid)

            count = count_live_sessions(self.conn, port)
            self.assertEqual(count, 2)

            unregister_session(self.conn, port, pid=proc.pid)
            count = count_live_sessions(self.conn, port)
            self.assertEqual(count, 1)
        finally:
            proc.terminate()
            proc.wait()

    def test_dead_pid_self_healing_cleanup(self):
        port = 8080
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid

        self.assertFalse(_pid_alive(dead_pid))

        register_session(self.conn, port, pid=dead_pid)
        count = count_live_sessions(self.conn, port)
        self.assertEqual(count, 0)

        cursor = self.conn.execute(
            "SELECT session_pid FROM _viewer_sessions WHERE port = ? AND session_pid = ?",
            (port, dead_pid),
        )
        self.assertEqual(len(cursor.fetchall()), 0)

    def test_unregister_unregistered_pid_does_not_raise(self):
        port = 8080
        unregister_session(self.conn, port, pid=999999)

    def test_pid_alive_windows_branch(self):
        target_pid = 123

        with (
            patch("sys.platform", "win32"),
            patch("saltmdb.db.viewer_sessions.kernel32", create=True) as mock_kernel32,
            patch("saltmdb.db.viewer_sessions.wintypes", create=True) as mock_wintypes,
            patch("ctypes.get_last_error", create=True) as mock_get_last_error,
            patch("ctypes.byref", create=True),
        ):
            mock_exit_code = MagicMock()
            mock_wintypes.DWORD.return_value = mock_exit_code

            # Alive: OpenProcess succeeds, GetExitCodeProcess returns STILL_ACTIVE (259)
            mock_kernel32.OpenProcess.return_value = 9999
            mock_kernel32.GetExitCodeProcess.return_value = True
            mock_exit_code.value = 259
            self.assertTrue(_pid_alive(target_pid))
            mock_kernel32.OpenProcess.assert_called_with(0x1000, False, target_pid)
            mock_kernel32.CloseHandle.assert_called_with(9999)

            # Dead (zombie/exited): OpenProcess succeeds, GetExitCodeProcess returns exit code 0
            mock_kernel32.OpenProcess.return_value = 9999
            mock_kernel32.GetExitCodeProcess.return_value = True
            mock_exit_code.value = 0
            self.assertFalse(_pid_alive(target_pid))

            # GetExitCodeProcess API failure: conservatively assume alive
            mock_kernel32.OpenProcess.return_value = 9999
            mock_kernel32.GetExitCodeProcess.return_value = False
            mock_get_last_error.return_value = 6  # ERROR_INVALID_HANDLE
            self.assertTrue(_pid_alive(target_pid))

            # OpenProcess returns NULL, error is not ACCESS_DENIED: dead
            mock_kernel32.OpenProcess.return_value = 0
            mock_get_last_error.return_value = 87  # ERROR_INVALID_PARAMETER
            self.assertFalse(_pid_alive(target_pid))

            # OpenProcess returns NULL, ERROR_ACCESS_DENIED (5): process exists, alive
            mock_kernel32.OpenProcess.return_value = 0
            mock_get_last_error.return_value = 5
            self.assertTrue(_pid_alive(target_pid))

    def test_trace_count_and_lifecycle_defaults_for_trace_session(self):
        self.conn.execute(
            """
            INSERT INTO _agent_sessions
                (session_id, cwd, owner_id, started_at, last_activity_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("trace-session", "/project", "owner-a", "2026-09-01T10:00:00+00:00", None, None),
        )
        self.conn.execute(
            """
            INSERT INTO conversation_traces
                (id, agent_session_id, owner_id, harness, harness_session_id,
                 harness_turn_id, status, user_prompt, user_prompt_hash,
                 created_at, updated_at)
            VALUES (?, ?, ?, 'codex', ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                "trace-session-row",
                "trace-session",
                "owner-a",
                "codex-session",
                "turn-1",
                "prompt",
                "prompt-hash",
                "2026-09-01T10:01:00+00:00",
                "2026-09-01T10:01:00+00:00",
            ),
        )
        self.conn.commit()

        self.conn.row_factory = sqlite3.Row
        rows = _load_sessions(self.conn, set(), True)

        session = next(row for row in rows if row["session_id"] == "trace-session")
        self.assertEqual(session["trace_count"], 1)
        self.assertEqual(session["liveness"], "unknown")


if __name__ == "__main__":
    unittest.main()
