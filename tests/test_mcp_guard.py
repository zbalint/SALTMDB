from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import types
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from saltmdb.mcp import tools
from saltmdb.mcp.guard import GuardedFastMCP, REMOVED_TOOLS
from saltmdb.utils import envelope

import mcp_contract


_SKIP_REASONS = {
    "required": {
        "search_tags": "Published schema has no required field.",
        "list_predicates": "Published schema has no required field.",
        "merge_tags": "Published schema has no required field.",
        "search_memory": "Published schema has no required field.",
        "consolidate_memories": "Published schema has no required field.",
        "get_memory": "Published schema has no required field.",
        "search_traces": "Published schema has no required field.",
        "retrieve_context": "Published schema has no required field.",
        "get_events": "Published schema has no required field.",
        "review_core_memory": "Published schema has no required field.",
    },
    "typed": {},
    "nullable": {
        "search_tags": "Every published parameter accepts null.",
        "list_predicates": "Every published parameter accepts null.",
        "merge_tags": "Every published parameter accepts null.",
        "search_memory": "Every published parameter accepts null.",
        "search_traces": "Every published parameter accepts null.",
        "retrieve_context": "Every published parameter accepts null.",
        "review_core_memory": "Every published parameter accepts null.",
    },
}


def _branches(schema):
    return schema.get("anyOf", [schema])


def _wrong_value(schema):
    for branch in _branches(schema):
        if branch.get("type") == "null":
            continue
        value_type = branch.get("type")
        if value_type == "string":
            return 0
        if value_type in {"integer", "number"}:
            return "wrong"
        if value_type == "boolean":
            return "wrong"
        if value_type == "array":
            return {}
        if value_type == "object":
            return []
    return None


def _is_nullable(schema):
    return any(branch.get("type") == "null" for branch in _branches(schema))


class _NestedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class _RaisingBackend:
    def call(self, _tool_name, _kwargs):
        raise RuntimeError("backend detail must not escape")


class TestMCPGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection, self.previous_backend, self.previous_db_path = mcp_contract.configure(
            Path(self.temp_dir.name) / "guard.db"
        )

    def tearDown(self):
        mcp_contract.cleanup(self.connection, self.previous_backend, self.previous_db_path)
        self.temp_dir.cleanup()

    async def test_unknown_field_is_rejected_for_every_registered_tool_without_side_effects(self):
        before = (
            self.connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        for name in mcp_contract.tool_names():
            with self.subTest(tool=name):
                properties = await mcp_contract.published_properties(name)
                result = await mcp_contract.call(name, {"__unknown_guard_field__": 1})
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["errors"][0]["code"], "UNKNOWN_FIELD")
                self.assertEqual(result["errors"][0]["field"], "__unknown_guard_field__")
                self.assertIn(", ".join(properties), result["errors"][0]["message"])
                self.assertIn("corrected_call", result["errors"][0])
        after = (
            self.connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        self.assertEqual(after, before)

    async def test_unknown_tool_lists_registered_names(self):
        result = await mcp_contract.call("not_registered", {})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "UNKNOWN_TOOL")
        for name in mcp_contract.tool_names():
            self.assertIn(name, result["errors"][0]["message"])

    async def test_removed_tool_uses_replacement_sentence(self):
        with patch.dict(REMOVED_TOOLS, {"old_tool": "Use replacement_tool instead."}):
            result = await mcp_contract.call("old_tool", {})
        self.assertEqual(result["errors"][0]["code"], "REMOVED_TOOL")
        self.assertEqual(result["errors"][0]["field"], "tool")
        self.assertIn("replacement_tool", result["errors"][0]["message"])

    async def test_caller_validation_errors_are_normal_rejections(self):
        result = await mcp_contract.call("store_memory", {"title": 7, "tags": "wrong"})
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            [(item["code"], item["field"]) for item in result["errors"]],
            [("VALIDATION_ERROR", "title"), ("VALIDATION_ERROR", "tags")],
        )
        wrong_type = await mcp_contract.call("search_memory", {"query_keywords": ["wrong"]})
        self.assertEqual(wrong_type["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(wrong_type["errors"][0]["field"], "query_keywords")

    async def test_missing_required_field_is_rejected(self):
        result = await mcp_contract.call("store_memory", {})
        self.assertEqual(result["errors"][0]["code"], "VALIDATION_ERROR")
        self.assertEqual(result["errors"][0]["field"], "title")

    def _database_snapshot(self):
        return tuple(self.connection.iterdump())

    async def _published_schemas(self):
        return {tool.name: tool.inputSchema for tool in await mcp_contract.mcp.list_tools()}

    async def test_schema_driven_missing_required_fields_for_every_tool(self):
        schemas = await self._published_schemas()
        for name in mcp_contract.tool_names():
            with self.subTest(tool=name):
                required = schemas[name].get("required", [])
                if not required:
                    self.assertIn(name, _SKIP_REASONS["required"], "skip reason required")
                    continue
                before = self._database_snapshot()
                result = await mcp_contract.call_protocol(name, {})
                after = self._database_snapshot()
                payload = json.loads(result.content[0].text)
                self.assertFalse(result.isError)
                self.assertEqual(payload["status"], "rejected")
                self.assertTrue(
                    set(required).issubset({item["field"] for item in payload["errors"]}),
                    payload,
                )
                self.assertEqual(after, before)

    async def test_schema_driven_wrong_type_for_every_tool(self):
        schemas = await self._published_schemas()
        for name in mcp_contract.tool_names():
            with self.subTest(tool=name):
                typed = next(
                    (
                        (field, _wrong_value(schema))
                        for field, schema in schemas[name].get("properties", {}).items()
                        if _wrong_value(schema) is not None
                    ),
                    None,
                )
                if typed is None:
                    self.assertIn(name, _SKIP_REASONS["typed"], "skip reason required")
                    continue
                field, wrong_value = typed
                before = self._database_snapshot()
                result = await mcp_contract.call_protocol(name, {field: wrong_value})
                after = self._database_snapshot()
                payload = json.loads(result.content[0].text)
                self.assertFalse(result.isError)
                self.assertEqual(payload["errors"][0]["code"], "VALIDATION_ERROR")
                self.assertIn(
                    payload["errors"][0]["field"],
                    (field, f"{field}.str", f"{field}.int", f"{field}.float"),
                )
                self.assertEqual(after, before)

    async def test_schema_driven_explicit_null_for_every_non_nullable_parameter(self):
        schemas = await self._published_schemas()
        for name in mcp_contract.tool_names():
            with self.subTest(tool=name):
                field = next(
                    (
                        field
                        for field, schema in schemas[name].get("properties", {}).items()
                        if not _is_nullable(schema)
                    ),
                    None,
                )
                if field is None:
                    self.assertIn(name, _SKIP_REASONS["nullable"], "skip reason required")
                    continue
                before = self._database_snapshot()
                result = await mcp_contract.call_protocol(name, {field: None})
                after = self._database_snapshot()
                payload = json.loads(result.content[0].text)
                self.assertFalse(result.isError)
                self.assertEqual(payload["errors"][0]["code"], "VALIDATION_ERROR")
                self.assertEqual(payload["errors"][0]["field"], field)
                self.assertEqual(after, before)

    async def test_nested_extra_forbidden_maps_to_unknown_field(self):
        server = GuardedFastMCP("nested-validation")

        @server.tool()
        def nested_tool(items: list[_NestedItem]) -> dict:
            return {"items": len(items)}

        result = await server.call_tool(
            "nested_tool",
            {"items": [{"name": "valid", "bogus": True}]},
        )
        payload = json.loads(result[0].text)
        self.assertEqual(payload["errors"][0]["code"], "UNKNOWN_FIELD")
        self.assertEqual(payload["errors"][0]["field"], "items[0].bogus")

    def test_ok_rejects_corrected_call_keyword(self):
        with self.assertRaises(TypeError):
            getattr(envelope, "ok")({"id": "x"}, corrected_call={"id": "y"})

    async def test_unknown_field_corrected_call_preserves_valid_values(self):
        result = await mcp_contract.call(
            "store_memory",
            {"title": "T", "content": "C", "tags": ["#a"], "bogus": 1},
        )
        self.assertEqual(
            result["errors"][0]["corrected_call"],
            {"title": "T", "content": "C", "tags": ["#a"]},
        )

    async def test_multiple_unknown_fields_share_corrected_call_in_submission_order(self):
        result = await mcp_contract.call(
            "store_memory",
            {
                "title": "T",
                "content": "C",
                "tags": ["#a"],
                "first_unknown": 1,
                "second_unknown": 2,
            },
        )
        self.assertEqual(
            [item["field"] for item in result["errors"]],
            ["first_unknown", "second_unknown"],
        )
        expected = {"title": "T", "content": "C", "tags": ["#a"]}
        self.assertEqual(
            [item["corrected_call"] for item in result["errors"]],
            [expected, expected],
        )

    async def test_output_schema_rejections_pass_lowlevel_validation(self):
        for name, arguments in (
            ("store_memory", {"__unknown_guard_field__": 1}),
            ("revise_memory", {"__unknown_guard_field__": 1}),
            ("consolidate_memories", {"__unknown_guard_field__": 1}),
        ):
            with self.subTest(tool=name):
                result = await mcp_contract.call_protocol(name, arguments)
                self.assertFalse(result.isError)
                self.assertEqual(json.loads(result.content[0].text)["status"], "rejected")

    async def test_internal_backend_failure_is_protocol_error_without_traceback(self):
        previous = tools._set_backend_for_test(_RaisingBackend())
        try:
            result = await mcp_contract.call_protocol("list_predicates", {})
        finally:
            tools._set_backend_for_test(previous)
        self.assertTrue(result.isError)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["errors"][0]["code"], "INTERNAL_ERROR")
        self.assertNotIn("backend detail", result.content[0].text)
        self.assertNotIn("Traceback", result.content[0].text)

    async def test_body_validation_error_is_internal_error(self):
        server = GuardedFastMCP("body-validation")

        class BodyModel(BaseModel):
            required: str

        @server.tool()
        def body_validation() -> dict:
            BodyModel.model_validate({})
            return {"status": "ok"}

        with self.assertRaises(ToolError) as caught:
            await server.call_tool("body_validation", {})
        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["errors"][0]["code"], "INTERNAL_ERROR")

    async def test_guard_result_matches_fastmcp_conversion_for_plain_and_wrapped_tools(self):
        for name in ("list_predicates", "consolidate_memories"):
            tool = mcp_contract.mcp._tool_manager.get_tool(name)
            properties = await mcp_contract.published_properties(name)
            envelope = {
                "status": "rejected",
                "errors": [
                    {
                        "code": "UNKNOWN_FIELD",
                        "message": (f"Unknown field 'bad'; valid fields: {', '.join(properties)}."),
                        "field": "bad",
                        "corrected_call": {},
                    }
                ],
                "warnings": [],
            }
            expected = tool.fn_metadata.convert_result(envelope)
            actual = await mcp_contract.mcp.call_tool(name, {"bad": 1})
            self.assertEqual(actual, expected)
            self.assertEqual(isinstance(actual, tuple), name == "consolidate_memories")

    async def test_none_arguments_are_treated_as_empty(self):
        result = await mcp_contract.call("store_memory", None)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["field"], "title")

    async def test_schema_cache_is_lazy_and_override_is_bound_to_lowlevel_handler(self):
        server = GuardedFastMCP("lazy")
        seen = []

        @server.tool()
        def late_tool() -> dict:
            seen.append(True)
            return {"status": "ok"}

        handler = server._mcp_server.request_handlers[types.CallToolRequest]
        response = await handler(
            types.CallToolRequest(
                params=types.CallToolRequestParams(name="late_tool", arguments={})
            )
        )
        self.assertFalse(response.root.isError)
        self.assertEqual(seen, [True])

        class SpyServer(GuardedFastMCP):
            def __init__(self):
                self.calls = 0
                super().__init__("spy")

            async def call_tool(self, name, arguments):
                self.calls += 1
                return await super().call_tool(name, arguments)

        spy = SpyServer()

        @spy.tool()
        def spy_tool() -> dict:
            return {"status": "ok"}

        spy_handler = spy._mcp_server.request_handlers[types.CallToolRequest]
        await spy_handler(
            types.CallToolRequest(params=types.CallToolRequestParams(name="spy_tool", arguments={}))
        )
        self.assertEqual(spy.calls, 1)

    async def test_inventory_matches_registered_tools(self):
        self.assertEqual(
            set(mcp_contract.tool_names()), set(await mcp_contract.registered_tool_names())
        )


if __name__ == "__main__":
    unittest.main()
