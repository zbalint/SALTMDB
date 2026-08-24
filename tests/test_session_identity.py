import unittest
from unittest.mock import patch

from saltmdb.mcp.identity import SESSION_IDENTITY, IdentityRebindRejected, _SessionIdentity
from saltmdb.mcp import tools as mcp_tools


class TestSessionIdentity(unittest.TestCase):
    """§4.5/§4.6: per-adapter-process owner binding. Uses a fresh _SessionIdentity instance per
    test (never the process-wide SESSION_IDENTITY singleton) so tests can't leak binding state
    into each other."""

    def setUp(self):
        self.identity = _SessionIdentity()

    def test_first_bind_sets_owner(self):
        self.identity.bind("claude")
        self.assertEqual(self.identity.owner_id, "claude")

    def test_repeated_identical_bind_is_noop(self):
        self.identity.bind("claude")
        self.identity.bind("claude")  # must not raise
        self.assertEqual(self.identity.owner_id, "claude")

    def test_falsy_bind_is_noop(self):
        self.identity.bind(None)
        self.identity.bind("")
        self.assertIsNone(self.identity.owner_id)

    def test_rebind_to_different_owner_raises(self):
        self.identity.bind("claude")
        with self.assertRaises(IdentityRebindRejected) as ctx:
            self.identity.bind("antigravity")
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
        self.identity.bind("claude")
        self.identity.reset()
        self.assertIsNone(self.identity.owner_id)
        self.assertTrue(self.identity.agent_session_id)
        self.assertNotEqual(original, self.identity.agent_session_id)


class TestRpcBackendIdentityWiring(unittest.TestCase):
    """RpcBackend.call binds from and injects into kwargs (§4.5), bind/inject only -- no
    hard-fail on a missing owner_id yet (deferred to Phase 2)."""

    def setUp(self):
        SESSION_IDENTITY.reset()

    def tearDown(self):
        SESSION_IDENTITY.reset()

    def test_supplied_owner_id_binds_and_is_forwarded_unchanged(self):
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("store_memory", {"owner_id": "claude", "title": "T"})
        self.assertEqual(SESSION_IDENTITY.owner_id, "claude")
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertEqual(forwarded_kwargs["owner_id"], "claude")

    def test_missing_owner_id_is_injected_from_bound_identity(self):
        SESSION_IDENTITY.bind("claude")
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("store_memory", {"owner_id": None, "title": "T"})
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertEqual(forwarded_kwargs["owner_id"], "claude")

    def test_no_owner_id_key_at_all_is_left_untouched(self):
        # Tools without an owner_id concept (merge_tags, search_tags, get_events, ...) never
        # carry the key -- injection must not fabricate one.
        SESSION_IDENTITY.bind("claude")
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("merge_tags", {"keep_tag": "#docs", "tags_to_merge": ["#doc"]})
        forwarded_kwargs = mock_call.call_args[0][2]
        self.assertNotIn("owner_id", forwarded_kwargs)

    def test_rebind_attempt_raises_and_surfaces(self):
        SESSION_IDENTITY.bind("claude")
        backend = mcp_tools.RpcBackend()
        with self.assertRaises(IdentityRebindRejected):
            backend.call("store_memory", {"owner_id": "antigravity", "title": "T"})

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

    def test_non_session_stamped_tool_does_not_receive_agent_session_id(self):
        backend = mcp_tools.RpcBackend()
        with patch("saltmdb.daemon.client.call", return_value="ok") as mock_call:
            backend.call("merge_tags", {})
        self.assertNotIn("agent_session_id", mock_call.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
