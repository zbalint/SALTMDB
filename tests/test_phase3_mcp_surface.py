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
        self.previous_backend = tools._set_backend_for_test(_CaptureBackend())

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()

    def test_tool_count_and_registration(self):
        self.assertEqual(len(tools.mcp._tool_manager._tools), 17)
        self.assertIn("get_memory", dispatch.DISPATCH_TABLE)
        self.assertIn("get_lineage", dispatch.DISPATCH_TABLE)
        self.assertIn("get_related_memories", dispatch.DISPATCH_TABLE)
        self.assertNotIn("inspect_graph", dispatch.DISPATCH_TABLE)

    def test_search_schema_has_no_explicit_fetch_controls(self):
        params = inspect.signature(tools.search_memory).parameters
        self.assertNotIn("entity_id", params)
        self.assertNotIn("fetch_full", params)

    def test_graph_tools_have_small_explicit_schemas(self):
        self.assertEqual(
            list(inspect.signature(tools.get_memory).parameters), ["entity_id", "owner_id"]
        )
        self.assertEqual(
            list(inspect.signature(tools.get_lineage).parameters),
            ["entity_id", "direction", "max_depth", "owner_id"],
        )
        self.assertEqual(
            list(inspect.signature(tools.get_related_memories).parameters),
            ["entity_id", "max_depth", "owner_id"],
        )

    def test_graph_tools_forward_normalized_calls(self):
        tools.get_memory(entity_id="memory-id", owner_id="agent")
        tools.get_lineage(entity_id="memory-id", owner_id="agent")
        tools.get_related_memories(entity_id="memory-id", owner_id="agent")
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


if __name__ == "__main__":
    unittest.main()
