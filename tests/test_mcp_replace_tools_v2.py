from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_contract

from saltmdb.mcp import tools


def test_replacement_public_signature_uses_memory_id_and_reason():
    assert list(tools.revise_memory.__annotations__)[:3] == ["memory_id", "title", "reason"]
    assert "entity_id" not in tools.revise_memory.__annotations__


def test_replacement_public_result_maps_both_lineage_ids():
    result = tools._to_public_write_result(
        "supersede_memory", {"status": "ok", "data": {"old_id": "old", "new_id": "new"}}
    )
    assert result["data"] == {"predecessor_memory_id": "old", "successor_memory_id": "new"}


def test_replacement_payload_maps_all_public_fields_for_both_tools():
    captured = []

    def capture(tool_name, payload):
        captured.append((tool_name, payload))
        return {"status": "ok", "data": {}, "warnings": []}

    with (
        tempfile.TemporaryDirectory() as temp_dir,
        patch.object(tools, "_call_and_record_write", side_effect=capture),
        patch.object(tools, "_effective_agent_id", return_value="mapping-agent"),
    ):
        for tool_name, tool_func in (
            ("revise_memory", tools.revise_memory),
            ("supersede_memory", tools.supersede_memory),
        ):
            direct = {
                "memory_id": f"{tool_name}-direct-id",
                "title": f"{tool_name}-direct-title",
                "reason": f"{tool_name}-direct-reason",
                "content": f"{tool_name}-direct-content",
                "content_file_path": None,
                "tags": ["#mapping-direct"],
                "scope": "private",
                "memory_type": "decision",
                "retrieval_text": f"{tool_name}-direct-retrieval",
                "repoint_relations": True,
                "context_id": f"{tool_name}-direct-context",
                "core": None,
                "demote_core": False,
            }
            assert tool_func(**direct)["status"] == "ok"
            assert captured[-1] == (
                tool_name,
                {
                    "entity_id": f"{tool_name}-direct-id",
                    "title": f"{tool_name}-direct-title",
                    "content": f"{tool_name}-direct-content",
                    "reason": f"{tool_name}-direct-reason",
                    "tags": ["#mapping-direct"],
                    "context_id": f"{tool_name}-direct-context",
                    "scope": "private",
                    "memory_type": "decision",
                    "retrieval_text": f"{tool_name}-direct-retrieval",
                    "repoint_relations": True,
                    "agent_id": "mapping-agent",
                },
            )

            content_path = Path(temp_dir) / f"{tool_name}-core.md"
            content_path.write_text(f"{tool_name}-core-file-content")
            core = tools.CoreInput(
                reason=f"{tool_name}-core-reason",
                exit_condition=f"{tool_name}-core-exit",
                review_after="2030-01-02",
                detail_memory_ids=[f"{tool_name}-core-detail"],
            )
            core_values = {
                "memory_id": f"{tool_name}-core-id",
                "title": f"{tool_name}-core-title",
                "reason": f"{tool_name}-core-replacement-reason",
                "content": None,
                "content_file_path": str(content_path),
                "tags": ["#mapping-core"],
                "scope": "shared",
                "memory_type": "event",
                "retrieval_text": f"{tool_name}-core-retrieval",
                "repoint_relations": False,
                "context_id": f"{tool_name}-core-context",
                "core": core,
                "demote_core": False,
            }
            assert tool_func(**core_values)["status"] == "ok"
            assert captured[-1] == (
                tool_name,
                {
                    "entity_id": f"{tool_name}-core-id",
                    "title": f"{tool_name}-core-title",
                    "content": f"{tool_name}-core-file-content",
                    "reason": f"{tool_name}-core-replacement-reason",
                    "tags": ["#mapping-core"],
                    "context_id": f"{tool_name}-core-context",
                    "scope": "shared",
                    "memory_type": "event",
                    "retrieval_text": f"{tool_name}-core-retrieval",
                    "repoint_relations": False,
                    "agent_id": "mapping-agent",
                    "is_core": True,
                    "core_reason": f"{tool_name}-core-reason",
                    "core_exit_condition": f"{tool_name}-core-exit",
                    "core_review_after": "2030-01-02",
                    "detail_memory_ids": [f"{tool_name}-core-detail"],
                },
            )

            demoted = {
                "memory_id": f"{tool_name}-demote-id",
                "title": f"{tool_name}-demote-title",
                "reason": f"{tool_name}-demote-reason",
                "content": f"{tool_name}-demote-content",
                "content_file_path": None,
                "tags": ["#mapping-demote"],
                "scope": "private",
                "memory_type": "fact",
                "retrieval_text": f"{tool_name}-demote-retrieval",
                "repoint_relations": True,
                "context_id": f"{tool_name}-demote-context",
                "core": None,
                "demote_core": True,
            }
            assert tool_func(**demoted)["status"] == "ok"
            assert captured[-1] == (
                tool_name,
                {
                    "entity_id": f"{tool_name}-demote-id",
                    "title": f"{tool_name}-demote-title",
                    "content": f"{tool_name}-demote-content",
                    "reason": f"{tool_name}-demote-reason",
                    "tags": ["#mapping-demote"],
                    "context_id": f"{tool_name}-demote-context",
                    "scope": "private",
                    "memory_type": "fact",
                    "retrieval_text": f"{tool_name}-demote-retrieval",
                    "repoint_relations": True,
                    "agent_id": "mapping-agent",
                    "is_core": False,
                },
            )


def test_replacement_rejects_core_and_demote_conflict():
    core = tools.CoreInput(
        reason="A sufficiently long core reason",
        exit_condition="A sufficiently long core exit condition",
    )
    result = tools._replacement_payload(
        tools._ReplacementArgs(
            memory_id="old",
            title="Replacement",
            reason="Reason",
            content="Body",
            content_file_path=None,
            tags=None,
            scope=None,
            memory_type=None,
            retrieval_text=None,
            repoint_relations=False,
            context_id=None,
            core=core,
            demote_core=True,
        ),
        "Body",
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CORE_AND_DEMOTE_CONFLICT"


def test_replacement_rejects_both_content_sources():
    result = tools._replacement_call(
        "revise_memory",
        tools.revise_memory,
        tools._ReplacementArgs(
            memory_id="old",
            title="Replacement",
            reason="Reason",
            content="Body",
            content_file_path="/tmp/content.md",
            tags=None,
            scope=None,
            memory_type=None,
            retrieval_text=None,
            repoint_relations=False,
            context_id=None,
            core=None,
            demote_core=False,
        ),
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CONTENT_AND_FILE_PATH_BOTH_SET"


def test_replacement_errors_and_changed_core_echo_keep_public_fields():
    result = tools._to_public_write_result(
        "revise_memory",
        {
            "status": "rejected",
            "errors": [{"code": "TARGET_CHANGED", "field": "entity_id"}],
            "warnings": [],
        },
    )
    assert result["errors"][0]["field"] == "memory_id"
    ok = tools._to_public_write_result(
        "revise_memory",
        {
            "status": "ok",
            "data": {
                "old_id": "old",
                "new_id": "new",
                "changed": {"core": {"reason": "x"}, "demote_core": False},
            },
        },
    )
    assert ok["data"]["changed"]["core"]["reason"] == "x"
    assert ok["data"]["changed"]["demote_core"] is False


class TestMcpReplacementContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection, self.previous_backend, self.previous_db_path = mcp_contract.configure(
            Path(self.temp_dir.name) / "replacement.db"
        )

    async def asyncTearDown(self):
        mcp_contract.cleanup(self.connection, self.previous_backend, self.previous_db_path)
        self.temp_dir.cleanup()

    async def test_public_schema_uses_memory_id_and_core_controls(self):
        for name in ("revise_memory", "supersede_memory"):
            properties = await mcp_contract.published_properties(name)
            self.assertIn("memory_id", properties)
            self.assertNotIn("entity_id", properties)
            self.assertIn("core", properties)
            self.assertIn("demote_core", properties)

    async def test_active_core_corrected_calls_resubmit_with_content_file(self):
        created = await mcp_contract.call(
            "store_memory",
            {
                "title": "Public replacement core",
                "content": "A durable public core predecessor body with enough descriptive content.",
                "core": {
                    "reason": "A sufficiently long public core reason",
                    "exit_condition": "A sufficiently long public core exit condition",
                },
            },
        )
        content_path = Path(self.temp_dir.name) / "replacement.md"
        content_path.write_text("A durable public successor body with enough descriptive content.")
        rejected = await mcp_contract.call(
            "revise_memory",
            {
                "memory_id": created["data"]["memory_id"],
                "title": "Public replacement successor",
                "reason": "Corrected public representation",
                "content_file_path": str(content_path),
            },
        )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(rejected["errors"][0]["code"], "CORE_DECISION_REQUIRED")
        corrected = rejected["errors"][0]["corrected_calls"]
        self.assertEqual(len(corrected), 2)
        for call in corrected:
            self.assertEqual(call["memory_id"], created["data"]["memory_id"])
            self.assertEqual(call["content_file_path"], str(content_path))
        kept = await mcp_contract.call("revise_memory", corrected[0])
        self.assertEqual(kept["status"], "ok", kept)
        self.assertIn("successor_memory_id", kept["data"])
        created_again = await mcp_contract.call(
            "store_memory",
            {
                "title": "Public replacement core two",
                "content": "A durable second public core predecessor body with enough descriptive content.",
                "core": {
                    "reason": "A sufficiently long public core reason two",
                    "exit_condition": "A sufficiently long public core exit condition two",
                },
            },
        )
        rejected_again = await mcp_contract.call(
            "supersede_memory",
            {
                "memory_id": created_again["data"]["memory_id"],
                "title": "Public supersede successor",
                "reason": "Newer public facts",
                "content": "A durable public supersede body with enough descriptive content.",
            },
        )
        self.assertEqual(rejected_again["status"], "rejected", rejected_again)
        demoted = await mcp_contract.call(
            "supersede_memory", rejected_again["errors"][0]["corrected_calls"][1]
        )
        self.assertEqual(demoted["status"], "ok", demoted)
        self.assertIn("successor_memory_id", demoted["data"])

    async def test_private_core_keep_fragment_requests_shared_scope(self):
        created = await mcp_contract.call(
            "store_memory",
            {
                "title": "Private replacement core",
                "content": "A durable private core predecessor body with enough descriptive content.",
                "core": {
                    "reason": "A sufficiently long private core reason",
                    "exit_condition": "A sufficiently long private core exit condition",
                },
            },
        )
        self.assertEqual(created["status"], "ok", created)
        self.connection.execute(
            "UPDATE entities SET scope = 'private' WHERE id = ?",
            (created["data"]["memory_id"],),
        )
        self.connection.commit()
        rejected = await mcp_contract.call(
            "revise_memory",
            {
                "memory_id": created["data"]["memory_id"],
                "title": "Private successor",
                "reason": "Private scope test",
                "content": "A durable private successor body with enough descriptive content.",
            },
        )
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(rejected["errors"][0]["code"], "CORE_DECISION_REQUIRED")
        self.assertEqual(rejected["errors"][0]["corrected_calls"][0]["scope"], "shared")
