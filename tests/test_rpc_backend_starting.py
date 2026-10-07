import unittest
from unittest.mock import patch

from saltmdb.daemon import client as daemon_client
from saltmdb.mcp import tools


class TestRpcBackendStarting(unittest.TestCase):
    backend: tools.RpcBackend

    def setUp(self):
        self.backend = tools.RpcBackend()

    def test_daemon_starting_returns_retryable_result(self):
        with patch.object(
            daemon_client,
            "call",
            side_effect=daemon_client.DaemonStartingError(
                "the SALTMDB daemon is still starting (initializing after 25 s)"
            ),
        ):
            result = self.backend.call("search_tags", {})
        self.assertEqual(
            result,
            {
                "status": "DAEMON_STARTING",
                "tool": "search_tags",
                "advice": (
                    "The SALTMDB daemon is still starting after a restart. Wait a few seconds "
                    "and repeat the call; nothing was executed."
                ),
            },
        )

    def test_daemon_starting_from_read_retry_returns_same_result(self):
        with patch.object(
            daemon_client,
            "call",
            side_effect=[
                daemon_client.DaemonRpcError("MID_CALL_FAILURE", "connection dropped"),
                daemon_client.DaemonStartingError("still initializing"),
            ],
        ) as call:
            result = self.backend.call("search_tags", {})
        self.assertEqual(result["status"], "DAEMON_STARTING")
        self.assertEqual(result["tool"], "search_tags")
        self.assertEqual(call.call_count, 2)

    def test_plain_startup_error_still_propagates(self):
        error = daemon_client.DaemonStartupError("startup failed")
        with patch.object(daemon_client, "call", side_effect=error):
            with self.assertRaises(daemon_client.DaemonStartupError):
                self.backend.call("search_tags", {})

    def test_mid_call_read_retry_uses_the_original_twenty_five_second_budget(self):
        class _Clock:
            def __init__(self):
                self.now = 0.0

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                self.now += seconds

        clock = _Clock()
        calls = []

        def fake_call(*_args, **_kwargs):
            calls.append(clock.now)
            if len(calls) == 1:
                clock.now = 20.0
                raise daemon_client.DaemonRpcError("MID_CALL_FAILURE", "read dropped")
            daemon_client.ensure_daemon_running("/tmp/rpc-starting-test.db")

        with (
            patch.object(daemon_client, "time", clock),
            patch.object(daemon_client, "probe_owner", return_value="initializing"),
            patch.object(daemon_client, "reachable_daemon_info", return_value=None),
            patch.object(daemon_client, "_spawn_if_due"),
            patch.object(daemon_client, "call", side_effect=fake_call) as rpc_call,
        ):
            result = self.backend.call("search_tags", {})

        self.assertEqual(result["status"], "DAEMON_STARTING")
        self.assertEqual(result["tool"], "search_tags")
        self.assertEqual(rpc_call.call_count, 2)
        self.assertEqual(clock.now, 25.0)


if __name__ == "__main__":
    unittest.main()
