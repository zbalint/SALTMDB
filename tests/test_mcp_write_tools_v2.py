from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import mcp_contract
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class TestMCPWriteToolsV2(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_disable_librarian = os.environ.get("SALTMDB_DISABLE_LIBRARIAN")
        os.environ["SALTMDB_DISABLE_LIBRARIAN"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection, self.previous_backend, self.previous_db_path = mcp_contract.configure(
            Path(self.temp_dir.name) / "write-v2.db"
        )

    async def asyncTearDown(self):
        mcp_contract.cleanup(self.connection, self.previous_backend, self.previous_db_path)
        if self.previous_disable_librarian is None:
            os.environ.pop("SALTMDB_DISABLE_LIBRARIAN", None)
        else:
            os.environ["SALTMDB_DISABLE_LIBRARIAN"] = self.previous_disable_librarian
        self.temp_dir.cleanup()

    async def test_store_create_response_uses_memory_id_and_rejects_entity_id(self):
        result = await mcp_contract.call(
            "store_memory",
            {"title": "Create-only memory", "content": "A durable create-only memory record."},
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("memory_id", result["data"])
        self.assertNotIn("id", result["data"])
        before = self._counts()
        rejected = await mcp_contract.call(
            "store_memory",
            {"entity_id": result["data"]["memory_id"], "title": "x", "content": "y"},
        )
        self.assertEqual(rejected["errors"][0]["code"], "UNKNOWN_FIELD")
        self.assertEqual(self._counts(), before)

    async def test_core_nested_model_and_null_tags(self):
        result = await mcp_contract.call(
            "store_memory",
            {
                "title": "Core nested memory",
                "content": "A temporary hazard requires immediate visibility before retrieval.",
                "tags": None,
                "core": {
                    "reason": "Immediate hazard requires visibility",
                    "exit_condition": "Hazard is repaired and verified",
                },
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("memory_id", result["data"])
        row = self.connection.execute(
            "SELECT is_core, core_reason, core_exit_condition FROM entities WHERE id = ?",
            (result["data"]["memory_id"],),
        ).fetchone()
        self.assertTrue(row[0])
        self.assertEqual(row[1], "Immediate hazard requires visibility")
        self.assertEqual(row[2], "Hazard is repaired and verified")
        json_core = await mcp_contract.call(
            "store_memory",
            {
                "title": "JSON-string core",
                "content": "A second temporary hazard needs bootstrap visibility before retrieval.",
                "core": '{"reason":"JSON hazard requires visibility","exit_condition":"JSON hazard is repaired"}',
            },
        )
        self.assertEqual(json_core["status"], "ok")
        before_unknown = self._counts()
        unknown = await mcp_contract.call(
            "store_memory",
            {
                "title": "Unknown nested field",
                "content": "A sufficiently descriptive memory body for validation.",
                "core": {"reason": "A" * 20, "exit_condition": "B" * 20, "extra": 1},
            },
        )
        self.assertEqual(unknown["errors"][0]["code"], "UNKNOWN_FIELD")
        self.assertEqual(unknown["errors"][0]["field"], "core.extra")
        self.assertEqual(self._counts(), before_unknown)

    async def test_metadata_null_deletes_shallow_key(self):
        created = await mcp_contract.call(
            "store_memory",
            {
                "title": "Metadata delete memory",
                "content": "A metadata deletion test record with nested values.",
                "metadata": {"remove": 1, "nested": {"keep": 1}},
            },
        )
        memory_id = created["data"]["memory_id"]
        updated = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": memory_id, "metadata": {"remove": None, "nested": {"x": None}}},
        )
        self.assertIn("deleted (1 key(s): remove)", updated["data"]["message"])
        fetched = await mcp_contract.call("get_memory", {"memory_id": memory_id})
        self.assertNotIn("remove", fetched["data"]["metadata"])
        self.assertEqual(fetched["data"]["metadata"]["nested"], {"x": None})

    async def test_trace_capture_records_store_but_not_metadata_update(self):
        with tools._PENDING_TRACE_WRITES_LOCK:
            tools._PENDING_TRACE_WRITES.clear()
        with patch("saltmdb.mcp.tools.is_trace_capture_enabled", return_value=True):
            stored = await mcp_contract.call(
                "store_memory",
                {"title": "Trace store memory", "content": "A trace-linked store record."},
            )
            memory_id = stored["data"]["memory_id"]
            updated = await mcp_contract.call(
                "update_memory_metadata",
                {"memory_id": memory_id, "metadata": {"trace": True}},
            )
        self.assertEqual(updated["status"], "ok")
        with tools._PENDING_TRACE_WRITES_LOCK:
            self.assertEqual(list(tools._PENDING_TRACE_WRITES), [("store_memory", memory_id)])
        with tools._PENDING_TRACE_WRITES_LOCK:
            tools._PENDING_TRACE_WRITES.clear()

    def _counts(self):
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("entities", "tags", "events")
        }

    def _row_state(self, memory_id):
        return self.connection.execute(
            "SELECT metadata, updated_at, last_touched_session_id FROM entities WHERE id = ?",
            (memory_id,),
        ).fetchone()

    async def test_store_content_source_rejections_are_side_effect_free(self):
        before = self._counts()
        for arguments, code in (
            (
                {"title": "Both", "content": "body", "content_file_path": "/missing"},
                "CONTENT_AND_FILE_PATH_BOTH_SET",
            ),
            ({"title": "Neither"}, "MISSING_CONTENT"),
        ):
            result = await mcp_contract.call("store_memory", arguments)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["errors"][0]["code"], code)
            self.assertEqual(self._counts(), before)

    async def test_store_path_duplicate_and_near_duplicate_contracts(self):
        before = self._counts()
        path_result = await mcp_contract.call(
            "store_memory",
            {"title": "Missing path", "content_file_path": "/definitely/missing/file"},
        )
        self.assertEqual(path_result["status"], "rejected")
        self.assertEqual(path_result["errors"][0]["code"], "CONTENT_FILE_READ_FAILED")
        self.assertEqual(self._counts(), before)
        first = await mcp_contract.call(
            "store_memory",
            {"title": "Exact duplicate", "content": "A unique exact duplicate body for S2."},
        )
        before_duplicate = self._counts()
        duplicate = await mcp_contract.call(
            "store_memory",
            {"title": "Exact duplicate", "content": "A unique exact duplicate body for S2."},
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(duplicate["status"], "rejected")
        self.assertEqual(self._counts(), before_duplicate)
        near = await mcp_contract.call(
            "store_memory",
            {
                "title": "Exact duplicate with extra",
                "content": "A unique exact duplicate body for S2 with extra wording.",
            },
        )
        self.assertEqual(near["status"], "ok")
        self.assertIn("duplicate_candidates", near["data"])
        self.assertIsInstance(near["data"]["duplicate_candidates"], list)
        self.assertEqual(near["warnings"][0]["code"], "NEAR_DUPLICATE")
        self.assertEqual(
            near["warnings"][0]["message"],
            "Memory stored; review duplicate_candidates and use supersede_memory for one replacement, consolidate_memories to merge several, or manage_relation to link them if related but not redundant.",
        )
        self.assertEqual(
            near["warnings"][0]["detail"]["duplicate_candidates"],
            near["data"]["duplicate_candidates"],
        )

    async def test_core_validation_literals_are_wrapped_without_fields(self):
        cases = (
            (
                {"reason": "x", "exit_condition": "B" * 20},
                "Error: core_reason must be 20-500 characters after redaction/normalization (got 1).",
            ),
            (
                {"reason": "r" * 501, "exit_condition": "B" * 20},
                "Error: core_reason must be 20-500 characters after redaction/normalization (got 501).",
            ),
            (
                {"reason": "A" * 20, "exit_condition": "x"},
                "Error: core_exit_condition must be 20-500 characters after redaction/normalization (got 1).",
            ),
            (
                {"reason": "A" * 20, "exit_condition": "e" * 501},
                "Error: core_exit_condition must be 20-500 characters after redaction/normalization (got 501).",
            ),
            (
                {"reason": "A" * 20, "exit_condition": "B" * 20, "review_after": "x"},
                "Error: core_review_after is not a valid ISO-8601 timestamp: 'x'",
            ),
            (
                {"reason": "A" * 20, "exit_condition": "B" * 20, "detail_memory_ids": ["x"]},
                "Error: detail_memory_ids must be full UUIDs, not prefixes or other references (got 'x').",
            ),
            (
                {
                    "reason": "A" * 20,
                    "exit_condition": "B" * 20,
                    "detail_memory_ids": ["00000000-0000-4000-8000-000000000000"] * 4,
                },
                "Error: A core memory may declare at most 3 detail_memory_ids (got 4).",
            ),
        )
        for index, (core, expected_message) in enumerate(cases):
            before = self._counts()
            result = await mcp_contract.call(
                "store_memory",
                {
                    "title": f"Core invalid {index}",
                    "content": f"Core validation body {index}.",
                    "core": core,
                },
            )
            self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
            self.assertNotIn("field", result["errors"][0])
            self.assertEqual(result["errors"][0]["message"], expected_message)
            self.assertEqual(self._counts(), before)

    async def test_store_removed_fields_and_json_core_contract(self):
        for field in ("entity_id", "is_core", "core_reason"):
            before = self._counts()
            result = await mcp_contract.call(
                "store_memory",
                {
                    "title": f"Removed {field}",
                    "content": "Removed field rejection body.",
                    field: True,
                },
            )
            self.assertEqual(result["errors"][0]["code"], "UNKNOWN_FIELD")
            self.assertEqual(result["errors"][0]["field"], field)
            self.assertEqual(self._counts(), before)
        accepted = await mcp_contract.call(
            "store_memory",
            {
                "title": "JSON core accepted",
                "content": "JSON core acceptance body with enough detail.",
                "core": '{"reason":"JSON core reason is sufficiently long","exit_condition":"JSON exit condition is sufficiently long"}',
            },
        )
        self.assertEqual(accepted["status"], "ok")
        self.assertIn("memory_id", accepted["data"])
        self.assertNotIn("id", accepted["data"])

    async def test_update_metadata_noops_messages_and_inert_core_keys(self):
        created = await mcp_contract.call(
            "store_memory",
            {
                "title": "Metadata matrix",
                "content": "Metadata matrix body.",
                "metadata": {"keep": 1},
            },
        )
        memory_id = created["data"]["memory_id"]
        before = self._counts()
        before_row = self._row_state(memory_id)
        absent = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": memory_id, "metadata": {"absent": None}},
        )
        self.assertEqual(
            absent["data"]["message"],
            f"Memory '{memory_id}' metadata unchanged (empty patch).",
        )
        self.assertEqual(self._counts(), before)
        self.assertEqual(self._row_state(memory_id), before_row)
        empty = await mcp_contract.call(
            "update_memory_metadata", {"memory_id": memory_id, "metadata": {}}
        )
        self.assertEqual(
            empty["data"]["message"],
            f"Memory '{memory_id}' metadata unchanged (empty patch).",
        )
        self.assertEqual(self._row_state(memory_id), before_row)
        inert = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": memory_id, "metadata": {"outcome": "archive", "core_review_after": "x"}},
        )
        self.assertEqual(
            inert["data"]["message"],
            f"Memory '{memory_id}' metadata updated (2 key(s): core_review_after, outcome).",
        )
        before_missing = self._counts()
        missing = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": "missing-memory", "metadata": {"x": 1}},
        )
        self.assertEqual(missing["errors"][0]["code"], "NOT_FOUND")
        self.assertEqual(self._counts(), before_missing)

    async def test_update_metadata_validation_and_merge_messages(self):
        created = await mcp_contract.call(
            "store_memory",
            {
                "title": "Metadata messages",
                "content": "Metadata message body.",
                "metadata": {"a": 1, "b": 2},
            },
        )
        memory_id = created["data"]["memory_id"]
        delete_only = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": memory_id, "metadata": {"a": None}},
        )
        self.assertEqual(
            delete_only["data"]["message"],
            f"Memory '{memory_id}' metadata deleted (1 key(s): a).",
        )
        both = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": memory_id, "metadata": {"b": None, "c": 3}},
        )
        self.assertEqual(
            both["data"]["message"],
            f"Memory '{memory_id}' metadata updated (1 key(s): c); deleted (1 key(s): b).",
        )
        before_wrong = self._counts()
        wrong_type = await mcp_contract.call(
            "update_memory_metadata", {"memory_id": memory_id, "metadata": []}
        )
        self.assertEqual(wrong_type["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(wrong_type["errors"][0]["field"], "metadata")
        self.assertEqual(self._counts(), before_wrong)

    async def test_retrieval_text_presence_and_omission(self):
        explicit = await mcp_contract.call(
            "store_memory",
            {
                "title": "Explicit retrieval text",
                "content": "Retrieval text explicit body.",
                "retrieval_text": "Explicit retrieval phrase",
            },
        )
        omitted = await mcp_contract.call(
            "store_memory",
            {"title": "Omitted retrieval text", "content": "Retrieval text omitted body."},
        )
        explicit_value = self.connection.execute(
            "SELECT retrieval_text FROM entities WHERE id = ?",
            (explicit["data"]["memory_id"],),
        ).fetchone()[0]
        omitted_value = self.connection.execute(
            "SELECT retrieval_text FROM entities WHERE id = ?",
            (omitted["data"]["memory_id"],),
        ).fetchone()[0]
        self.assertEqual(explicit_value, "Explicit retrieval phrase")
        self.assertIsNone(omitted_value)

    async def test_metadata_falsy_non_null_values_are_stored_and_updated(self):
        created = await mcp_contract.call(
            "store_memory",
            {"title": "Falsy metadata", "content": "Falsy metadata body."},
        )
        memory_id = created["data"]["memory_id"]
        result = await mcp_contract.call(
            "update_memory_metadata",
            {
                "memory_id": memory_id,
                "metadata": {"zero": 0, "false": False, "empty": "", "list": [], "dict": {}},
            },
        )
        self.assertEqual(
            result["data"]["message"],
            f"Memory '{memory_id}' metadata updated (5 key(s): dict, empty, false, list, zero).",
        )
        stored = self.connection.execute(
            "SELECT metadata FROM entities WHERE id = ?", (memory_id,)
        ).fetchone()[0]
        self.assertEqual(
            json.loads(stored),
            {"zero": 0, "false": False, "empty": "", "list": [], "dict": {}},
        )

    async def test_update_metadata_public_error_fields_are_memory_id(self):
        before = self._counts()
        missing = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": "missing-memory", "metadata": {"x": 1}},
        )
        self.assertEqual(missing["errors"][0]["code"], "NOT_FOUND")
        self.assertEqual(missing["errors"][0]["field"], "memory_id")
        invalid = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": "", "metadata": {}},
        )
        self.assertEqual(invalid["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(invalid["errors"][0]["field"], "memory_id")
        self.assertEqual(self._counts(), before)
        first = await mcp_contract.call(
            "store_memory",
            {"title": "Ambiguous A", "content": "Ambiguous prefix A body.", "tags": []},
        )
        second = await mcp_contract.call(
            "store_memory",
            {"title": "Ambiguous B", "content": "Ambiguous prefix B body.", "tags": []},
        )
        id_a = first["data"]["memory_id"]
        id_b = second["data"]["memory_id"]
        prefix = id_a[:8]
        forced = prefix + id_b[8:]
        self.connection.execute("UPDATE entities SET id = ? WHERE id = ?", (forced, id_b))
        self.connection.commit()
        before_ambiguous = self._counts()
        ambiguous = await mcp_contract.call(
            "update_memory_metadata",
            {"memory_id": prefix, "metadata": {"x": 1}},
        )
        self.assertEqual(ambiguous["errors"][0]["code"], "AMBIGUOUS_ID_PREFIX")
        self.assertEqual(ambiguous["errors"][0]["field"], "memory_id")
        self.assertGreaterEqual(len(ambiguous["errors"][0]["candidates"]), 2)
        self.assertEqual(self._counts(), before_ambiguous)

    async def test_update_private_memory_from_another_agent_is_not_found(self):
        private = await mcp_contract.call(
            "store_memory",
            {
                "title": "Private metadata memory",
                "content": "Private ownership body.",
                "scope": "private",
            },
        )
        memory_id = private["data"]["memory_id"]
        before = self._counts()
        try:
            SESSION_IDENTITY.reset()
            SESSION_IDENTITY.configure_agent_id("different-agent")
            result = await mcp_contract.call(
                "update_memory_metadata",
                {"memory_id": memory_id, "metadata": {"x": 1}},
            )
        finally:
            SESSION_IDENTITY.reset()
            SESSION_IDENTITY.configure_agent_id("guard-test-agent")
        self.assertEqual(result["errors"][0]["code"], "NOT_FOUND")
        with tools._PENDING_TRACE_WRITES_LOCK:
            tools._PENDING_TRACE_WRITES.clear()
        self.assertEqual(self._counts(), before)

    async def test_merge_tags_bounds_and_missing_canonical_contract(self):
        for arguments, field in (
            ({"keep_tag": "#missing", "tags_to_merge": ["#source"]}, None),
            ({"keep_tag": "#missing", "tags_to_merge": ["#x"] * 51}, "tags_to_merge"),
        ):
            before = self._counts()
            result = await mcp_contract.call("merge_tags", arguments)
            self.assertEqual(result["status"], "rejected")
            if field is None:
                self.assertEqual(result["errors"][0]["code"], "NOT_FOUND")
                self.assertEqual(result["errors"][0]["field"], "keep_tag")
                self.assertEqual(
                    result["errors"][0]["message"],
                    "keep_tag '#missing' does not exist in the tags table.",
                )
            else:
                self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
                self.assertEqual(result["errors"][0]["field"], field)
                self.assertEqual(
                    result["errors"][0]["message"],
                    "List should have at most 50 items after validation, not 51",
                )
            self.assertEqual(self._counts(), before)

    async def test_merge_tags_collision_and_repeat_are_idempotent(self):
        await mcp_contract.call(
            "store_memory",
            {
                "title": "Canonical tag memory",
                "content": "Canonical tag collision body.",
                "tags": ["#canonical-v2"],
            },
        )
        await mcp_contract.call(
            "store_memory",
            {
                "title": "Source tag memory",
                "content": "Source tag collision body.",
                "tags": ["#source-v2"],
            },
        )
        first = await mcp_contract.call(
            "merge_tags",
            {"keep_tag": "#canonical-v2", "tags_to_merge": ["#source-v2"]},
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["data"]["merged"], ["#source-v2"])
        before_repeat = self._counts()
        second = await mcp_contract.call(
            "merge_tags",
            {"keep_tag": "#canonical-v2", "tags_to_merge": ["#source-v2"]},
        )
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["data"]["merged"], [])
        self.assertEqual(len(second["data"]["skipped"]), 1)
        self.assertEqual(self._counts(), before_repeat)

    async def test_merge_tags_requires_nonempty_list(self):
        before = self._counts()
        result = await mcp_contract.call(
            "merge_tags", {"keep_tag": "#missing", "tags_to_merge": []}
        )
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(result["errors"][0]["field"], "tags_to_merge")
        self.assertEqual(
            result["errors"][0]["message"],
            "List should have at least 1 item after validation, not 0",
        )
        self.assertEqual(self._counts(), before)
        before = self._counts()
        result = await mcp_contract.call(
            "merge_tags", {"keep_tag": "#missing", "tags_to_merge": "#x"}
        )
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(result["errors"][0]["field"], "tags_to_merge")
        self.assertEqual(result["errors"][0]["message"], "Input should be a valid list")
        self.assertEqual(self._counts(), before)


class TestWriteResultCharacterization(unittest.TestCase):
    def setUp(self):
        tools._drain_pending_trace_writes()
        self.addCleanup(tools._drain_pending_trace_writes)

    def test_non_dict_results_are_unchanged_and_never_recorded(self):
        for response in (None, [], [{"status": "ok", "data": {"id": "not-a-write"}}]):
            with (
                self.subTest(response=response),
                patch("saltmdb.mcp.tools._backend_or_raise") as backend,
                patch("saltmdb.mcp.tools.is_trace_capture_enabled", return_value=True),
            ):
                backend.return_value.call.return_value = response
                self.assertIs(tools._call_and_record_write("store_memory", {}), response)
                self.assertEqual(tools._drain_pending_trace_writes(), [])

    def test_missing_or_false_written_ids_never_record_a_trace(self):
        for data in (None, [], "invalid", {}, {"id": None}, {"id": ""}):
            with (
                self.subTest(data=data),
                patch("saltmdb.mcp.tools._backend_or_raise") as backend,
                patch("saltmdb.mcp.tools.is_trace_capture_enabled", return_value=True),
            ):
                backend.return_value.call.return_value = {"status": "ok", "data": data}
                result = tools._call_and_record_write("store_memory", {})
                self.assertEqual(result["status"], "ok")
                self.assertEqual(tools._drain_pending_trace_writes(), [])
                if isinstance(data, dict) and "id" in data:
                    self.assertEqual(result["data"], {"memory_id": data["id"]})
                else:
                    self.assertEqual(result["data"], data)

    def test_error_remap_is_independent_of_status_and_preserves_other_entries(self):
        for status in ("ok", "rejected"):
            original = {
                "status": status,
                "data": {"id": "metadata-record"},
                "errors": [
                    {"field": "entity_id", "code": "NOT_FOUND"},
                    {"field": "metadata", "code": "VALIDATION_ERROR"},
                    "legacy error",
                    None,
                ],
            }
            with (
                self.subTest(status=status),
                patch("saltmdb.mcp.tools._backend_or_raise") as backend,
                patch("saltmdb.mcp.tools.is_trace_capture_enabled") as capture,
            ):
                backend.return_value.call.return_value = original
                result = tools._call_and_record_write("update_memory_metadata", {})
                self.assertEqual(
                    result["errors"],
                    [
                        {"field": "memory_id", "code": "NOT_FOUND"},
                        {"field": "metadata", "code": "VALIDATION_ERROR"},
                        "legacy error",
                        None,
                    ],
                )
                original_errors = cast(list[object], original["errors"])
                self.assertEqual(cast(dict[str, object], original_errors[0])["field"], "entity_id")
                self.assertEqual(original["data"], {"id": "metadata-record"})
                self.assertEqual(
                    result["data"],
                    {"memory_id": "metadata-record"} if status == "ok" else original["data"],
                )
                capture.assert_not_called()

    def test_other_tool_keeps_internal_fields_and_does_not_read_capture_flag(self):
        response = {
            "status": "ok",
            "data": {"id": "unrelated-record"},
            "errors": [{"field": "entity_id", "code": "NOT_FOUND"}],
        }
        with (
            patch("saltmdb.mcp.tools._backend_or_raise") as backend,
            patch("saltmdb.mcp.tools.is_trace_capture_enabled") as capture,
        ):
            backend.return_value.call.return_value = response
            result = tools._call_and_record_write("merge_tags", {})
        self.assertEqual(
            result,
            {
                "status": "ok",
                "data": {"id": "unrelated-record"},
                "errors": [{"field": "entity_id", "code": "NOT_FOUND"}],
            },
        )
        self.assertEqual(tools._drain_pending_trace_writes(), [])
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
