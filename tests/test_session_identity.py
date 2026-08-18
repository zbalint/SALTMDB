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

    def test_host_session_id_first_value_wins_no_error_on_mismatch(self):
        self.identity.bind_host_session_id("session-1")
        self.identity.bind_host_session_id("session-2")  # advisory: never raises
        self.assertEqual(self.identity.host_session_id, "session-1")

    def test_host_session_id_falsy_is_noop(self):
        self.identity.bind_host_session_id(None)
        self.assertIsNone(self.identity.host_session_id)

    def test_reset_clears_both_fields(self):
        self.identity.bind("claude")
        self.identity.bind_host_session_id("session-1")
        self.identity.reset()
        self.assertIsNone(self.identity.owner_id)
        self.assertIsNone(self.identity.host_session_id)


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
        # Tools without an owner_id concept (merge_tags, get_canonical_tags, ...) never carry
        # the key -- injection must not fabricate one.
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


if __name__ == "__main__":
    unittest.main()
