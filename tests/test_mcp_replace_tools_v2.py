from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


def test_replacement_rejects_core_and_demote_conflict():
    core = tools.CoreInput(
        reason="A sufficiently long core reason",
        exit_condition="A sufficiently long core exit condition",
    )
    result = tools._replacement_payload(
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
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CORE_AND_DEMOTE_CONFLICT"


def test_replacement_rejects_both_content_sources():
    result = tools._replacement_call(
        "revise_memory",
        tools.revise_memory,
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
