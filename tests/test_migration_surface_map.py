from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from saltmdb.mcp.guard import REMOVED_TOOLS
from saltmdb.mcp.public_shapes import (
    context_data,
    memory_record,
    page,
    related_data,
    trace_preview,
    trace_record,
)
from saltmdb.mcp.server import mcp

MIGRATION = Path(__file__).resolve().parents[1] / "MIGRATION.md"
SECTION_HEADING = "## Live cutover: MCP tool surface v2"


def _section() -> str:
    text = MIGRATION.read_text()
    assert SECTION_HEADING in text
    return text.split(SECTION_HEADING, 1)[1]


def _table(section: str, heading: str, next_heading: str) -> list[str]:
    body = section.split(heading, 1)[1].split(next_heading, 1)[0]
    return [line for line in body.splitlines() if line.startswith("|") and "---" not in line]


def _schema_properties(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        schema = root.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})
    return schema.get("properties", {})


def test_section_and_cutover_steps_are_ordered():
    section = _section()
    positions = [section.index(f"{number}. ") for number in range(1, 8)]
    assert positions == sorted(positions)
    assert "capture_trace_start" in section
    assert "schema unchanged" in section
    assert "diff and merge" in section.lower()
    assert "render procedure" in section.lower()


def test_tool_map_uses_registered_names_and_removed_tool_registry():
    section = _section()
    registered = {tool.name for tool in asyncio.run(mcp.list_tools())}
    tool_rows = _table(section, "### Tool map", "### Parameter map")
    removed = set()
    for row in tool_rows:
        if "(removed)" in row:
            match = re.search(r"\| `([a-z][a-z0-9_]*)` \(removed\)", row)
            assert match
            removed.add(match.group(1))
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        new_tools = re.findall(r"`([a-z][a-z0-9_]*)`", cells[1])
        assert set(new_tools) <= registered
    assert removed == set(REMOVED_TOOLS)


def test_parameter_map_names_match_live_input_schemas():
    section = _section()
    schemas = {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}
    rows = _table(section, "### Parameter map", "### Response and behavior changes")
    for row in rows:
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        if len(cells) < 2 or cells[0].startswith(":") or cells[0] == "Old parameter or form":
            continue
        nested = re.search(
            r"`([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\[\]\..*?([a-z][a-z0-9_]*)`",
            cells[1],
        )
        if nested:
            tool, outer, inner = nested.groups()
            properties = _schema_properties(schemas[tool], schemas[tool])
            assert outer in properties
            item_properties = _schema_properties(properties[outer].get("items", {}), schemas[tool])
            assert inner in item_properties
            continue
        match = re.search(r"`([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)", cells[1])
        assert match, row
        tool, field = match.groups()
        assert tool in schemas
        assert field in schemas[tool].get("properties", {})


def test_read_reference_lengths_match_migration_row():
    section = _section()
    schemas = {
        tool.name: tool.inputSchema.get("properties", {}) for tool in asyncio.run(mcp.list_tools())
    }
    assert schemas["get_memory"]["memory_id"]["maxLength"] == 200
    assert schemas["get_memories"]["memory_ids"]["items"]["maxLength"] == 200
    assert schemas["get_lineage"]["memory_id"]["maxLength"] == 200
    assert schemas["get_related_memories"]["memory_id"]["maxLength"] == 200
    assert schemas["retrieve_context"]["memory_ids"]["items"]["maxLength"] == 200
    row = next(line for line in section.splitlines() if "One generic id bound" in line)
    assert "relations" not in row
    for reference in (
        "get_memories.memory_ids[]",
        "get_memory.memory_id",
        "get_lineage.memory_id",
        "get_related_memories.memory_id",
        "retrieve_context.memory_ids[]",
    ):
        assert reference in row


def test_response_rows_pin_real_public_shape_keys():
    section = _section()
    memory = memory_record({"id": "m", "parent_ids": ["p"]})
    context = context_data({"entity_ids": ["m"]})
    related = related_data({"entity_id": "m", "total_related_found": 1})
    _ = trace_record({"id": "t", "trace_memory_links": [{"entity_id": "m"}]})
    trace_item = trace_preview({"linked_entity_ids": ["m"]})
    paging = page(["first", "second"], 1, 0)
    expected_rows = (
        ("data.id", "data.memory_id", "memory_id" in memory),
        ("data.old_id", "data.predecessor_memory_id", True),
        ("data.new_id", "data.successor_memory_id", True),
        ("data.results", "data.items", "items" in paging),
        ("entity_ids", "memory_ids", "memory_ids" in context),
        ("linked_entity_ids", "linked_memory_ids", "linked_memory_ids" in trace_item),
        ("total_related_found", "total_edges", "total_edges" in related),
        ("has_more", "next_cursor", "has_more" in paging and "next_cursor" in paging),
    )
    response_section = section.split("### Response and behavior changes", 1)[1]
    for old_name, new_name, produced in expected_rows:
        assert produced
        assert any(
            old_name in row and new_name in row
            for row in response_section.splitlines()
            if row.startswith("|")
        )
