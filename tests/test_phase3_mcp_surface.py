import inspect
import unittest

from saltmdb.daemon import dispatch, protocol
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class _CaptureBackend:
    def __init__(self):
        self.calls = []

    def call(self, tool_name, kwargs):
        self.calls.append((tool_name, kwargs))
        return {"tool": tool_name, "kwargs": kwargs}


class TestPhase3McpSurface(unittest.TestCase):
    def setUp(self):
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")
        self.previous_backend = tools._set_backend_for_test(_CaptureBackend())

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_owner("test_agent")

    def test_tool_count_and_registration(self):
        # Phase 6 removed dismiss_event (19 -> 18); Phase 7 removed ephemeral_memory and
        # export_corpus_snapshot (18 -> 16, the plan's §2 target). update_memory_metadata was
        # added afterward (16 -> 17, API-ergonomics Gap 1), then inspect_memory (17 -> 18,
        # API-ergonomics Gap 2). See test_mcp_tools.py's test_mcp_tool_count_regression_guard
        # for the authoritative count guard.
        self.assertEqual(len(tools.mcp._tool_manager._tools), 18)
        self.assertIn("get_memory", dispatch.DISPATCH_TABLE)
        self.assertIn("get_lineage", dispatch.DISPATCH_TABLE)
        self.assertIn("get_related_memories", dispatch.DISPATCH_TABLE)
        self.assertNotIn("inspect_graph", dispatch.DISPATCH_TABLE)

    def test_search_schema_has_no_explicit_fetch_controls(self):
        params = inspect.signature(tools.search_memory).parameters
        self.assertNotIn("entity_id", params)
        self.assertNotIn("fetch_full", params)

    def test_graph_tools_have_small_explicit_schemas(self):
        self.assertEqual(list(inspect.signature(tools.get_memory).parameters), ["entity_id"])
        self.assertEqual(
            list(inspect.signature(tools.get_lineage).parameters),
            ["entity_id", "direction", "max_depth"],
        )
        self.assertEqual(
            list(inspect.signature(tools.get_related_memories).parameters),
            ["entity_id", "max_depth", "direction", "include_inspect"],
        )

    def test_graph_tools_forward_normalized_calls(self):
        tools.get_memory(entity_id="memory-id")
        tools.get_lineage(entity_id="memory-id")
        tools.get_related_memories(entity_id="memory-id")
        self.assertEqual(
            [name for name, _kwargs in tools._backend.calls],
            ["get_memory", "get_lineage", "get_related_memories"],
        )
        self.assertEqual(tools._backend.calls[1][1]["direction"], "ancestors")
        self.assertEqual(tools._backend.calls[1][1]["max_depth"], 5)

    def test_protocol_classifies_graph_tools_as_reads(self):
        for name in ("get_memory", "get_lineage", "get_related_memories"):
            self.assertIn(name, protocol.READ_TOOLS)
            self.assertNotIn(name, protocol.WRITE_TOOLS)
        self.assertNotIn("inspect_graph", protocol.READ_TOOLS)

    def test_protocol_classifies_api_ergonomics_gap_tools_correctly(self):
        # API-ergonomics Gap 1/2: both new tools must land in the daemon's crash/in-flight-write
        # classification (protocol.py's READ_TOOLS/WRITE_TOOLS), or a MID_CALL_FAILURE during
        # either falls through RpcBackend's exception handler to a raw re-raise instead of the
        # correct retry (reads) / DAEMON_CONNECTION_LOST_DURING_WRITE advisory (writes) -- see
        # SALTMDB memory 70cbcfc1 for the real gap this regression-guards against.
        self.assertIn("update_memory_metadata", protocol.WRITE_TOOLS)
        self.assertNotIn("update_memory_metadata", protocol.READ_TOOLS)
        self.assertIn("inspect_memory", protocol.READ_TOOLS)
        self.assertNotIn("inspect_memory", protocol.WRITE_TOOLS)
        # Both must also be registered for real dispatch, and inspect_memory must not be a
        # coordinator-serialized mutating call (matches get_memory's own read-only treatment).
        self.assertIn("update_memory_metadata", dispatch.DISPATCH_TABLE)
        self.assertIn("inspect_memory", dispatch.DISPATCH_TABLE)
        self.assertIn("update_memory_metadata", dispatch.MUTATING_TOOLS)
        self.assertNotIn("inspect_memory", dispatch.MUTATING_TOOLS)


if __name__ == "__main__":
    unittest.main()
