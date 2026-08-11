"""Track B (scratch/plans/track_b_daemon_detailed.md): daemon/client.py's SessionConnection.open().

Regression coverage for a Codex round-1 finding: open() previously read the daemon's hello
response but never checked its "ok" flag, so a well-formed AUTH_FAILED/DAEMON_SHUTTING_DOWN error
response was silently treated as a successfully-opened session -- and any failure past the initial
connect (rejected hello, a framing error) leaked the socket open() itself created, since there was
no cleanup path.
"""

import socket
import threading
import time
import unittest
from unittest.mock import patch

from saltmdb.daemon import client, protocol


class _FakeDaemonServer:
    """Single-shot TCP responder: accepts one connection, reads one length-prefixed frame, sends
    back a fixed canned response. Enough to exercise SessionConnection.open()'s hello handling
    without a real daemon process."""

    def __init__(self, response: dict):
        self._response = response
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.accepted_conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        self.accepted_conn = conn
        try:
            protocol.recv_frame(conn)
            protocol.send_frame(conn, self._response)
        except (OSError, protocol.FrameError):
            pass

    def wait_for_accepted_conn(self, timeout: float = 5.0) -> socket.socket:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.accepted_conn is not None:
                return self.accepted_conn
            time.sleep(0.02)
        raise AssertionError("fake daemon server never accepted a connection")

    def close(self) -> None:
        self._sock.close()
        if self.accepted_conn is not None:
            try:
                self.accepted_conn.close()
            except OSError:
                pass


class _AbruptCloseServer:
    """Accepts one connection, reads the incoming hello frame, then closes without responding at
    all -- forces the client's recv_frame() to fail with FrameError, exercising open()'s
    framing-failure cleanup path (distinct from a well-formed rejected-hello response)."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        try:
            protocol.recv_frame(conn)
        except (OSError, protocol.FrameError):
            pass
        conn.close()

    def close(self) -> None:
        self._sock.close()


class TestSessionConnectionOpen(unittest.TestCase):
    def setUp(self):
        client._current_session = None

    def tearDown(self):
        client._current_session = None

    def _blank_session(self) -> client.SessionConnection:
        session = client.SessionConnection.__new__(client.SessionConnection)
        session.db_path = "/tmp/saltmdb_test_daemon_client.db"
        session._sock = None
        session._auth_token = None
        return session

    def test_open_raises_and_closes_socket_on_rejected_hello(self):
        server = _FakeDaemonServer(
            protocol.build_error_response("x", protocol.AUTH_FAILED, "stale token")
        )
        try:
            session = self._blank_session()
            with patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": server.port, "auth_token": "tok"},
            ):
                with self.assertRaises(client.DaemonRpcError):
                    session.open()
            self.assertIsNone(session._sock)
            self.assertIsNone(client._current_session)

            # Confirms the client-side socket was actually closed on the failure path (not just
            # that open() raised) -- the server observes EOF only if the peer closed its end.
            accepted = server.wait_for_accepted_conn()
            accepted.settimeout(3.0)
            data = accepted.recv(1)
            self.assertEqual(data, b"", "client socket was not closed on the rejected-hello path")
        finally:
            server.close()

    def test_open_raises_and_closes_socket_on_framing_failure(self):
        """Codex round-2 finding: the rejected-hello test above only proved cleanup for a
        well-formed error response; a framing-level failure (peer closes without responding) is a
        distinct code path through the same try/except and needs its own coverage. Verified via a
        captured-socket technique (fileno() == -1 once closed) since there's no live peer left to
        observe EOF from, unlike the rejected-hello case above."""
        server = _AbruptCloseServer()
        created_sockets: list[socket.socket] = []
        real_create_connection = socket.create_connection

        def _capturing_create_connection(*args, **kwargs):
            sock = real_create_connection(*args, **kwargs)
            created_sockets.append(sock)
            return sock

        try:
            session = self._blank_session()
            with (
                patch.object(
                    client,
                    "ensure_daemon_running",
                    return_value={"service_port": server.port, "auth_token": "tok"},
                ),
                patch.object(
                    client.socket, "create_connection", side_effect=_capturing_create_connection
                ),
            ):
                with self.assertRaises((protocol.FrameError, OSError)):
                    session.open()
            self.assertIsNone(session._sock)
            self.assertIsNone(client._current_session)
            self.assertEqual(len(created_sockets), 1)
            self.assertEqual(
                created_sockets[0].fileno(),
                -1,
                "client socket was not closed on the framing-failure path",
            )
        finally:
            server.close()

    def test_open_succeeds_and_sets_session_state_on_ok_hello(self):
        server = _FakeDaemonServer(protocol.build_ok_response("x", {"status": "ok"}))
        try:
            session = self._blank_session()
            with patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": server.port, "auth_token": "tok"},
            ):
                session.open()
            self.assertIsNotNone(session._sock)
            self.assertEqual(session._auth_token, "tok")
            self.assertIs(client._current_session, session)
            session.close()
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
