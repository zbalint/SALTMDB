import os
import tempfile
import inspect
import unittest
from unittest.mock import patch

from saltmdb.config import MAX_MEMORY_CONTENT_CHARS
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service.lifecycle import revise_memory, supersede_memory
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
        SESSION_IDENTITY.configure_agent_id("test_agent")
        self.backend = _CaptureBackend()
        self.previous_backend = tools._set_backend_for_test(self.backend)

    def tearDown(self):
        tools._set_backend_for_test(self.previous_backend)
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_agent_id("test_agent")

    def test_lifecycle_tools_are_typed_and_old_name_is_not_public(self):
        # Phase 4 adds two lifecycle intents and renames consolidation one-for-one.
        # dismiss_event was removed in Phase 6; ephemeral_memory and export_corpus_snapshot were
        # removed in Phase 7 (18 -> 16, the plan's §2 target of exactly 16 MCP tools).
        # update_memory_metadata was added afterward (16 -> 17, API-ergonomics Gap 1), then
        # inspect_memory (17 -> 18, API-ergonomics Gap 2). Milestone A slice A5 added
        # retrieve_context (18 -> 19). Phase 1 conversation-trace-provenance added 5 new tools
        # (19 -> 24).
        self.assertEqual(len(tools.mcp._tool_manager._tools), 24)
        self.assertIn("revise_memory", tools.mcp._tool_manager._tools)
        self.assertIn("supersede_memory", tools.mcp._tool_manager._tools)
        self.assertIn("consolidate_memories", tools.mcp._tool_manager._tools)
        self.assertNotIn("commit_consolidation", tools.mcp._tool_manager._tools)
        self.assertNotIn("dismiss_event", tools.mcp._tool_manager._tools)
        self.assertNotIn("ephemeral_memory", tools.mcp._tool_manager._tools)
        self.assertNotIn("export_corpus_snapshot", tools.mcp._tool_manager._tools)
        for function in (tools.revise_memory, tools.supersede_memory):
            params = list(inspect.signature(function).parameters)
            self.assertEqual(
                params,
                [
                    "memory_id",
                    "title",
                    "reason",
                    "content",
                    "content_file_path",
                    "tags",
                    "scope",
                    "memory_type",
                    "retrieval_text",
                    "repoint_relations",
                    "context_id",
                    "core",
                    "demote_core",
                ],
            )
            for required in ("memory_id", "title", "reason"):
                self.assertIs(
                    inspect.signature(function).parameters[required].default,
                    inspect.Parameter.empty,
                )

    def test_replacements_forward_identity_and_intent(self):
        tools.revise_memory(
            memory_id="old",
            title="Revised",
            content="# Revised\n\nComplete representation.",
            tags=["#api"],
            reason="Fix an incomplete representation",
        )
        tools.supersede_memory(
            memory_id="old",
            title="Current",
            content="# Current\n\nNew knowledge.",
            tags=["#api"],
            reason="A newer decision replaced the old one",
            memory_type="decision",
        )
        self.assertEqual(
            [name for name, _ in self.backend.calls], ["revise_memory", "supersede_memory"]
        )
        self.assertEqual(self.backend.calls[0][1]["entity_id"], "old")
        self.assertEqual(self.backend.calls[0][1]["reason"], "Fix an incomplete representation")
        self.assertEqual(self.backend.calls[1][1]["memory_type"], "decision")

    def test_revise_rejects_content_over_maximum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = init_db(os.path.join(temp_dir, "test.db"))
            try:
                result = revise_memory(
                    entity_id="not-a-real-memory",
                    title="Revised Content",
                    content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
                    tags=["#length-cap"],
                    reason="Boundary validation test",
                    db_connection=conn,
                )
            finally:
                conn.close()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_TOO_LONG")

    def test_supersede_rejects_content_over_maximum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = init_db(os.path.join(temp_dir, "test.db"))
            try:
                result = supersede_memory(
                    entity_id="not-a-real-memory",
                    title="Superseded Content",
                    content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
                    tags=["#length-cap"],
                    reason="Boundary validation test",
                    db_connection=conn,
                )
            finally:
                conn.close()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "CONTENT_TOO_LONG")

    def test_lifecycle_registration_and_protocol_classification(self):
        for name in ("revise_memory", "supersede_memory", "consolidate_memories"):
            self.assertIn(name, dispatch.DISPATCH_TABLE)
            self.assertIn(name, dispatch.MUTATING_TOOLS)
            self.assertIn(name, protocol.WRITE_TOOLS)
            self.assertNotIn(name, protocol.READ_TOOLS)
        for registry in (dispatch.DISPATCH_TABLE, dispatch.MUTATING_TOOLS, protocol.WRITE_TOOLS):
            self.assertNotIn("commit_consolidation", registry)

    @patch("saltmdb.daemon.dispatch.memory_service.replace_memory", return_value={"status": "ok"})
    def test_revise_dispatch_calls_lifecycle_service(self, replace):
        dispatch._dispatch_revise_memory(
            entity_id="old",
            title="Revised",
            content="content",
            tags=["#api"],
            reason="representation repair",
        )
        replace.assert_called_once()
        operation, request = replace.call_args.args
        self.assertEqual(operation, "revises")
        self.assertEqual(request.entity_id, "old")
        self.assertEqual(request.reason, "representation repair")

    @patch("saltmdb.daemon.dispatch.memory_service.replace_memory", return_value={"status": "ok"})
    def test_supersede_dispatch_calls_lifecycle_service(self, replace):
        dispatch._dispatch_supersede_memory(
            entity_id="old",
            title="Current",
            content="new content",
            tags=["#api"],
            reason="newer knowledge",
        )
        replace.assert_called_once()
        operation, request = replace.call_args.args
        self.assertEqual(operation, "supersedes")
        self.assertEqual(request.reason, "newer knowledge")

    @patch(
        "saltmdb.daemon.dispatch.memory_service.replace_memory",
        return_value={
            "status": "rejected",
            "errors": [
                {
                    "code": "INVALID_TAGS",
                    "message": "tags must be a non-empty list of non-empty strings.",
                    "field": "tags",
                }
            ],
            "warnings": [],
        },
    )
    def test_replacement_requires_nonempty_tags(self, replace):
        result = dispatch._dispatch_revise_memory(
            entity_id="old",
            title="Revised",
            content="content",
            tags=[],
            reason="representation repair",
        )
        replace.assert_called_once()
        self.assertEqual(replace.call_args.args[1].tags, [])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "INVALID_TAGS")

    @patch(
        "saltmdb.daemon.dispatch.relation_service.apply_consolidations",
        return_value={"status": "ok"},
    )
    def test_consolidate_dispatch_calls_batch_service(self, apply):
        dispatch._dispatch_consolidate_memories(
            items=[{"parent_ids": ["a", "b"], "title": "Merged", "content": "content"}]
        )
        apply.assert_called_once()
        self.assertEqual(apply.call_args.kwargs["items"][0]["parent_ids"], ["a", "b"])

    @patch.object(tools, "_backend_or_raise")
    def test_consolidation_uses_batch_public_method(self, backend):
        backend.return_value.call.return_value = {"status": "ok"}
        tools.consolidate_memories(
            items=[
                {
                    "parent_memory_ids": ["a", "b"],
                    "title": "Merged",
                    "content": "content",
                }
            ]
        )
        backend.return_value.call.assert_called_once()
        self.assertEqual(backend.return_value.call.call_args.args[0], "consolidate_memories")


if __name__ == "__main__":
    unittest.main()
