"""Track B (scratch/plans/track_b_daemon_detailed.md): daemon/client.py's SessionConnection.open().

Regression coverage for a Codex round-1 finding: open() previously read the daemon's hello
response but never checked its "ok" flag, so a well-formed AUTH_FAILED/DAEMON_SHUTTING_DOWN error
response was silently treated as a successfully-opened session -- and any failure past the initial
connect (rejected hello, a framing error) leaked the socket open() itself created, since there was
no cleanup path.
"""

import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from saltmdb.daemon import client, protocol


class _FakeDaemonServer:
    """Single-shot TCP responder: accepts one connection, reads one length-prefixed frame, sends
    back a fixed canned response. Enough to exercise SessionConnection.open()'s hello handling
    without a real daemon process."""

    def __init__(self, response: dict):
        self._response = response
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self.accepted_conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        for _ in range(2):
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            if self.accepted_conn is None:
                self.accepted_conn = conn
            try:
                protocol.recv_frame(conn)
                protocol.send_frame(conn, self._response)
            except (OSError, protocol.FrameError):
                pass
            finally:
                if conn is not self.accepted_conn:
                    try:
                        conn.close()
                    except OSError:
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
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        for _ in range(2):
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
        session._agent_session_id = None
        session._cwd = None
        session._owner_id = None
        return session

    def test_restart_reconnect_does_not_send_definitive_goodbye(self):
        session = self._blank_session()
        session._auth_token = "old-token"
        session._sock = MagicMock()
        with (
            patch.object(
                client.discovery,
                "read",
                return_value={"auth_token": "new-token"},
            ),
            patch.object(session, "_close_transport_locked") as close,
            patch.object(session, "open"),
        ):
            session.ensure_fresh(session.db_path)
        close.assert_called_once_with()

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
                # The stale-auth response is retryable once; this single-shot fixture has no
                # second daemon accept, so the final connection failure is also valid here.
                with self.assertRaises((client.DaemonRpcError, OSError)):
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
            self.assertEqual(len(created_sockets), 2)
            self.assertTrue(all(sock.fileno() == -1 for sock in created_sockets))
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

    def test_open_retries_stale_auth_once_and_retains_daemon_capability(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        fake_sock = MagicMock()
        responses = [
            protocol.build_error_response("x", protocol.AUTH_FAILED, "stale token"),
            protocol.build_ok_response(
                "x", {"caller_agent_session_capability": "opaque-capability"}
            ),
        ]
        with (
            patch.object(
                client,
                "ensure_daemon_running",
                side_effect=[
                    {"service_port": 1, "auth_token": "old"},
                    {"service_port": 2, "auth_token": "new"},
                ],
            ),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(client.protocol, "send_frame"),
            patch.object(client.protocol, "recv_frame", side_effect=responses),
        ):
            session.open()
        self.assertEqual(session.session_metadata, ("logical-session", "opaque-capability"))
        self.assertEqual(fake_sock.close.call_count, 1)
        session.close(send_goodbye=False)

    def test_open_does_not_retry_final_internal_error(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        fake_sock = MagicMock()
        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ) as ensure_daemon,
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(client.protocol, "send_frame"),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_error_response(
                    "x", protocol.INTERNAL_ERROR, "registration failed"
                ),
            ),
        ):
            with self.assertRaises(client.DaemonRpcError) as ctx:
                session.open()
        self.assertEqual(ctx.exception.code, protocol.INTERNAL_ERROR)
        self.assertEqual(ensure_daemon.call_count, 1)

    def test_call_uses_capability_minted_during_refresh_not_pre_refresh_snapshot(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        session._session_capability = "old-capability"
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []

        def refresh(_db_path):
            session._session_capability = "new-capability"

        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ),
            patch.object(session, "ensure_fresh", side_effect=refresh),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_ok_response("x", "ok"),
            ),
        ):
            client._current_session = session
            try:
                result = client.call(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    caller_agent_session_id="logical-session",
                )
            finally:
                client._current_session = None

        self.assertEqual(result, "ok")
        self.assertEqual(sent_requests[0]["params"]["caller_agent_session_id"], "logical-session")
        self.assertEqual(
            sent_requests[0]["params"]["caller_agent_session_capability"], "new-capability"
        )

    def test_failed_refresh_blocks_rpc_and_next_call_can_retry_logical_session(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        session._session_capability = "old-capability"
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []
        refresh_results = iter(
            [
                client.DaemonRpcError(protocol.INTERNAL_ERROR, "refresh failed"),
                None,
            ]
        )

        def refresh(_db_path):
            result = next(refresh_results)
            if result is not None:
                raise result
            session._session_capability = "retry-capability"

        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ),
            patch.object(session, "ensure_fresh", side_effect=refresh),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_ok_response("x", "ok"),
            ),
        ):
            client._current_session = session
            try:
                with self.assertRaises(client.DaemonRpcError):
                    client.call(
                        "/tmp/saltmdb-test.db",
                        "search_tags",
                        {},
                        caller_agent_session_id="logical-session",
                    )
                self.assertEqual(sent_requests, [])
                result = client.call(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    caller_agent_session_id="logical-session",
                )
            finally:
                client._current_session = None

        self.assertEqual(result, "ok")
        self.assertEqual(
            sent_requests[0]["params"]["caller_agent_session_capability"],
            "retry-capability",
        )
        self.assertEqual(session._agent_session_id, "logical-session")

    def test_explicit_mismatched_caller_id_does_not_borrow_current_capability(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        session._session_capability = "current-capability"
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []
        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ),
            patch.object(session, "ensure_fresh"),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_ok_response("x", "ok"),
            ),
        ):
            client._current_session = session
            try:
                client.call(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    caller_agent_session_id="other-session",
                )
            finally:
                client._current_session = None
        self.assertNotIn("caller_agent_session_capability", sent_requests[0]["params"])

    def test_protocol_retry_resnapshots_capability_after_refresh(self):
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        session._session_capability = "old-capability"
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []
        refreshed_capabilities = iter(("first-capability", "retry-capability"))

        def refresh(_db_path):
            session._session_capability = next(refreshed_capabilities)

        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ),
            patch.object(session, "ensure_fresh", side_effect=refresh),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol,
                "recv_frame",
                side_effect=[
                    protocol.build_error_response("x", protocol.AUTH_FAILED, "stale auth"),
                    protocol.build_ok_response("x", "ok"),
                ],
            ),
        ):
            client._current_session = session
            try:
                result = client.call(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    caller_agent_session_id="logical-session",
                )
            finally:
                client._current_session = None

        self.assertEqual(result, "ok")
        self.assertEqual(
            [
                request["params"]["caller_agent_session_capability"]
                for request in sent_requests
            ],
            ["first-capability", "retry-capability"],
        )

    def test_call_forces_reconnect_and_retries_on_caller_session_invalid(self):
        """Fresh review finding (2026-08-26): CALLER_SESSION_INVALID means the daemon has
        already dropped our capability (e.g. a raw persistent-socket disconnect) while its own
        auth_token never changed, so ensure_fresh()'s cheap token comparison alone can never
        detect it. call_method() must force a real reconnect specifically on this error code
        instead of resending the same stale capability."""
        session = self._blank_session()
        session._agent_session_id = "logical-session"
        session._session_capability = "old-capability"
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []

        def fake_force_reconnect(_db_path):
            session._session_capability = "reconnected-capability"

        with (
            patch.object(
                client,
                "ensure_daemon_running",
                return_value={"service_port": 1, "auth_token": "tok"},
            ),
            patch.object(session, "ensure_fresh"),
            patch.object(
                session, "force_reconnect", side_effect=fake_force_reconnect
            ) as force_reconnect,
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol,
                "recv_frame",
                side_effect=[
                    protocol.build_error_response(
                        "x", protocol.CALLER_SESSION_INVALID, "inactive session"
                    ),
                    protocol.build_ok_response("x", "ok"),
                ],
            ),
        ):
            client._current_session = session
            try:
                result = client.call(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    caller_agent_session_id="logical-session",
                )
            finally:
                client._current_session = None

        self.assertEqual(result, "ok")
        force_reconnect.assert_called_once()
        self.assertEqual(
            [
                request["params"]["caller_agent_session_capability"]
                for request in sent_requests
            ],
            ["old-capability", "reconnected-capability"],
        )

    def test_force_reconnect_closes_transport_and_reopens(self):
        session = self._blank_session()
        session._sock = MagicMock()
        session._auth_token = "old-token"
        calls = []
        with (
            patch.object(
                session,
                "_close_transport_locked",
                side_effect=lambda: calls.append("close_transport"),
            ),
            patch.object(session, "open", side_effect=lambda: calls.append("open")),
        ):
            session.force_reconnect(session.db_path)
        self.assertEqual(calls, ["close_transport", "open"])

    def test_close_reads_and_logs_failed_goodbye_ack(self):
        """Fresh review finding (2026-08-26): close() previously sent goodbye and tore down the
        socket without ever attempting to read the response, so a server-side persistence
        failure was silently indistinguishable from success."""
        session = self._blank_session()
        session._sock = MagicMock()
        session._auth_token = "tok"
        session._agent_session_id = "logical-session"
        with (
            patch.object(client.protocol, "send_frame"),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_error_response(
                    "x", protocol.INTERNAL_ERROR, "failed to close agent session: db busy"
                ),
            ),
            self.assertLogs(client.logger.name, level="WARNING") as logs,
        ):
            session.close()
        self.assertTrue(
            any("logical-session" in message for message in logs.output),
            logs.output,
        )
        self.assertIsNone(session._sock)


class TestSpawnDaemonSubprocessWindowsJobBreakaway(unittest.TestCase):
    """Regression coverage for the Windows job-object hard-kill finding (2026-08-26, reported live
    by a user running the adapter under Windows/Copilot): a daemon spawned without
    CREATE_BREAKAWAY_FROM_JOB stays a member of whatever Job Object its ancestor belongs to (VS
    Code/Copilot's extension host commonly assigns its whole child-process tree to a job with
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), so it gets force-killed the instant that job closes --
    bypassing shutdown_watcher/goodbye/the grace timer entirely, no different from SIGKILL."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_win32_spawn_includes_create_breakaway_from_job(self):
        with (
            patch.object(client.sys, "platform", "win32"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_subprocess(self.db_path)
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        # CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP -- the last one
        # (added alongside this test) isolates the daemon from console CTRL_CLOSE_EVENT delivery,
        # a mechanism separate from and in addition to Job Object breakaway.
        self.assertEqual(kwargs["creationflags"], 0x08000000 | 0x01000000 | 0x00000200)

    def test_win32_spawn_falls_back_without_breakaway_on_oserror(self):
        seen_creationflags = []

        def _fake_popen(_args, **kwargs):
            seen_creationflags.append(kwargs.get("creationflags"))
            if len(seen_creationflags) == 1:
                raise OSError("job object disallows breakaway")
            return MagicMock()

        with (
            patch.object(client.sys, "platform", "win32"),
            patch.object(client.subprocess, "Popen", side_effect=_fake_popen),
        ):
            client._spawn_daemon_subprocess(self.db_path)
        # CREATE_NEW_PROCESS_GROUP (0x00000200) is unrelated to job-breakaway policy, so it
        # survives the OSError fallback retry; only CREATE_BREAKAWAY_FROM_JOB is dropped.
        self.assertEqual(
            seen_creationflags, [0x08000000 | 0x01000000 | 0x00000200, 0x08000000 | 0x00000200]
        )

    def test_posix_spawn_unaffected_still_uses_start_new_session(self):
        with (
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_subprocess(self.db_path)
        _, kwargs = mock_popen.call_args
        self.assertNotIn("creationflags", kwargs)
        self.assertTrue(kwargs["start_new_session"])

    def test_posix_spawn_oserror_propagates_not_silently_retried(self):
        with (
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen", side_effect=OSError("boom")),
        ):
            with self.assertRaises(OSError):
                client._spawn_daemon_subprocess(self.db_path)


if __name__ == "__main__":
    unittest.main()
