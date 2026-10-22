"""Public MCP contracts for S3a consolidate_memories."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_contract
from saltmdb.mcp import tools
from saltmdb.mcp.guard import GuardedFastMCP


class TestMcpConsolidateToolV2(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection, self.previous_backend, self.previous_db_path = mcp_contract.configure(
            Path(self.temp_dir.name) / "consolidate.db"
        )

    def tearDown(self):
        mcp_contract.cleanup(self.connection, self.previous_backend, self.previous_db_path)
        self.temp_dir.cleanup()

    async def test_schema_is_items_and_context_only(self):
        self.assertEqual(
            await mcp_contract.published_properties("consolidate_memories"),
            ("items", "context_id"),
        )
        result = await mcp_contract.call("consolidate_memories", {"items": [], "context_id": "ctx"})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")

    async def test_schema_has_no_result_wrapper_on_the_tool(self):
        registered = {tool.name: tool for tool in await mcp_contract.mcp.list_tools()}
        self.assertFalse(
            GuardedFastMCP._has_result_wrapper(registered["consolidate_memories"].outputSchema)
        )

    async def test_zero_and_21_items_are_validation_errors(self):
        empty = await mcp_contract.call("consolidate_memories", {"items": []})
        too_many = await mcp_contract.call(
            "consolidate_memories",
            {
                "items": [
                    {"parent_memory_ids": ["a", "b"], "title": str(index), "content": "content"}
                    for index in range(21)
                ]
            },
        )
        self.assertEqual(empty["status"], "rejected")
        self.assertEqual(too_many["status"], "rejected")
        self.assertEqual(empty["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(too_many["errors"][0]["code"], "VALIDATION_ERROR")

    async def test_removed_fields_are_unknown_at_their_public_paths(self):
        cases = (
            ({"parent_ids": ["a", "b"]}, "items[0].parent_ids"),
            ({"weight": 2}, "items[0].weight"),
            ({"is_core": True}, "items[0].is_core"),
            ({"agent_id": "attacker"}, "items[0].agent_id"),
            ({"title": "top-level"}, "title"),
        )
        for extra, field in cases:
            with self.subTest(field=field):
                arguments = {
                    "items": [
                        {
                            "parent_memory_ids": ["a", "b"],
                            "title": "Result",
                            "content": "Durable result content.",
                            **extra,
                        }
                    ]
                }
                if field == "title":
                    arguments["title"] = "top-level"
                protocol_result = await mcp_contract.call_protocol(
                    "consolidate_memories", arguments
                )
                payload = json.loads(protocol_result.content[0].text)
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(payload["errors"][0]["code"], "UNKNOWN_FIELD")
                self.assertEqual(payload["errors"][0]["field"], field)

    async def test_content_and_core_errors_are_collected_without_dispatch(self):
        calls = []
        content_path = Path(self.temp_dir.name) / "both.md"
        content_path.write_text("file content", encoding="utf-8")
        with patch.object(
            tools, "_call_and_record_write", side_effect=lambda *args: calls.append(args)
        ):
            result = await mcp_contract.call(
                "consolidate_memories",
                {
                    "items": [
                        {"parent_memory_ids": ["a", "b"], "title": "neither"},
                        {
                            "parent_memory_ids": ["c", "d"],
                            "title": "both",
                            "content": "inline",
                            "content_file_path": str(content_path),
                        },
                        {
                            "parent_memory_ids": ["e", "f"],
                            "title": "core-conflict",
                            "content": "content",
                            "core": {"reason": "reason", "exit_condition": "exit"},
                            "demote_core": True,
                        },
                    ]
                },
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            [error["field"] for error in result["errors"]],
            ["items[0].content", "items[1].content", "items[2].demote_core"],
        )
        self.assertEqual(result["errors"][2]["code"], "CORE_AND_DEMOTE_CONFLICT")
        self.assertEqual(calls, [])

    async def test_content_file_is_read_and_json_string_items_are_preparsed(self):
        content_path = Path(self.temp_dir.name) / "content.md"
        content_path.write_text("Durable file content.", encoding="utf-8")
        seen = []
        with patch.object(
            tools,
            "_call_and_record_write",
            side_effect=lambda name, kwargs: seen.append((name, kwargs))
            or {"status": "ok", "data": {"items": []}},
        ):
            result = await mcp_contract.call(
                "consolidate_memories",
                {
                    "items": json.dumps(
                        [
                            {
                                "parent_memory_ids": ["a", "b"],
                                "title": "file-content",
                                "content_file_path": str(content_path),
                            }
                        ]
                    )
                },
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen[0][1]["items"][0]["content"], "Durable file content.")

    async def test_nested_unknown_and_old_fields_are_rejected(self):
        result = await mcp_contract.call_protocol(
            "consolidate_memories",
            {
                "items": [
                    {
                        "parent_memory_ids": ["a", "b"],
                        "title": "Result",
                        "content": "Durable result content.",
                        "weight": 2,
                    }
                ]
            },
        )
        payload = result.content[0].text
        self.assertIn("items[0].weight", payload)

    async def test_direct_python_call_maps_public_item_to_internal_batch(self):
        with patch.object(
            tools, "_call_and_record_write", return_value={"status": "ok", "data": {"items": []}}
        ) as call:
            result = tools.consolidate_memories(
                items=[
                    {
                        "parent_memory_ids": ["a", "b"],
                        "title": "Mapped",
                        "content": "Durable mapped content.",
                        "demote_core": True,
                    }
                ],
                context_id="batch-context",
            )
        self.assertEqual(result["status"], "ok")
        kwargs = call.call_args.args[1]
        self.assertEqual(kwargs["items"][0]["parent_ids"], ["a", "b"])
        self.assertIs(kwargs["items"][0]["is_core"], False)
        self.assertEqual(kwargs["context_id"], "batch-context")

    async def test_adapter_drops_none_item_keys_before_dispatch(self):
        with patch.object(
            tools,
            "_call_and_record_write",
            return_value={"status": "ok", "data": {"items": []}},
        ) as call:
            result = await mcp_contract.call(
                "consolidate_memories",
                {
                    "items": [
                        {
                            "parent_memory_ids": ["a", "b"],
                            "title": "No none keys",
                            "content": "Durable mapped content.",
                        }
                    ],
                    "context_id": "batch-context",
                },
            )
        self.assertEqual(result["status"], "ok")
        dispatched = call.call_args.args[1]["items"][0]
        self.assertEqual(
            set(dispatched),
            {"parent_ids", "title", "content", "scope"},
        )
        self.assertNotIn("context_id", dispatched)
        self.assertNotIn("tags", dispatched)
        self.assertNotIn("override_justification", dispatched)

    async def test_core_decision_error_has_full_corrected_calls(self):
        content_path = Path(self.temp_dir.name) / "core-content.md"
        content_path.write_text("Durable content from a file.", encoding="utf-8")
        core_result = {
            "status": "rejected",
            "errors": [
                {
                    "code": "CORE_DECISION_REQUIRED",
                    "message": "internal",
                    "field": "items[0].parent_memory_ids",
                    "corrected_calls": [
                        {
                            "core": {
                                "reason": "retain because required",
                                "exit_condition": "replace later",
                            }
                        },
                        {"demote_core": True},
                    ],
                }
            ],
        }
        with patch.object(tools, "_call_and_record_write", return_value=core_result):
            result = tools.consolidate_memories(
                items=[
                    {
                        "parent_memory_ids": ["a", "b"],
                        "title": "Core result",
                        "content_file_path": str(content_path),
                    }
                ],
                context_id="ctx",
            )
        self.assertEqual(result["errors"][0]["code"], "CORE_DECISION_REQUIRED")
        self.assertEqual(len(result["errors"][0]["corrected_calls"]), 2)
        self.assertEqual(
            result["errors"][0]["corrected_calls"][0]["items"][0]["core"],
            {"reason": "retain because required", "exit_condition": "replace later"},
        )
        self.assertEqual(result["errors"][0]["corrected_calls"][1]["items"][0]["demote_core"], True)
        self.assertTrue(
            all(
                call["items"][0]["content_file_path"] == str(content_path)
                for call in result["errors"][0]["corrected_calls"]
            )
        )

    async def test_core_completion_patches_only_index_one_and_keeps_context(self):
        submitted = [
            {
                "parent_memory_ids": ["a", "b"],
                "title": "first",
                "content": "First durable content.",
            },
            {
                "parent_memory_ids": ["c", "d"],
                "title": "core-item",
                "content": "Core durable content.",
            },
            {
                "parent_memory_ids": ["e", "f"],
                "title": "third",
                "content": "Third durable content.",
            },
        ]
        result = {
            "status": "rejected",
            "errors": [
                {
                    "code": "CORE_DECISION_REQUIRED",
                    "field": "items[1].parent_memory_ids",
                    "message": "internal",
                    "corrected_calls": [
                        {
                            "core": {
                                "reason": "keep core reason with enough detail",
                                "exit_condition": "keep core exit with enough detail",
                            }
                        },
                        {"demote_core": True},
                    ],
                }
            ],
        }
        with patch.object(tools, "_call_and_record_write", return_value=result):
            completed = tools.consolidate_memories(items=submitted, context_id="batch-context")
        corrected = completed["errors"][0]["corrected_calls"]
        baseline = [tools._public_consolidation_item(item) for item in submitted]
        for call in corrected:
            self.assertEqual(call["context_id"], "batch-context")
            self.assertEqual(call["items"][0], baseline[0])
            self.assertEqual(call["items"][2], baseline[2])
        self.assertIn("core", corrected[0]["items"][1])
        self.assertTrue(corrected[1]["items"][1]["demote_core"])

    async def test_public_core_round_trip_uses_exact_message_and_resubmittable_call(self):
        parent_one = (
            await mcp_contract.call(
                "store_memory",
                {
                    "title": "roundtrip-a",
                    "content": "A durable parent with enough detail for storage.",
                },
            )
        )["data"]["memory_id"]
        parent_two = (
            await mcp_contract.call(
                "store_memory",
                {
                    "title": "roundtrip-b",
                    "content": "B durable parent with enough detail for storage.",
                },
            )
        )["data"]["memory_id"]
        self.connection.execute(
            "UPDATE entities SET is_core = 1, core_reason = ?, core_exit_condition = ?, core_review_after = ? WHERE id = ?",
            (
                "roundtrip reason with enough detail",
                "roundtrip exit condition with enough detail",
                "2099-01-01T00:00:00+00:00",
                parent_one,
            ),
        )
        self.connection.commit()
        first = await mcp_contract.call(
            "consolidate_memories",
            {
                "items": [
                    {
                        "parent_memory_ids": [parent_one, parent_two],
                        "title": "roundtrip-result",
                        "content": "A durable roundtrip synthesis.",
                    }
                ]
            },
        )
        error = first["errors"][0]
        self.assertEqual(error["code"], "CORE_DECISION_REQUIRED")
        self.assertEqual(
            error["message"],
            "One or more parents of this item is an active core memory. Resend the item with core (keep the result core: reason and exit_condition) or demote_core true (let the result become an ordinary memory).",
        )
        keep_call, demote_call = error["corrected_calls"]
        kept = await mcp_contract.call("consolidate_memories", keep_call)
        self.assertEqual(kept["status"], "ok", kept)
        self.assertEqual(
            self.connection.execute(
                "SELECT is_core, core_reason, core_exit_condition FROM entities WHERE id = ?",
                (kept["data"]["items"][0]["memory_id"],),
            ).fetchone(),
            (
                1,
                "roundtrip reason with enough detail",
                "roundtrip exit condition with enough detail",
            ),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM entities WHERE id = ?", (parent_one,)
            ).fetchone()[0],
            "archived",
        )

        demoted = await mcp_contract.call("consolidate_memories", demote_call)
        self.assertEqual(demoted["status"], "rejected", demoted)
        self.assertEqual(demoted["errors"][0]["code"], "INACTIVE_PARENT")

    async def test_public_core_and_demote_core_map_lifecycle(self):
        async def parent(title):
            return (
                await mcp_contract.call(
                    "store_memory",
                    {
                        "title": title,
                        "content": f"A durable parent for {title} with enough detail.",
                    },
                )
            )["data"]["memory_id"]

        core_parents = [await parent("public-core-a"), await parent("public-core-b")]
        core_result = await mcp_contract.call(
            "consolidate_memories",
            {
                "items": [
                    {
                        "parent_memory_ids": core_parents,
                        "title": "public-core-result",
                        "content": "A durable public core synthesis.",
                        "core": {
                            "reason": "public core reason with enough detail",
                            "exit_condition": "public core exit with enough detail",
                        },
                    }
                ]
            },
        )
        self.assertEqual(core_result["status"], "ok", core_result)
        core_row = self.connection.execute(
            "SELECT is_core, core_reason, core_exit_condition FROM entities WHERE id = ?",
            (core_result["data"]["items"][0]["memory_id"],),
        ).fetchone()
        self.assertEqual(
            core_row,
            (1, "public core reason with enough detail", "public core exit with enough detail"),
        )

        demote_parents = [await parent("public-demote-a"), await parent("public-demote-b")]
        demote_result = await mcp_contract.call(
            "consolidate_memories",
            {
                "items": [
                    {
                        "parent_memory_ids": demote_parents,
                        "title": "public-demote-result",
                        "content": "A durable public ordinary synthesis.",
                        "demote_core": True,
                    }
                ]
            },
        )
        self.assertEqual(demote_result["status"], "ok", demote_result)
        self.assertEqual(
            self.connection.execute(
                "SELECT is_core FROM entities WHERE id = ?",
                (demote_result["data"]["items"][0]["memory_id"],),
            ).fetchone()[0],
            0,
        )

    async def test_rejected_batch_does_not_queue_trace_ids(self):
        with patch.object(tools, "is_trace_capture_enabled", return_value=True):
            result = tools._queue_trace_write(
                "consolidate_memories",
                {"status": "rejected", "errors": [{"code": "CORE_DECISION_REQUIRED"}]},
            )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(tools._drain_pending_trace_writes(), [])

    async def test_trace_queue_records_every_batch_id(self):
        with patch.object(tools, "is_trace_capture_enabled", return_value=True):
            result = tools._queue_trace_write(
                "consolidate_memories",
                {"status": "ok", "data": {"items": [{"memory_id": "m1"}, {"memory_id": "m2"}]}},
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            tools._drain_pending_trace_writes(),
            [("consolidate_memories", "m1"), ("consolidate_memories", "m2")],
        )
