import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.viewer.routes import SALTMDBHandler
from saltmdb.viewer.server import SALTMDBTCPServer, main


class TestSALTMDBTCPServer(unittest.TestCase):
    """Track B (scratch/plans/track_b_daemon_detailed.md §10): start_viewer/stop_viewer/
    _run_liveness_watchdog are retired -- the daemon constructs SALTMDBTCPServer directly and
    runs it as an in-process thread. Only the class's own error-suppression behavior and the
    now-thin main() RPC status client remain to test here."""

    def test_handle_error_suppresses_client_disconnect_tracebacks(self):
        server = SALTMDBTCPServer.__new__(SALTMDBTCPServer)  # bypass __init__, no real bind
        with patch(
            "sys.exc_info", return_value=(ConnectionResetError, ConnectionResetError(), None)
        ):
            with patch.object(SALTMDBTCPServer.__bases__[1], "handle_error") as mock_super:
                server.handle_error(MagicMock(), ("127.0.0.1", 12345))
                mock_super.assert_not_called()

    def test_handle_error_reraises_other_exceptions_via_super(self):
        server = SALTMDBTCPServer.__new__(SALTMDBTCPServer)
        with patch("sys.exc_info", return_value=(ValueError, ValueError(), None)):
            with patch.object(SALTMDBTCPServer.__bases__[1], "handle_error") as mock_super:
                server.handle_error(MagicMock(), ("127.0.0.1", 12345))
                mock_super.assert_called_once()


class TestViewerServerSlowClients(unittest.TestCase):
    """A stalled or excess client must not be able to hold Viewer threads open indefinitely."""

    def _serve(self):
        server = SALTMDBTCPServer(("127.0.0.1", 0), SALTMDBHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    @staticmethod
    def _closed_by_server(sock, wait_s):
        sock.settimeout(wait_s)
        try:
            return sock.recv(1) == b""
        except socket.timeout:
            return False

    def test_the_default_request_timeout_is_bounded(self):
        # The stall test below patches the timeout to keep it fast; this pins the real default.
        self.assertIsNotNone(SALTMDBHandler.timeout)
        self.assertTrue(0 < SALTMDBHandler.timeout <= 60)

    def test_a_client_that_stalls_mid_request_is_disconnected(self):
        with patch.object(SALTMDBHandler, "timeout", 0.3):
            port = self._serve()
            with socket.create_connection(("127.0.0.1", port)) as sock:
                sock.sendall(b"GET /api/sta")  # never finishes the request line
                self.assertTrue(self._closed_by_server(sock, 3))

    def test_connections_beyond_the_cap_are_shed_immediately(self):
        with (
            patch.object(SALTMDBHandler, "timeout", 5),
            patch.object(SALTMDBTCPServer, "max_connections", 1),
        ):
            port = self._serve()
            with socket.create_connection(("127.0.0.1", port)) as holder:
                holder.sendall(b"GET /api/sta")  # occupies the only slot
                with socket.create_connection(("127.0.0.1", port)) as excess:
                    self.assertTrue(self._closed_by_server(excess, 2))
                # the slot holder was not disturbed by the shed connection
                self.assertFalse(self._closed_by_server(holder, 0.3))


class TestViewerServerMain(unittest.TestCase):
    def setUp(self):
        # Never resolve against the live default DB path, per the standing dev rule -- even
        # though discovery.read is mocked below, resolve_canonical_db_path() still runs for real.
        self._prev_db_path = __import__("os").environ.get("SALTMDB_DB_PATH")
        __import__("os").environ["SALTMDB_DB_PATH"] = "/tmp/saltmdb_test_viewer_server.db"

    def tearDown(self):
        import os

        if self._prev_db_path is None:
            os.environ.pop("SALTMDB_DB_PATH", None)
        else:
            os.environ["SALTMDB_DB_PATH"] = self._prev_db_path

    @patch("saltmdb.daemon.discovery.read", return_value=None)
    def test_main_exits_when_no_daemon_running(self, mock_read):
        with patch("sys.argv", ["saltmdb-viewer"]):
            with self.assertRaises(SystemExit) as cm:
                main()
        self.assertEqual(cm.exception.code, 1)

    @patch("saltmdb.daemon.client.call_method", return_value={"enabled": True, "port": 8080})
    @patch(
        "saltmdb.daemon.discovery.read",
        return_value={
            "db_path": "/tmp/saltmdb_test_viewer_server.db",
            "service_port": 1,
            "auth_token": "t",
        },
    )
    def test_main_reports_viewer_url_when_daemon_reachable(self, mock_read, mock_call):
        # Track B round-1 Codex finding: status must come from the daemon's own viewer_status RPC
        # response (daemon-authoritative), never from the client-local config module.
        with patch("sys.argv", ["saltmdb-viewer"]):
            main()  # should not raise/exit
        mock_call.assert_called_once_with("/tmp/saltmdb_test_viewer_server.db", "viewer_status", {})

    @patch("saltmdb.daemon.client.call_method", return_value={"enabled": False, "port": None})
    @patch(
        "saltmdb.daemon.discovery.read",
        return_value={
            "db_path": "/tmp/saltmdb_test_viewer_server.db",
            "service_port": 1,
            "auth_token": "t",
        },
    )
    def test_main_reports_viewer_disabled_when_daemon_says_so(self, mock_read, mock_call):
        with patch("sys.argv", ["saltmdb-viewer"]):
            main()  # should not raise/exit -- disabled is a normal, non-error report

    @patch("saltmdb.daemon.client.call_method", side_effect=RuntimeError("connection refused"))
    @patch(
        "saltmdb.daemon.discovery.read",
        return_value={
            "db_path": "/tmp/saltmdb_test_viewer_server.db",
            "service_port": 1,
            "auth_token": "t",
        },
    )
    def test_main_exits_when_daemon_unreachable(self, mock_read, mock_call):
        with patch("sys.argv", ["saltmdb-viewer"]):
            with self.assertRaises(SystemExit) as cm:
                main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
