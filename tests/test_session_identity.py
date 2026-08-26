import unittest
from unittest.mock import patch

from saltmdb.config import get_owner_id

from saltmdb.mcp.identity import SESSION_IDENTITY, IdentityRebindRejected, _SessionIdentity
from saltmdb.mcp import tools as mcp_tools


class TestSessionIdentity(unittest.TestCase):
    def test_owner_configuration_requires_safe_stable_identifier(self):
        with patch.dict("os.environ", {"SALTMDB_OWNER_ID": "agent_qa"}, clear=False):
            self.assertEqual(get_owner_id(), "agent_qa")
        with patch.dict("os.environ", {"SALTMDB_OWNER_ID": "Agent QA"}, clear=False):
            with self.assertRaises(RuntimeError):
                get_owner_id()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                get_owner_id()
        with patch.dict("os.environ", {"SALTMDB_OWNER_ID": "a" * 65}, clear=True):
            with self.assertRaises(RuntimeError):
                get_owner_id()

    """§4.5/§4.6: startup-only per-adapter-process identity.

    Uses a fresh ``_SessionIdentity`` instance per test (never the process-wide singleton) so
    tests cannot leak configuration state into each other. Owner identity is deliberately not
    bound by a first MCP tool call or accepted from a tool argument.
    """

    def setUp(self):
        self.identity = _SessionIdentity()

    def test_startup_configuration_sets_owner(self):
        self.identity.configure_owner("claude")
        self.assertEqual(self.identity.owner_id, "claude")

    def test_repeated_identical_configuration_is_noop(self):
        self.identity.configure_owner("claude")
        self.identity.configure_owner("claude")  # must not raise
        self.assertEqual(self.identity.owner_id, "claude")

    def test_invalid_startup_configuration_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.identity.configure_owner("")
        self.assertIsNone(self.identity.owner_id)

    def test_reconfiguration_to_different_owner_raises(self):
        self.identity.configure_owner("claude")
        with self.assertRaises(IdentityRebindRejected) as ctx:
            self.identity.configure_owner("antigravity")
        self.assertEqual(ctx.exception.bound, "claude")
        self.assertEqual(ctx.exception.attempted, "antigravity")
        self.assertIn("claude", str(ctx.exception))
        self.assertIn("antigravity", str(ctx.exception))

    def test_eager_mint_is_non_empty(self):
        self.assertTrue(self.identity.agent_session_id)

    def test_separate_instances_get_different_ids(self):
        self.assertNotEqual(self.identity.agent_session_id, _SessionIdentity().agent_session_id)

    def test_reset_re_mints_session_id(self):
        original = self.identity.agent_session_id
        self.identity.configure_owner("claude")
        self.identity.reset()
        self.assertIsNone(self.identity.owner_id)
        self.assertTrue(self.identity.agent_session_id)
        self.assertNotEqual(original, self.identity.agent_session_id)


class TestRpcBackendIdentityWiring(unittest.TestCase):
    """RpcBackend adds session provenance without accepting agent-controlled owner input."""

    def setUp(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")

    def tearDown(self):
        SESSION_IDENTITY.reset()

    def test_backend_does_not_bind_owner_from_dispatch_kwargs(self):
        SESSION_IDENTITY.configure_owner("test_agent")
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("store_memory", {"owner_id": "claude", "title": "T"})
        self.assertEqual(SESSION_IDENTITY.owner_id, "test_agent")
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertEqual(forwarded_kwargs["owner_id"], "test_agent")

    def test_owner_is_injected_by_public_wrapper_before_backend(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("claude")
        capture = unittest.mock.Mock()
        previous = mcp_tools._set_backend_for_test(capture)
        try:
            mcp_tools.search_memory(query_keywords="probe")
        finally:
            mcp_tools._set_backend_for_test(previous)
        self.assertEqual(capture.call.call_args.args[1]["owner_id"], "claude")

    def test_no_owner_id_key_at_all_is_left_untouched(self):
        # Tools without an owner_id concept (merge_tags, search_tags, get_events, ...) never
        # carry the key -- injection must not fabricate one.
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call(
                "merge_tags",
                {"keep_tag": "#docs", "tags_to_merge": ["#doc"], "owner_id": "attacker"},
            )
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertNotIn("owner_id", forwarded_kwargs)

    def test_caller_session_metadata_is_transport_level(self):
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("search_tags", {})
        self.assertNotIn("caller_agent_session_id", mock_call.call_args.args[2])
        self.assertEqual(
            mock_call.call_args.kwargs["caller_agent_session_id"],
            SESSION_IDENTITY.agent_session_id,
        )

    def test_write_tools_always_receive_current_agent_session_id(self):
        backend = mcp_tools.RpcBackend()
        write_tools = (
            "log_event",
            "store_memory",
            "consolidate_memories",
            "revise_memory",
            "supersede_memory",
        )
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            for tool_name in write_tools:
                backend.call(tool_name, {"agent_session_id": "bogus"})
                forwarded = mock_call.call_args[0][2]
                self.assertEqual(forwarded["agent_session_id"], SESSION_IDENTITY.agent_session_id)

    def test_log_event_never_receives_owner_id_injection(self):
        """Regression for the 2026-08-26 live outage (v0.1.0-alpha.87): log_event was
        listed in _OWNER_INJECTED_TOOLS, so RpcBackend injected an `owner_id` kwarg on
        every call. event_service.log_event's signature has no owner_id parameter and no
        **kwargs catch-all, so every real log_event call raised TypeError in production.
        log_event already binds ownership itself via `agent_id` inside its own tools.py
        wrapper before backend.call() runs -- it must never receive a bolted-on owner_id
        key from this layer."""
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call(
                "log_event",
                {
                    "agent_id": "claude",
                    "type": "issue",
                    "content": "x",
                    "error_code": None,
                    "context_id": None,
                },
            )
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertNotIn("owner_id", forwarded_kwargs)

    def test_non_session_stamped_tool_does_not_receive_agent_session_id(self):
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("merge_tags", {})
        self.assertNotIn("agent_session_id", mock_call.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
