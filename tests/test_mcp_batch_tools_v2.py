"""Public MCP contracts for list-only batch tools."""

import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from typing import Any
import mcp_contract


class TestMcpBatchToolsV2(unittest.IsolatedAsyncioTestCase):
    temp_dir: tempfile.TemporaryDirectory[str]
    connection: Any
    previous_backend: Any
    previous_db_path: str | None

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection, self.previous_backend, self.previous_db_path = mcp_contract.configure(
            Path(self.temp_dir.name) / "batch-mcp.db"
        )

    async def asyncTearDown(self):
        mcp_contract.cleanup(self.connection, self.previous_backend, self.previous_db_path)
        self.temp_dir.cleanup()

    async def test_required_list_properties_and_no_output_wrapper(self):
        archive = await mcp_contract.published_properties("archive_memory")
        relation = await mcp_contract.published_properties("manage_relation")
        self.assertEqual(archive, ("memory_ids",))
        self.assertEqual(relation, ("relations",))
        archive_result = await mcp_contract.call("archive_memory", {"memory_ids": []})
        relation_result = await mcp_contract.call("manage_relation", {"relations": []})
        self.assertEqual(archive_result["status"], "rejected")
        self.assertEqual(relation_result["status"], "rejected")

    async def test_old_single_keywords_and_nested_extras_reject_without_writes(self):
        before = self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        old_archive = await mcp_contract.call("archive_memory", {"entity_id": "x"})
        old_relation = await mcp_contract.call(
            "manage_relation",
            {"source_id": "a", "target_id": "b", "predicate": "part_of"},
        )
        nested = await mcp_contract.call(
            "manage_relation",
            {
                "relations": [
                    {
                        "source_id": "a",
                        "target_id": "b",
                        "predicate": "part_of",
                        "agent_id": "attacker",
                    }
                ]
            },
        )
        for result in (old_archive, old_relation, nested):
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], "UNKNOWN_FIELD")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], before
        )

    async def test_json_string_relation_list_is_accepted(self):
        result = await mcp_contract.call(
            "manage_relation",
            {"relations": '[{"source_id":"a","target_id":"b","predicate":"part_of"}]'},
        )
        self.assertIn(result["status"], {"ok", "rejected"})

    async def test_collection_limits_and_every_rejection_is_zero_change(self):
        before = self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        archive = await mcp_contract.call("archive_memory", {"memory_ids": ["x"] * 51})
        relation = await mcp_contract.call(
            "manage_relation",
            {"relations": [{"source_id": "a", "target_id": "b", "predicate": "part_of"}] * 101},
        )
        for result, field in ((archive, "memory_ids"), (relation, "relations")):
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
            self.assertEqual(result["errors"][0]["field"], field)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], before
        )

    async def test_nested_unknown_fields_are_rejected_before_dispatch(self):
        before = self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        for key, expected_field in (
            ("agent_id", "relations[0].agent_id"),
            ("unexpected", "relations[0].unexpected"),
        ):
            result = await mcp_contract.call(
                "manage_relation",
                {
                    "relations": [
                        {
                            "source_id": "a",
                            "target_id": "b",
                            "predicate": "part_of",
                            key: "attacker",
                        }
                    ]
                },
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], "UNKNOWN_FIELD")
            self.assertEqual(result["errors"][0]["field"], expected_field)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], before
        )

    async def test_predicate_rejection_exposes_batch_corrected_call(self):
        result = await mcp_contract.call(
            "manage_relation",
            {
                "relations": [
                    {"source_id": "source", "target_id": "target", "predicate": "relates_to"}
                ]
            },
        )
        self.assertEqual(result["status"], "rejected")
        error = result["errors"][0]
        self.assertEqual(error["code"], "NONCANONICAL_PREDICATE")
        self.assertEqual(error["field"], "relations[0].predicate")
        self.assertEqual(error["corrected_call"]["relations"][0]["predicate"], "related_to")

    async def test_removed_single_form_keywords_reject_each_without_backend_dispatch(self):
        before = self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        for arguments in (
            {"source_id": "a", "target_id": "b", "predicate": "part_of"},
            {"entity_id": "memory"},
        ):
            name = "manage_relation" if "source_id" in arguments else "archive_memory"
            result = await mcp_contract.call(name, arguments)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], "UNKNOWN_FIELD")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], before
        )

    async def test_batch_callers_probe_trace_enabled_shared_backend_helpers(self):
        backend = Mock()
        backend.call.side_effect = [
            {"status": "ok", "data": {"items": [], "archived_count": 0}},
            {"status": "ok", "data": {"items": [], "created_count": 0, "duplicate_count": 0}},
        ]
        with (
            patch("saltmdb.mcp.tools._backend_or_raise", return_value=backend),
            patch("saltmdb.mcp.tools.is_trace_capture_enabled", return_value=True),
        ):
            archive = __import__("saltmdb.mcp.tools", fromlist=["archive_memory"]).archive_memory(
                memory_ids=["memory-id"]
            )
            relations = __import__(
                "saltmdb.mcp.tools", fromlist=["manage_relation"]
            ).manage_relation(
                relations=[{"source_id": "source", "target_id": "target", "predicate": "part_of"}]
            )
        self.assertEqual(archive["status"], "ok")
        self.assertEqual(relations["status"], "ok")
        self.assertEqual(backend.call.call_args_list[0].args[0], "archive_memory")
        self.assertEqual(backend.call.call_args_list[1].args[0], "manage_relation")
