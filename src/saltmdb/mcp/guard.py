"""Public MCP call guard for strict argument and failure envelopes."""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent, Tool as MCPTool
from pydantic import ValidationError

from saltmdb.utils.corrected_call import build_corrected_call
from saltmdb.utils.envelope import error, rejected

logger = logging.getLogger(__name__)

RENAMED_FIELDS: dict[str, dict[str, str]] = {
    "search_memory": {"query_keywords": "query"},
    "revise_memory": {"entity_id": "memory_id"},
    "supersede_memory": {"entity_id": "memory_id"},
    "review_core_memory": {"entity_id": "memory_id"},
    "get_memory": {"entity_id": "memory_id"},
    "get_lineage": {"entity_id": "memory_id"},
    "get_related_memories": {
        "entity_id": "memory_id",
        "include_inspect": "include_summary",
    },
    "retrieve_context": {"entity_ids": "memory_ids"},
}

REMOVED_TOOLS: dict[str, str] = {
    "inspect_memory": 'inspect_memory was merged into get_memory(memory_id, detail="summary").',
}


class GuardedFastMCP(FastMCP):
    """FastMCP server that normalizes caller validation and internal failures."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._guard_schemas: dict[str, MCPTool] | None = None

    async def _schemas(self) -> dict[str, MCPTool]:
        if self._guard_schemas is None:
            self._guard_schemas = {tool.name: tool for tool in await self.list_tools()}
        return self._guard_schemas

    @staticmethod
    def _field_path(location: tuple[Any, ...]) -> str:
        field = ""
        for part in location:
            if isinstance(part, int):
                field += f"[{part}]"
            else:
                field = f"{field}.{part}" if field else str(part)
        return field

    @staticmethod
    def _has_result_wrapper(schema: dict[str, Any] | None) -> bool:
        if not schema or schema.get("type") != "object":
            return False
        properties = schema.get("properties", {})
        return set(properties) == {"result"} and schema.get("required") == ["result"]

    @staticmethod
    def _result(envelope: dict[str, Any], schema: MCPTool | None) -> Any:
        content = [TextContent(type="text", text=json.dumps(envelope, indent=2))]
        if schema is not None and GuardedFastMCP._has_result_wrapper(schema.outputSchema):
            return content, {"result": envelope}
        return content

    @staticmethod
    def _safe_internal_error() -> ToolError:
        envelope = rejected([error("INTERNAL_ERROR", "Tool execution failed unexpectedly.")])
        return ToolError(json.dumps(envelope))

    def _unknown_tool(self, name: str, schemas: dict[str, MCPTool]) -> Any:
        if name in REMOVED_TOOLS:
            envelope = rejected([error("REMOVED_TOOL", REMOVED_TOOLS[name], "tool")])
        else:
            valid_names = ", ".join(sorted(schemas))
            envelope = rejected(
                [
                    error(
                        "UNKNOWN_TOOL",
                        f"Unknown tool '{name}'. Valid tools: {valid_names}.",
                        "tool",
                    )
                ]
            )
        return self._result(envelope, None)

    def _unknown_fields(
        self, name: str, schema: MCPTool, submitted: dict[str, Any], properties: list[str]
    ) -> Any:
        tool_func = getattr(importlib.import_module("saltmdb.mcp.tools"), name)
        renamed = RENAMED_FIELDS.get(name, {})
        fixes = {
            new: submitted[old]
            for old, new in renamed.items()
            if old in submitted and new not in submitted
        }
        corrected = build_corrected_call(tool_func, submitted, fixes)
        valid_fields = ", ".join(properties)
        errors = []
        for key in submitted:
            if key in properties:
                continue
            message = f"Unknown field '{key}'; valid fields: {valid_fields}."
            if name == "retrieve_context" and key in {"query", "strategy"}:
                message += (
                    " This tool takes memory_ids anchors only; find them with search_memory first."
                )
            elif key in renamed:
                message += f" It was renamed to '{renamed[key]}'."
            errors.append(error("UNKNOWN_FIELD", message, key, corrected_call=corrected))
        return self._result(rejected(errors), schema)

    def _validation_result(self, schema: MCPTool, cause: ValidationError) -> Any:
        errors = []
        for detail in cause.errors(include_url=False):
            location = tuple(detail.get("loc", ()))
            code = (
                "UNKNOWN_FIELD" if detail.get("type") == "extra_forbidden" else "VALIDATION_ERROR"
            )
            errors.append(
                error(
                    code, detail.get("msg", "Input validation failed"), self._field_path(location)
                )
            )
        return self._result(rejected(errors), schema)

    @staticmethod
    def _is_input_validation(name: str, cause: BaseException | None) -> bool:
        return isinstance(cause, ValidationError) and cause.title == f"{name}Arguments"

    def _internal_failure(self, name: str, _exc: BaseException) -> ToolError:
        logger.exception("Unhandled MCP tool failure for %s", name)
        return self._safe_internal_error()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> Any:
        schemas = await self._schemas()
        schema = schemas.get(name)
        if schema is None:
            return self._unknown_tool(name, schemas)

        submitted = arguments or {}
        properties = list(schema.inputSchema.get("properties", {}))
        if any(key not in properties for key in submitted):
            return self._unknown_fields(name, schema, submitted, properties)
        try:
            return await super().call_tool(name, submitted)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError) and self._is_input_validation(name, cause):
                return self._validation_result(schema, cause)
            raise self._internal_failure(name, exc) from exc
        except Exception as exc:
            raise self._internal_failure(name, exc) from exc
