import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from saltmdb.daemon import server


_KEY = "s11-test-key"
_DB_PATH = "/tmp/s11-test.db"
_SERVICE_PORT = 41001
_ELECTION_PORT = 41002
_PROBE_PORT = 41003
_START = 2.0
_BOUND = 3.5
_DB_READY = 6.0
_SERVICES_STARTED = 10.25


class _State:
    def __init__(self, inflight: bool):
        self.auth_token = None
        self._inflight = inflight

    def _acquire_inflight(self) -> bool:
        return self._inflight


def _publish_ready_grouped(state: _State) -> None:
    server._publish_ready(
        state,
        _KEY,
        _DB_PATH,
        server._DaemonPorts(_SERVICE_PORT, _ELECTION_PORT, _PROBE_PORT),
        server._StartupTimings(_START, _BOUND, _DB_READY, _SERVICES_STARTED),
    )


class TestPublishReady(unittest.TestCase):
    def test_token_discovery_order_and_startup_logs(self):
        state = _State(True)
        token = "fixed-s11-token"
        events = Mock()
        real_logger = server.logger
        logger = Mock(wraps=real_logger)
        discovery = Mock()
        thread = Mock()
        thread_factory = Mock(return_value=thread)
        events.attach_mock(logger, "logger")
        events.attach_mock(discovery, "discovery")
        events.attach_mock(thread_factory, "Thread")
        events.attach_mock(thread, "recovery_thread")

        def write_discovery(_key, _db_path, _pid, _service_port, _auth_token):
            self.assertEqual(state.auth_token, token)

        discovery.write.side_effect = write_discovery
        server_time = SimpleNamespace(monotonic=Mock(side_effect=[12.0]))
        server_secrets = SimpleNamespace(token_urlsafe=Mock(return_value=token))
        server_discovery = SimpleNamespace(write=discovery.write)
        server_threading = SimpleNamespace(Thread=thread_factory)
        server_sys = SimpleNamespace(platform="linux")
        with (
            patch.object(server, "secrets", server_secrets),
            patch.object(server, "discovery", server_discovery),
            patch.object(server, "time", server_time),
            patch.object(server, "format_process_memory", return_value="rss=fixture"),
            patch.object(server, "threading", server_threading),
            patch.object(server, "sys", server_sys),
            patch.object(server, "logger", logger),
            patch.object(server, "_log_windows_job_diagnostics"),
        ):
            with self.assertLogs(real_logger, level="INFO") as captured:
                _publish_ready_grouped(state)

        server_secrets.token_urlsafe.assert_called_once_with(32)
        discovery.write.assert_called_once_with(
            _KEY,
            _DB_PATH,
            os.getpid(),
            _SERVICE_PORT,
            token,
        )
        self.assertEqual(state.auth_token, token)
        ready_log = call.logger.info(
            "SALTMDB daemon ready: db=%s service_port=%d election_port=%d probe_port=%d",
            _DB_PATH,
            _SERVICE_PORT,
            _ELECTION_PORT,
            _PROBE_PORT,
        )
        phases_log = call.logger.info(
            "Daemon startup phases: election_s=%.3f init_db_s=%.3f recovery_s=%.3f "
            "listeners_s=%.3f total_s=%.3f",
            1.5,
            2.5,
            4.25,
            1.75,
            10.0,
        )
        memory_log = call.logger.info("Process memory (daemon ready): %s", "rss=fixture")
        self.assertIn(ready_log, events.mock_calls)
        self.assertIn(phases_log, events.mock_calls)
        self.assertIn(memory_log, events.mock_calls)
        self.assertTrue(
            any(
                "SALTMDB daemon ready: db=/tmp/s11-test.db service_port=41001 "
                "election_port=41002 probe_port=41003" in line
                for line in captured.output
            )
        )
        self.assertTrue(
            any(
                "Daemon startup phases: election_s=1.500 init_db_s=2.500 recovery_s=4.250 "
                "listeners_s=1.750 total_s=10.000" in line
                for line in captured.output
            )
        )
        discovery_index = events.mock_calls.index(
            call.discovery.write(_KEY, _DB_PATH, os.getpid(), _SERVICE_PORT, token)
        )
        ready_index = events.mock_calls.index(ready_log)
        thread_start_index = events.mock_calls.index(call.recovery_thread.start())
        self.assertLess(discovery_index, ready_index)
        self.assertLess(ready_index, thread_start_index)
        self.assertEqual(server_time.monotonic.call_args_list, [call()])

    def test_recovery_thread_starts_when_inflight(self):
        state = _State(True)
        thread = Mock()
        thread_factory = Mock(return_value=thread)
        server_time = SimpleNamespace(monotonic=Mock(side_effect=[12.0]))
        server_secrets = SimpleNamespace(token_urlsafe=Mock(return_value="token"))
        server_discovery = SimpleNamespace(write=Mock())
        server_threading = SimpleNamespace(Thread=thread_factory)
        server_sys = SimpleNamespace(platform="linux")
        with (
            patch.object(server, "secrets", server_secrets),
            patch.object(server, "discovery", server_discovery),
            patch.object(server, "time", server_time),
            patch.object(server, "format_process_memory", return_value="rss=fixture"),
            patch.object(server, "threading", server_threading),
            patch.object(server, "sys", server_sys),
            patch.object(server, "_log_windows_job_diagnostics"),
        ):
            _publish_ready_grouped(state)

        thread_factory.assert_called_once_with(
            target=server._run_embedding_recovery,
            args=(state,),
            daemon=True,
            name="saltmdb-embedding-recovery",
        )
        thread.start.assert_called_once_with()

    def test_recovery_thread_is_not_started_when_draining(self):
        state = _State(False)
        server_time = SimpleNamespace(monotonic=Mock(side_effect=[12.0]))
        server_secrets = SimpleNamespace(token_urlsafe=Mock(return_value="token"))
        server_discovery = SimpleNamespace(write=Mock())
        server_threading = SimpleNamespace(Thread=Mock())
        server_sys = SimpleNamespace(platform="linux")
        with (
            patch.object(server, "secrets", server_secrets),
            patch.object(server, "discovery", server_discovery),
            patch.object(server, "time", server_time),
            patch.object(server, "format_process_memory", return_value="rss=fixture"),
            patch.object(server, "threading", server_threading),
            patch.object(server, "sys", server_sys),
            patch.object(server, "_log_windows_job_diagnostics"),
        ):
            with self.assertLogs(server.logger, level="DEBUG") as captured:
                _publish_ready_grouped(state)

        server_threading.Thread.assert_not_called()
        self.assertTrue(
            any(
                "Embedding recovery not started: daemon is already draining" in line
                for line in captured.output
            )
        )

    def test_win32_diagnostics_only_runs_on_win32(self):
        for platform, expected_calls in (("linux", 0), ("win32", 1)):
            with self.subTest(platform=platform):
                state = _State(False)
                diagnostics = Mock()
                server_time = SimpleNamespace(monotonic=Mock(side_effect=[12.0]))
                server_secrets = SimpleNamespace(token_urlsafe=Mock(return_value="token"))
                server_discovery = SimpleNamespace(write=Mock())
                server_threading = SimpleNamespace(Thread=Mock())
                server_sys = SimpleNamespace(platform=platform)
                with (
                    patch.object(server, "secrets", server_secrets),
                    patch.object(server, "discovery", server_discovery),
                    patch.object(server, "time", server_time),
                    patch.object(server, "format_process_memory", return_value="rss=fixture"),
                    patch.object(server, "threading", server_threading),
                    patch.object(server, "sys", server_sys),
                    patch.object(server, "_log_windows_job_diagnostics", diagnostics),
                ):
                    _publish_ready_grouped(state)

                self.assertEqual(diagnostics.call_count, expected_calls)
