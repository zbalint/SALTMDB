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


class _CountingRpcServer:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.methods: list[str] = []
        self.hello_seen = threading.Event()
        self.tool_seen = threading.Event()
        self._stop = threading.Event()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (OSError, TimeoutError):
                continue
            self._connections.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                request = protocol.recv_frame(conn)
                method = request.get("method")
                self.methods.append(method)
                if method == "hello":
                    self.hello_seen.set()
                    response = protocol.build_ok_response(
                        "x", {"caller_agent_session_capability": "server-capability"}
                    )
                elif method == "tool_call":
                    self.tool_seen.set()
                    response = protocol.build_ok_response("x", "ok")
                else:
                    response = protocol.build_ok_response("x", {})
                protocol.send_frame(conn, response)
                if method in {"tool_call", "goodbye"}:
                    return
        except (OSError, protocol.FrameError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        for conn in self._connections:
            try:
                conn.close()
            except OSError:
                pass


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
        session._agent_id = None
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
            [request["params"]["caller_agent_session_capability"] for request in sent_requests],
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
            [request["params"]["caller_agent_session_capability"] for request in sent_requests],
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

    def test_win32_spawn_goes_through_intermediary_launcher(self):
        """Since alpha.97 (SALTMDB memory 61fc01d0's ProcMon-confirmed taskkill /T finding),
        win32 no longer spawns the daemon directly from _spawn_daemon_subprocess -- it spawns a
        short-lived intermediary launcher (`--spawn-detached`) that exits immediately after
        spawning the real daemon, so the daemon isn't a live descendant of the CLI session's
        root PID by the time any later tree-kill walks it."""
        with (
            patch.object(client.sys, "platform", "win32"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_subprocess(self.db_path)
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        self.assertIn("--spawn-detached", args[0])
        self.assertIn(self.db_path, args[0])
        # Same DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP |
        # CREATE_NO_WINDOW combo as the daemon spawn itself -- the intermediary needs the same
        # console/job isolation to survive long enough to spawn its own child.
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x01000000 | 0x00000200 | 0x08000000)

    def test_win32_intermediary_spawn_falls_back_to_direct_on_oserror(self):
        """If even the intermediary fails to spawn (e.g. job disallows breakaway outright), fall
        back to the old direct-spawn path rather than leaving no daemon at all."""
        with (
            patch.object(client.sys, "platform", "win32"),
            patch.object(client.subprocess, "Popen", side_effect=OSError("boom")) as mock_popen,
            patch.object(client, "_spawn_daemon_process") as mock_direct_spawn,
        ):
            client._spawn_daemon_subprocess(self.db_path)
        mock_popen.assert_called_once()
        mock_direct_spawn.assert_called_once_with(self.db_path)

    def test_win32_direct_spawn_includes_create_breakaway_from_job(self):
        with (
            patch.object(client.sys, "platform", "win32"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_process(self.db_path)
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        # DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP |
        # CREATE_NO_WINDOW -- DETACHED_PROCESS (not CREATE_NEW_PROCESS_GROUP alone, alpha.94's
        # disproven attempt) is what actually stops console CTRL_CLOSE_EVENT delivery, per
        # Microsoft's docs: that signal goes to every process attached to the console regardless
        # of process group. CREATE_NO_WINDOW is restored defense-in-depth (Win11/Copilot visible-
        # terminal-window bug, 2026-08-27).
        self.assertEqual(kwargs["creationflags"], 0x00000008 | 0x01000000 | 0x00000200 | 0x08000000)

    def test_win32_direct_spawn_falls_back_without_breakaway_on_oserror(self):
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
            client._spawn_daemon_process(self.db_path)
        # DETACHED_PROCESS (0x00000008), CREATE_NEW_PROCESS_GROUP (0x00000200), and
        # CREATE_NO_WINDOW (0x08000000) are unrelated to job-breakaway policy, so they survive
        # the OSError fallback retry; only CREATE_BREAKAWAY_FROM_JOB is dropped.
        self.assertEqual(
            seen_creationflags,
            [
                0x00000008 | 0x01000000 | 0x00000200 | 0x08000000,
                0x00000008 | 0x00000200 | 0x08000000,
            ],
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
        self.assertIsInstance(float(kwargs["env"]["SALTMDB_DAEMON_SPAWNED_AT"]), float)

    def test_persistent_posix_spawn_disables_idle_timer(self):
        import sys

        with (
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_process(self.db_path, persistent=True)
        args, kwargs = mock_popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "saltmdb.daemon.server", "--foreground"])
        self.assertEqual(kwargs["env"]["SALTMDB_DB_PATH"], self.db_path)
        self.assertTrue(kwargs["start_new_session"])

    def test_default_posix_spawn_keeps_idle_timer(self):
        import sys

        with (
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_process(self.db_path)
        self.assertEqual(
            mock_popen.call_args.args[0], [sys.executable, "-m", "saltmdb.daemon.server"]
        )

    def test_posix_spawn_defaults_malloc_arena_max_without_parent_override(self):
        with (
            patch.dict(os.environ),
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            os.environ.pop("MALLOC_ARENA_MAX", None)
            self.assertNotIn("MALLOC_ARENA_MAX", os.environ)
            client._spawn_daemon_process(self.db_path)
            _, kwargs = mock_popen.call_args
            self.assertEqual(kwargs["env"]["MALLOC_ARENA_MAX"], "2")
            self.assertNotIn("MALLOC_ARENA_MAX", os.environ)

    def test_posix_spawn_preserves_parent_malloc_arena_max(self):
        with (
            patch.dict(os.environ, {"MALLOC_ARENA_MAX": "4"}),
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen") as mock_popen,
        ):
            client._spawn_daemon_process(self.db_path)
            _, kwargs = mock_popen.call_args
            self.assertEqual(kwargs["env"]["MALLOC_ARENA_MAX"], "4")
            self.assertEqual(os.environ["MALLOC_ARENA_MAX"], "4")

    def test_posix_spawn_oserror_propagates_not_silently_retried(self):
        with (
            patch.object(client.sys, "platform", "linux"),
            patch.object(client.subprocess, "Popen", side_effect=OSError("boom")),
        ):
            with self.assertRaises(OSError):
                client._spawn_daemon_subprocess(self.db_path)


class TestIntermediaryMain(unittest.TestCase):
    """`python -m saltmdb.daemon.client --spawn-detached <db_path>` -- the win32 intermediary
    launcher's entry point (see _spawn_daemon_via_intermediary)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_spawns_daemon_and_returns(self):
        with patch.object(client, "_spawn_daemon_process") as mock_spawn:
            client._intermediary_main(["client.py", "--spawn-detached", self.db_path])
        mock_spawn.assert_called_once_with(self.db_path)

    def test_missing_flag_exits_with_usage(self):
        with self.assertRaises(SystemExit):
            client._intermediary_main(["client.py", self.db_path])

    def test_missing_db_path_exits_with_usage(self):
        with self.assertRaises(SystemExit):
            client._intermediary_main(["client.py", "--spawn-detached"])


@unittest.skipIf(client.sys.platform == "win32", "Cross-process stamps require fcntl")
class TestSpawnStamp(unittest.TestCase):
    db_path: str = ""
    stamp_path: str = ""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = os.path.join(temp_dir.name, "test.db")
        directory_patch = patch.object(
            client.discovery, "_discovery_dir", return_value=temp_dir.name
        )
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        clock_patch = patch.object(client.time, "time", return_value=1000.0)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        last_spawn_patch = patch.object(client, "_last_spawn_at", None)
        last_spawn_patch.start()
        self.addCleanup(last_spawn_patch.stop)
        self.stamp_path = client.discovery.spawn_stamp_path(
            client.discovery.daemon_key(self.db_path)
        )

    def test_missing_stamp_claims_slot_and_records_time(self):
        self.assertTrue(client._claim_spawn_slot(self.db_path))
        with open(self.stamp_path, encoding="utf-8") as stamp:
            self.assertEqual(float(stamp.read()), 1000.0)

    def test_fresh_stamp_refuses_second_claim(self):
        self.assertTrue(client._claim_spawn_slot(self.db_path))
        self.assertFalse(client._claim_spawn_slot(self.db_path))

    def test_stale_stamp_claims_slot_and_rewrites_time(self):
        with open(self.stamp_path, "w", encoding="utf-8") as stamp:
            stamp.write("900.0")
        self.assertTrue(client._claim_spawn_slot(self.db_path))
        with open(self.stamp_path, encoding="utf-8") as stamp:
            self.assertEqual(float(stamp.read()), 1000.0)
        self.assertFalse(client._claim_spawn_slot(self.db_path))

    def test_future_stamp_does_not_prevent_spawn(self):
        with open(self.stamp_path, "w", encoding="utf-8") as stamp:
            stamp.write("1100.0")
        self.assertTrue(client._claim_spawn_slot(self.db_path))
        self.assertFalse(client._claim_spawn_slot(self.db_path))

    def test_garbage_stamp_does_not_prevent_spawn(self):
        with open(self.stamp_path, "w", encoding="utf-8") as stamp:
            stamp.write("invalid timestamp")
        self.assertTrue(client._claim_spawn_slot(self.db_path))
        self.assertFalse(client._claim_spawn_slot(self.db_path))

    def test_unwritable_stamp_fails_open_with_warning(self):
        with (
            patch.object(client.os, "open", side_effect=OSError("stamp unavailable")),
            self.assertLogs(client.logger.name, level="WARNING") as logs,
        ):
            self.assertTrue(client._claim_spawn_slot(self.db_path))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("stamp unavailable", logs.output[0])

    def test_missing_fcntl_fails_open_with_warning(self):
        with (
            patch.dict("sys.modules", {"fcntl": None}),
            self.assertLogs(client.logger.name, level="WARNING") as logs,
        ):
            self.assertTrue(client._claim_spawn_slot(self.db_path))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("fcntl", logs.output[0])

    def test_refused_claim_does_not_spawn_or_delay_next_attempt(self):
        with (
            patch.object(client, "_claim_spawn_slot", side_effect=[False, True]),
            patch.object(client.time, "monotonic", side_effect=[100.0, 101.0]),
            patch.object(client, "_spawn_daemon_subprocess") as spawn,
        ):
            self.assertFalse(client._spawn_if_due(self.db_path))
            self.assertIsNone(client._last_spawn_at)
            spawn.assert_not_called()
            self.assertTrue(client._spawn_if_due(self.db_path))
        spawn.assert_called_once_with(self.db_path)

    def test_allowed_claim_spawns_and_retains_process_throttle(self):
        with (
            patch.object(client, "_claim_spawn_slot", return_value=True),
            patch.object(client.time, "monotonic", return_value=100.0),
            patch.object(client, "_spawn_daemon_subprocess") as spawn,
        ):
            self.assertTrue(client._spawn_if_due(self.db_path))
            self.assertFalse(client._spawn_if_due(self.db_path))
        spawn.assert_called_once_with(self.db_path)


class _StartupClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestColdStartWait(unittest.TestCase):
    def setUp(self):
        self.clock = _StartupClock()
        self.db_path = "/tmp/cold-start-test.db"
        self.info = {"db_path": self.db_path, "service_port": 1, "auth_token": "tok"}
        clock_patch = patch.object(client, "time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        spawn_patch = patch.object(client, "_spawn_daemon_subprocess")
        self.spawn = spawn_patch.start()
        self.addCleanup(spawn_patch.stop)
        last_spawn_patch = patch.object(client, "_last_spawn_at", None, create=True)
        last_spawn_patch.start()
        self.addCleanup(last_spawn_patch.stop)
        claim_patch = patch.object(client, "_claim_spawn_slot", return_value=True)
        claim_patch.start()
        self.addCleanup(claim_patch.stop)
        classify_patch = patch.object(client, "_classify_startup_failure", return_value="no owner")
        classify_patch.start()
        self.addCleanup(classify_patch.stop)

    def test_existing_initializing_owner_can_take_twenty_seconds(self):
        with (
            patch.object(client, "probe_owner", return_value="initializing", create=True),
            patch.object(
                client,
                "reachable_daemon_info",
                side_effect=lambda _: self.info if self.clock.now >= 20 else None,
                create=True,
            ),
            patch.object(client.discovery, "read", return_value=None),
        ):
            self.assertEqual(client.ensure_daemon_running(self.db_path), self.info)
        self.assertEqual(self.clock.now, 20.0)
        self.spawn.assert_not_called()

    def test_new_owner_needs_only_one_spawn_while_initializing(self):
        with (
            patch.object(
                client,
                "probe_owner",
                side_effect=lambda _: None if self.clock.now == 0 else "initializing",
                create=True,
            ),
            patch.object(
                client,
                "reachable_daemon_info",
                side_effect=lambda _: self.info if self.clock.now >= 20 else None,
                create=True,
            ),
            patch.object(client.discovery, "read", return_value=None),
        ):
            self.assertEqual(client.ensure_daemon_running(self.db_path), self.info)
        self.spawn.assert_called_once_with(self.db_path)

    def test_missing_owner_exhausts_legacy_window_without_spawn_storm(self):
        with (
            patch.object(client, "probe_owner", return_value=None, create=True),
            patch.object(client, "reachable_daemon_info", return_value=None, create=True),
            patch.object(client.discovery, "read", return_value=None),
        ):
            with self.assertRaises(client.DaemonStartupError) as caught:
                client.ensure_daemon_running(self.db_path)
        self.assertEqual(type(caught.exception), client.DaemonStartupError)
        self.assertGreaterEqual(self.clock.now, 10.0)
        self.assertLessEqual(self.clock.now, 14.0)
        self.spawn.assert_called_once_with(self.db_path)

    def test_alive_owner_expires_at_twenty_five_seconds(self):
        with (
            patch.object(client, "probe_owner", return_value="initializing", create=True),
            patch.object(client, "reachable_daemon_info", return_value=None, create=True),
        ):
            with self.assertRaisesRegex(client.DaemonStartingError, "initializing after 25"):
                client.ensure_daemon_running(self.db_path)
        self.assertEqual(self.clock.now, 25.0)
        self.spawn.assert_not_called()

    def test_transient_probe_holes_and_ready_state_keep_owner_alive(self):
        for answers in (
            ["initializing", None] * 20,
            ["initializing", None, "initializing"] + ["initializing"] * 20,
            ["ready"] * 20,
        ):
            with self.subTest(answers=answers[:3]):
                self.clock.now = 0
                with (
                    patch.object(client, "probe_owner", side_effect=answers, create=True),
                    patch.object(
                        client,
                        "reachable_daemon_info",
                        side_effect=lambda _: self.info if self.clock.now >= 10 else None,
                        create=True,
                    ),
                ):
                    self.assertEqual(client.ensure_daemon_running(self.db_path), self.info)
                self.assertEqual(self.clock.now, 10.0)
        self.spawn.assert_not_called()

    def test_owner_probes_are_throttled(self):
        with (
            patch.object(client, "probe_owner", return_value="initializing", create=True) as probe,
            patch.object(
                client,
                "reachable_daemon_info",
                side_effect=lambda _: self.info if self.clock.now >= 10 else None,
                create=True,
            ),
        ):
            self.assertEqual(client.ensure_daemon_running(self.db_path), self.info)
        self.assertLessEqual(probe.call_count, 11)

    def test_reachable_and_probe_helpers_validate_identity_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "real.db")
            link = os.path.join(temp_dir, "link.db")
            os.symlink(target, link)
            canonical = os.path.realpath(link)
            info = {"db_path": canonical, "service_port": 1, "auth_token": "tok"}

            with (
                patch.object(client.discovery, "read", return_value=info),
                patch.object(client, "_authenticated_ping_ok", return_value=True),
            ):
                self.assertEqual(client.reachable_daemon_info(link), info)
            with patch.object(client.discovery, "read", return_value=None):
                self.assertIsNone(client.reachable_daemon_info(link))
            with (
                patch.object(
                    client.discovery, "read", return_value={**info, "db_path": "/other.db"}
                ),
                patch.object(client, "_authenticated_ping_ok", return_value=True),
            ):
                self.assertIsNone(client.reachable_daemon_info(link))
            with (
                patch.object(client.discovery, "read", return_value=info),
                patch.object(client, "_authenticated_ping_ok", return_value=False),
            ):
                self.assertIsNone(client.reachable_daemon_info(link))

            with patch.object(
                client, "_identify_probe", return_value={"db_path": canonical, "state": "ready"}
            ):
                self.assertEqual(client.probe_owner(link), "ready")
            for response in (None, {}, {"db_path": "/other.db", "state": "ready"}, "ready"):
                with patch.object(client, "_identify_probe", return_value=response):
                    self.assertIsNone(client.probe_owner(link))

    def test_nested_and_sequential_waits_share_one_budget(self):
        with (
            patch.object(client, "probe_owner", return_value="initializing", create=True),
            patch.object(
                client,
                "reachable_daemon_info",
                side_effect=lambda _: self.info if self.clock.now == 20 else None,
                create=True,
            ),
        ):
            with client.startup_budget():
                self.assertEqual(client.ensure_daemon_running(self.db_path), self.info)
                with client.startup_budget(100):
                    self.clock.sleep(0.25)
                    with self.assertRaises(client.DaemonStartingError):
                        client.ensure_daemon_running(self.db_path)
            self.assertEqual(self.clock.now, 25.0)
            with client.startup_budget():
                with self.assertRaises(client.DaemonStartingError):
                    client.ensure_daemon_running(self.db_path)
            self.assertEqual(self.clock.now, 50.0)

    def test_spawn_throttle_allows_next_spawn_at_fifteen_seconds(self):
        for now in (0, 5, 14):
            self.clock.now = now
            client._spawn_if_due(self.db_path)
        self.spawn.assert_called_once_with(self.db_path)
        self.clock.now = 15
        self.assertTrue(client._spawn_if_due(self.db_path))
        self.assertEqual(self.spawn.call_count, 2)

    def test_begin_lazy_start_adopts_session_and_skips_spawn_for_owner(self):
        session = MagicMock()
        session.db_path = self.db_path
        thread = MagicMock()
        with (
            patch.object(client, "probe_owner", return_value="initializing"),
            patch.object(client, "_spawn_if_due") as spawn_if_due,
            patch.object(client.threading, "Thread", return_value=thread) as thread_cls,
        ):
            client.begin_lazy_start(session)
        self.assertIs(client.get_current_session(), session)
        spawn_if_due.assert_not_called()
        thread_cls.assert_called_once()
        thread.start.assert_called_once()
        client._current_session = None

    def test_lazy_thread_retries_pre_probe_startup_then_hellos(self):
        session = MagicMock()
        session.db_path = self.db_path
        ensure_calls = []

        def fake_ensure(_db_path, *, cap_s):
            ensure_calls.append(cap_s)
            if len(ensure_calls) == 1:
                self.clock.now = 20
                raise client.DaemonStartupError("no owner during imports")
            self.clock.now = 60
            return self.info

        thread = MagicMock()
        with (
            patch.object(client, "probe_owner", return_value=None),
            patch.object(client, "_spawn_if_due"),
            patch.object(client, "ensure_daemon_running", side_effect=fake_ensure),
            patch.object(client.threading, "Thread", return_value=thread) as thread_cls,
            self.assertLogs(client.logger.name, level="WARNING") as logs,
        ):
            client.begin_lazy_start(session)
            thread_cls.call_args.kwargs["target"]()
        self.assertEqual(len(ensure_calls), 2)
        session.open.assert_called_once()
        self.assertEqual(sum("still pending" in message for message in logs.output), 1)
        client._current_session = None

    def test_lazy_thread_stops_at_cap_when_daemon_never_appears(self):
        session = MagicMock()
        session.db_path = self.db_path
        ensure_calls = []

        def fake_ensure(_db_path, *, cap_s):
            ensure_calls.append(cap_s)
            self.clock.now += 20
            raise client.DaemonStartupError("still importing")

        thread = MagicMock()
        with (
            patch.object(client, "probe_owner", return_value=None),
            patch.object(client, "_spawn_if_due"),
            patch.object(client, "ensure_daemon_running", side_effect=fake_ensure),
            patch.object(client.threading, "Thread", return_value=thread) as thread_cls,
            self.assertLogs(client.logger.name, level="WARNING") as logs,
        ):
            client.begin_lazy_start(session)
            thread_cls.call_args.kwargs["target"]()
        self.assertGreaterEqual(self.clock.now, 120)
        self.assertEqual(session.open.call_count, 0)
        self.assertEqual(sum("still pending" in message for message in logs.output), 1)
        self.assertTrue(any("exceeded 120 seconds" in message for message in logs.output))
        client._current_session = None

    def test_lazy_adoption_keeps_first_tool_call_attribution(self):
        session = client.SessionConnection.__new__(client.SessionConnection)
        session.db_path = self.db_path
        session._sock = None
        session._auth_token = None
        session._agent_session_id = "logical-session"
        session._session_capability = "lazy-capability"
        session._state_lock = threading.RLock()
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        sent_requests = []
        with (
            patch.object(client, "probe_owner", return_value="initializing"),
            patch.object(client, "_spawn_if_due"),
            patch.object(client.threading, "Thread", return_value=MagicMock()),
            patch.object(session, "ensure_fresh"),
            patch.object(client, "ensure_daemon_running", return_value=self.info),
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "send_frame",
                side_effect=lambda _sock, request: sent_requests.append(request),
            ),
            patch.object(
                client.protocol, "recv_frame", return_value=protocol.build_ok_response("x", "ok")
            ),
        ):
            client.begin_lazy_start(session)
            result = client.call(
                self.db_path,
                "search_tags",
                {},
                caller_agent_session_id="logical-session",
            )
        self.assertEqual(result, "ok")
        self.assertEqual(sent_requests[0]["params"]["caller_agent_session_id"], "logical-session")
        self.assertEqual(
            sent_requests[0]["params"]["caller_agent_session_capability"], "lazy-capability"
        )
        client._current_session = None

    def test_background_thread_hellos_without_a_tool_call(self):
        server = _CountingRpcServer()
        info = {**self.info, "service_port": server.port}
        session = client.SessionConnection(
            self.db_path,
            session_id="background-session",
            cwd="/tmp",
            agent_id="test-agent",
        )
        try:
            with (
                patch.object(client, "probe_owner", return_value="initializing"),
                patch.object(client, "_spawn_if_due"),
                patch.object(client, "ensure_daemon_running", return_value=info),
            ):
                client.begin_lazy_start(session)
                self.assertTrue(server.hello_seen.wait(2))
            self.assertEqual(server.methods, ["hello"])
            self.assertFalse(server.tool_seen.is_set())
        finally:
            session.close(send_goodbye=False)
            client._current_session = None
            server.close()

    def test_racing_tool_call_does_not_send_a_second_hello(self):
        server = _CountingRpcServer()
        info = {**self.info, "service_port": server.port}
        session = client.SessionConnection(
            self.db_path,
            session_id="racing-session",
            cwd="/tmp",
            agent_id="test-agent",
        )
        worker_started = threading.Event()
        worker_allowed = threading.Event()
        worker_done = threading.Event()
        main_thread = threading.current_thread()

        def fake_ensure(_db_path, *, cap_s=None):
            if threading.current_thread() is not main_thread:
                worker_started.set()
                self.assertTrue(worker_allowed.wait(2))
                worker_done.set()
            return info

        try:
            with (
                patch.object(client, "probe_owner", return_value="initializing"),
                patch.object(client, "_spawn_if_due"),
                patch.object(client, "ensure_daemon_running", side_effect=fake_ensure),
                patch.object(client.discovery, "read", return_value=info),
            ):
                client.begin_lazy_start(session)
                self.assertTrue(worker_started.wait(2))
                result = client.call(
                    self.db_path,
                    "search_tags",
                    {},
                    caller_agent_session_id="racing-session",
                )
                self.assertEqual(result, "ok")
                self.assertTrue(server.tool_seen.wait(2))
                worker_allowed.set()
                self.assertTrue(worker_done.wait(2))
            self.assertEqual(server.methods.count("hello"), 1)
            self.assertEqual(server.methods.count("tool_call"), 1)
        finally:
            worker_allowed.set()
            session.close(send_goodbye=False)
            client._current_session = None
            server.close()

    def test_begin_lazy_start_returns_while_probe_is_blocked(self):
        session = MagicMock()
        session.db_path = self.db_path
        probe_started = threading.Event()
        probe_release = threading.Event()
        hello_done = threading.Event()

        def blocked_probe(_db_path):
            probe_started.set()
            probe_release.wait(2)
            return "initializing"

        session.open.side_effect = lambda: hello_done.set()
        started_at = time.monotonic()
        with (
            patch.object(client, "probe_owner", side_effect=blocked_probe),
            patch.object(client, "_spawn_if_due"),
            patch.object(client, "ensure_daemon_running", return_value=self.info),
        ):
            client.begin_lazy_start(session)
            self.assertTrue(probe_started.wait(1))
            self.assertLess(time.monotonic() - started_at, 0.2)
            probe_release.set()
            self.assertTrue(hello_done.wait(1))
        client._current_session = None


class TestCallWithoutDaemonSpawn(unittest.TestCase):
    def test_refused_connection_raises_daemon_not_running_without_spawning(self):
        with (
            patch.object(
                client,
                "reachable_daemon_info",
                return_value={"service_port": 1, "auth_token": "token"},
            ),
            patch.object(client, "ensure_daemon_running") as ensure,
            patch.object(client.socket, "create_connection", side_effect=ConnectionRefusedError()),
        ):
            with self.assertRaises(client.DaemonNotRunning):
                client.call("/tmp/saltmdb-test.db", "search_tags", {}, spawn=False)
        ensure.assert_not_called()

    def test_shutting_down_response_raises_daemon_not_running_without_retrying(self):
        fake_sock = MagicMock()
        fake_sock.__enter__.return_value = fake_sock
        with (
            patch.object(
                client,
                "reachable_daemon_info",
                return_value={"service_port": 1, "auth_token": "token"},
            ),
            patch.object(client, "ensure_daemon_running") as ensure,
            patch.object(client.socket, "create_connection", return_value=fake_sock),
            patch.object(
                client.protocol,
                "recv_frame",
                return_value=protocol.build_error_response(
                    "request", protocol.DAEMON_SHUTTING_DOWN, "draining"
                ),
            ),
        ):
            with self.assertRaises(client.DaemonNotRunning):
                client.call("/tmp/saltmdb-test.db", "search_tags", {}, spawn=False)
        ensure.assert_not_called()

    def test_spawn_false_does_not_refresh_an_existing_session(self):
        session = MagicMock()
        session._state_lock = threading.RLock()
        with (
            patch.object(
                client,
                "reachable_daemon_info",
                return_value={"service_port": 1, "auth_token": "token"},
            ),
            patch.object(client, "ensure_daemon_running") as ensure,
            patch.object(client.socket, "create_connection", side_effect=ConnectionRefusedError()),
        ):
            with self.assertRaises(client.DaemonNotRunning):
                client.call_method(
                    "/tmp/saltmdb-test.db",
                    "search_tags",
                    {},
                    spawn=False,
                    _session=session,
                )
        session.ensure_fresh.assert_not_called()
        ensure.assert_not_called()

    def test_spawn_false_auth_errors_do_not_recurse_or_reconnect(self):
        for code in (protocol.AUTH_FAILED, protocol.CALLER_SESSION_INVALID):
            with self.subTest(code=code):
                fake_sock = MagicMock()
                fake_sock.__enter__.return_value = fake_sock
                session = MagicMock()
                session._state_lock = threading.RLock()
                response = protocol.build_error_response("request", code, "rejected")
                with (
                    patch.object(
                        client,
                        "reachable_daemon_info",
                        return_value={"service_port": 1, "auth_token": "token"},
                    ),
                    patch.object(client, "ensure_daemon_running") as ensure,
                    patch.object(client.socket, "create_connection", return_value=fake_sock),
                    patch.object(client.protocol, "recv_frame", return_value=response),
                    patch.object(client, "call_method", wraps=client.call_method) as wrapped,
                ):
                    with self.assertRaises(client.DaemonRpcError) as raised:
                        client.call_method(
                            "/tmp/saltmdb-test.db",
                            "search_tags",
                            {},
                            spawn=False,
                            _session=session,
                        )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(wrapped.call_count, 1)
                session.force_reconnect.assert_not_called()
                ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
