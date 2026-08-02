import unittest
from unittest.mock import MagicMock, patch
from saltmdb.viewer.server import SALTMDBTCPServer, main, start_viewer, stop_viewer, _run_liveness_watchdog


class TestViewerServer(unittest.TestCase):
    @patch("saltmdb.viewer.server.subprocess.Popen")
    @patch("saltmdb.viewer.server.urllib.request.urlopen")
    def test_start_viewer_already_running(self, mock_urlopen, mock_popen):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        res = start_viewer(port=8080)

        self.assertIn("already running", res)
        self.assertIn("http://localhost:8080", res)
        mock_popen.assert_not_called()

    @patch("saltmdb.viewer.server.subprocess.Popen")
    @patch("saltmdb.viewer.server.time.sleep")
    @patch("saltmdb.viewer.server.stop_viewer")
    @patch("saltmdb.viewer.server.socket.socket")
    @patch("saltmdb.viewer.server.urllib.request.urlopen")
    def test_start_viewer_clears_stale_port(
        self, mock_urlopen, mock_socket_cls, mock_stop_viewer, mock_sleep, mock_popen
    ):
        mock_urlopen.side_effect = Exception("Connection refused")

        mock_sock_instance = MagicMock()
        mock_socket_cls.return_value = mock_sock_instance

        mock_sock_instance.connect.side_effect = [
            None,  # 1st connect in stale loop: port occupied
            OSError("Connection refused"),  # 2nd connect in stale loop: port cleared
            None,  # 3rd connect in post-spawn check: server started
        ]

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        res = start_viewer(port=8080)

        mock_stop_viewer.assert_called_once_with(port=8080)
        self.assertIn("started successfully", res)

    @patch("saltmdb.viewer.server.logger")
    @patch("saltmdb.viewer.server.SALTMDBTCPServer")
    def test_main_bind_oserror_exits(self, mock_tcp_server, mock_logger):
        mock_tcp_server.side_effect = OSError("Address already in use")

        with self.assertRaises(SystemExit) as cm:
            main()

        self.assertEqual(cm.exception.code, 1)
        mock_logger.error.assert_called()
        self.assertIn("Failed to bind viewer", mock_logger.error.call_args[0][0])

    @patch("saltmdb.viewer.server.os.path.exists", return_value=True)
    @patch("saltmdb.viewer.server.get_connection")
    @patch("saltmdb.viewer.server.count_live_sessions", return_value=0)
    def test_liveness_watchdog_shuts_down_when_no_live_sessions(
        self, mock_count, mock_get_conn, mock_exists
    ):
        mock_httpd = MagicMock()
        _run_liveness_watchdog(mock_httpd, port=8080, check_interval=0.001, grace_period=0.001)
        mock_httpd.shutdown.assert_called_once()

    @patch("saltmdb.viewer.server.os.path.exists", return_value=True)
    @patch("saltmdb.viewer.server.get_connection")
    @patch("saltmdb.viewer.server.count_live_sessions", side_effect=[1, 0])
    def test_liveness_watchdog_shuts_down_after_sessions_exit(
        self, mock_count, mock_get_conn, mock_exists
    ):
        mock_httpd = MagicMock()
        _run_liveness_watchdog(mock_httpd, port=8080, check_interval=0.001, grace_period=0.001)
        mock_httpd.shutdown.assert_called_once()
        self.assertEqual(mock_count.call_count, 2)


if __name__ == "__main__":
    unittest.main()
