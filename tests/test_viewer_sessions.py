import os
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

        with patch("sys.platform", "win32"), \
             patch("saltmdb.db.viewer_sessions.kernel32", create=True) as mock_kernel32, \
             patch("ctypes.get_last_error", create=True) as mock_get_last_error:

            # Alive: OpenProcess succeeds, WaitForSingleObject returns WAIT_TIMEOUT (0x102)
            mock_kernel32.OpenProcess.return_value = 9999
            mock_kernel32.WaitForSingleObject.return_value = 0x00000102
            self.assertTrue(_pid_alive(target_pid))
            mock_kernel32.OpenProcess.assert_called_with(0x1000, False, target_pid)
            mock_kernel32.WaitForSingleObject.assert_called_with(9999, 0)
            mock_kernel32.CloseHandle.assert_called_with(9999)

            # Dead (zombie or exited): OpenProcess succeeds, WaitForSingleObject returns WAIT_OBJECT_0 (0x0)
            mock_kernel32.OpenProcess.return_value = 9999
            mock_kernel32.WaitForSingleObject.return_value = 0x00000000
            self.assertFalse(_pid_alive(target_pid))

            # Dead: OpenProcess returns NULL, error is not ACCESS_DENIED
            mock_kernel32.OpenProcess.return_value = 0
            mock_get_last_error.return_value = 87  # ERROR_INVALID_PARAMETER
            self.assertFalse(_pid_alive(target_pid))

            # Alive (access denied): OpenProcess returns NULL, error is ACCESS_DENIED (5)
            mock_kernel32.OpenProcess.return_value = 0
            mock_get_last_error.return_value = 5  # ERROR_ACCESS_DENIED
            self.assertTrue(_pid_alive(target_pid))


if __name__ == "__main__":
    unittest.main()
