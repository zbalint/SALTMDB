import inspect
import unittest
from unittest.mock import patch

from saltmdb.daemon import dispatch, protocol
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class _CaptureBackend:
    def __init__(self):
        self.calls = []

    def call(self, tool_name, kwargs):
        self.calls.append((tool_name, kwargs))
        return {"tool": tool_name, "kwargs": kwargs}


class TestPhase4McpSurface(unittest.TestCase):
    def setUp(self):
        SESSION_IDENTITY.reset()
        self.backend = _CaptureBackend()
        self.previous_backend = tools._set_backend_for_test(self.backend)

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()

    def test_lifecycle_tools_are_typed_and_old_name_is_not_public(self):
        # Phase 4 adds two lifecycle intents and renames consolidation one-for-one.
        # dismiss_event and ephemeral_memory remain until their planned Phase 6/7 removals.
        self.assertEqual(len(tools.mcp._tool_manager._tools), 19)
        self.assertIn("revise_memory", tools.mcp._tool_manager._tools)
        self.assertIn("supersede_memory", tools.mcp._tool_manager._tools)
        self.assertIn("consolidate_memories", tools.mcp._tool_manager._tools)
        self.assertNotIn("commit_consolidation", tools.mcp._tool_manager._tools)
        self.assertIn("dismiss_event", tools.mcp._tool_manager._tools)
        self.assertIn("ephemeral_memory", tools.mcp._tool_manager._tools)
        for function in (tools.revise_memory, tools.supersede_memory):
            params = list(inspect.signature(function).parameters)
            self.assertEqual(params[:5], ["entity_id", "title", "content", "tags", "reason"])
            for required in params[:5]:
                self.assertIs(
                    inspect.signature(function).parameters[required].default,
                    inspect.Parameter.empty,
                )

    def test_replacements_forward_identity_and_intent(self):
        tools.revise_memory(
            entity_id="old",
            title="Revised",
            content="# Revised\n\nComplete representation.",
            tags=["#api"],
            reason="Fix an incomplete representation",
            owner_id="agent",
        )
        tools.supersede_memory(
            entity_id="old",
            title="Current",
            content="# Current\n\nNew knowledge.",
            tags=["#api"],
            reason="A newer decision replaced the old one",
            owner_id="agent",
            memory_type="decision",
        )
        self.assertEqual(
            [name for name, _ in self.backend.calls], ["revise_memory", "supersede_memory"]
        )
        self.assertEqual(self.backend.calls[0][1]["entity_id"], "old")
        self.assertEqual(self.backend.calls[0][1]["reason"], "Fix an incomplete representation")
        self.assertEqual(self.backend.calls[1][1]["memory_type"], "decision")

    def test_lifecycle_registration_and_protocol_classification(self):
        for name in ("revise_memory", "supersede_memory", "consolidate_memories"):
            self.assertIn(name, dispatch.DISPATCH_TABLE)
            self.assertIn(name, dispatch.MUTATING_TOOLS)
            self.assertIn(name, protocol.WRITE_TOOLS)
            self.assertNotIn(name, protocol.READ_TOOLS)
        for registry in (dispatch.DISPATCH_TABLE, dispatch.MUTATING_TOOLS, protocol.WRITE_TOOLS):
            self.assertNotIn("commit_consolidation", registry)

    @patch("saltmdb.daemon.dispatch.memory_service.revise_memory", return_value={"status": "ok"})
    def test_revise_dispatch_calls_lifecycle_service(self, revise):
        dispatch._dispatch_revise_memory(
            entity_id="old",
            title="Revised",
            content="content",
            tags=["#api"],
            reason="representation repair",
        )
        revise.assert_called_once_with(
            entity_id="old",
            title="Revised",
            content="content",
            tags=["#api"],
            reason="representation repair",
            owner_id=None,
            context_id=None,
            scope=None,
            memory_type=None,
        )

    @patch("saltmdb.daemon.dispatch.memory_service.supersede_memory", return_value={"status": "ok"})
    def test_supersede_dispatch_calls_lifecycle_service(self, supersede):
        dispatch._dispatch_supersede_memory(
            entity_id="old",
            title="Current",
            content="new content",
            tags=["#api"],
            reason="newer knowledge",
        )
        supersede.assert_called_once()
        self.assertEqual(supersede.call_args.kwargs["reason"], "newer knowledge")

    def test_replacement_requires_nonempty_tags(self):
        with self.assertRaisesRegex(ValueError, "tags is required"):
            dispatch._dispatch_revise_memory(
                entity_id="old",
                title="Revised",
                content="content",
                tags=[],
                reason="representation repair",
            )

    @patch(
        "saltmdb.daemon.dispatch.relation_service.consolidate_memories",
        return_value={"status": "ok"},
    )
    def test_consolidate_dispatch_calls_renamed_service(self, consolidate):
        dispatch._dispatch_consolidate_memories(
            parent_ids=["a", "b"], title="Merged", content="content", tags=["#api"]
        )
        consolidate.assert_called_once()
        self.assertEqual(consolidate.call_args.kwargs["parent_ids"], ["a", "b"])

    @patch.object(tools, "_backend_or_raise")
    def test_consolidation_uses_renamed_public_method(self, backend):
        backend.return_value.call.return_value = "ok"
        tools.consolidate_memories(
            parent_ids=["a", "b"], title="Merged", content="content", owner_id="agent"
        )
        backend.return_value.call.assert_called_once()
        self.assertEqual(backend.return_value.call.call_args.args[0], "consolidate_memories")


if __name__ == "__main__":
    unittest.main()
