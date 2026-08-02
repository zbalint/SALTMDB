import os
import sys
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from saltmdb import config
from saltmdb.db.connection import get_connection
from saltmdb.db.viewer_sessions import register_session, unregister_session, count_live_sessions
from saltmdb.mcp.server import server_lifespan


class TestMCPServerLifespan(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("saltmdb.mcp.server.start_viewer")
    @patch("saltmdb.mcp.server.stop_viewer")
    @patch("saltmdb.mcp.server.config.is_viewer_enabled", return_value=True)
    async def test_single_session_lifespan_calls_stop_viewer(
        self, mock_enabled, mock_stop_viewer, mock_start_viewer
    ):
        mock_start_viewer.return_value = "Started"
        with patch("saltmdb.mcp.server.get_db_path", return_value=self.db_path):
            async with server_lifespan(MagicMock()):
                pass

        viewer_port = config.get_viewer_port()
        mock_stop_viewer.assert_called_once_with(port=viewer_port)

    @patch("saltmdb.mcp.server.start_viewer")
    @patch("saltmdb.mcp.server.stop_viewer")
    @patch("saltmdb.mcp.server.config.is_viewer_enabled", return_value=True)
    async def test_overlapping_sessions_prevents_premature_viewer_stop(
        self, mock_enabled, mock_stop_viewer, mock_start_viewer
    ):
        mock_start_viewer.return_value = "Started"
        viewer_port = config.get_viewer_port()

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            with patch("saltmdb.mcp.server.get_db_path", return_value=self.db_path):
                async with server_lifespan(MagicMock()):
                    # Simulate second concurrent session joining
                    conn = get_connection(self.db_path)
                    try:
                        register_session(conn, viewer_port, pid=proc.pid)
                    finally:
                        conn.close()

            # Session 1 exited, but session 2 is still active -> stop_viewer must NOT be called
            mock_stop_viewer.assert_not_called()

            # Now session 2 exits and unregisters
            conn = get_connection(self.db_path)
            try:
                unregister_session(conn, viewer_port, pid=proc.pid)
                remaining = count_live_sessions(conn, viewer_port)
                if remaining == 0:
                    mock_stop_viewer(port=viewer_port)
            finally:
                conn.close()

            mock_stop_viewer.assert_called_once_with(port=viewer_port)
        finally:
            proc.terminate()
            proc.wait()


if __name__ == "__main__":
    unittest.main()
