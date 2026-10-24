"""Live MCP contract helpers for the guarded server tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp import types
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY
from saltmdb.mcp.server import mcp
from saltmdb.domain.services import librarian_service

TOOL_NAMES = (
    "log_event",
    "search_tags",
    "list_predicates",
    "merge_tags",
    "store_memory",
    "search_memory",
    "archive_memory",
    "manage_relation",
    "consolidate_memories",
    "revise_memory",
    "supersede_memory",
    "get_memory",
    "capture_trace_start",
    "capture_trace_memory_link",
    "capture_trace_complete",
    "search_traces",
    "get_trace",
    "get_memories",
    "get_lineage",
    "get_related_memories",
    "retrieve_context",
    "get_events",
    "set_core_memory",
    "review_core_memory",
    "update_memory_metadata",
)


def configure(db_path: Path, agent_id: str = "guard-test-agent") -> tuple[Any, Any, str | None]:
    """Configure the global server's direct backend against a temporary database."""
    connection = init_db(str(db_path))
    previous_db_path = os.environ.get("SALTMDB_DB_PATH")
    os.environ["SALTMDB_DB_PATH"] = str(db_path)
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id(agent_id)
    previous_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())
    return connection, previous_backend, previous_db_path


def drain_librarian_pool(timeout: float = 30.0) -> None:
    """Wait until all queued librarian work has completed."""
    librarian_service._librarian_trigger_pool.submit(lambda: None).result(timeout=timeout)


def cleanup(connection: Any, previous_backend: Any, previous_db_path: str | None) -> None:
    tools._set_backend_for_test(previous_backend)
    SESSION_IDENTITY.reset()
    if previous_db_path is None:
        os.environ.pop("SALTMDB_DB_PATH", None)
    else:
        os.environ["SALTMDB_DB_PATH"] = previous_db_path
    connection.close()
    drain_librarian_pool()


async def call(name: str, arguments: dict[str, Any] | None) -> Any:
    """Call a public tool and parse its first text content as JSON."""
    result = await mcp.call_tool(name, arguments)
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


async def call_protocol(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
    """Call a tool through the low-level request handler and return its MCP result."""
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    response = await handler(
        types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
    )
    return response.root


def tool_names() -> tuple[str, ...]:
    return TOOL_NAMES


async def registered_tool_names() -> tuple[str, ...]:
    return tuple(tool.name for tool in await mcp.list_tools())


async def published_properties(name: str) -> tuple[str, ...]:
    for tool in await mcp.list_tools():
        if tool.name == name:
            return tuple(tool.inputSchema.get("properties", {}))
    raise AssertionError(f"unknown registered tool {name}")
