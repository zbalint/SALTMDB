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
        canned_match_output = (
            "Image Name                     PID Session Name        Session#    Mem Usage\n"
            "========================= ======== ================ =========== ============\n"
            "python.exe                     123 Console                    1     15,000 K\n"
        )
        canned_substring_output = (
            "Image Name                     PID Session Name        Session#    Mem Usage\n"
            "========================= ======== ================ =========== ============\n"
            "python.exe                    1234 Console                    1     15,000 K\n"
        )

        with patch("sys.platform", "win32"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=canned_match_output)
                self.assertTrue(_pid_alive(target_pid))
                mock_run.assert_called_once_with(
                    ["tasklist", "/FI", f"PID eq {target_pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=canned_substring_output)
                self.assertFalse(_pid_alive(target_pid))


if __name__ == "__main__":
    unittest.main()
