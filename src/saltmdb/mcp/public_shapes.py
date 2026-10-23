"""Pure conversion helpers for the public MCP memory graph shapes."""

from __future__ import annotations

from typing import Any


def lineage_node(node: dict[str, Any]) -> dict[str, Any]:
    """Rename one lineage node without mutating the service result."""
    result = dict(node)
    if "id" in result:
        result["memory_id"] = result.pop("id")
    return result


def lineage_view(view: dict[str, Any]) -> dict[str, Any]:
    """Rename nodes in an embedded memory lineage view."""
    result = dict(view)
    for direction in ("ancestors", "descendants"):
        nodes = result.get(direction)
        if isinstance(nodes, list):
            result[direction] = [
                lineage_node(node) if isinstance(node, dict) else node for node in nodes
            ]
    return result


def memory_record(data: dict[str, Any]) -> dict[str, Any]:
    """Rename a memory record and its nested lineage without changing input."""
    result = dict(data)
    if "id" in result:
        result["memory_id"] = result.pop("id")
    if "parent_ids" in result:
        result["parent_memory_ids"] = result.pop("parent_ids")
    lineage = result.get("lineage")
    if isinstance(lineage, dict):
        result["lineage"] = lineage_view(lineage)
    return result


def lineage_data(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the data payload returned by the lineage service."""
    result = dict(data)
    if "entity_id" in result:
        result["memory_id"] = result.pop("entity_id")
    if "total" in result:
        result["total_edges"] = result.pop("total")
    if isinstance(result.get("root"), dict):
        result["root"] = lineage_node(result["root"])
    if isinstance(result.get("nodes"), list):
        result["nodes"] = [
            lineage_node(node) if isinstance(node, dict) else node for node in result["nodes"]
        ]
    return result


def related_data(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the data payload returned by the related-memory service."""
    result = dict(data)
    if "entity_id" in result:
        result["memory_id"] = result.pop("entity_id")
    if "total_dependencies_found" in result:
        result["total_edges"] = result.pop("total_dependencies_found")
    if "total_related_found" in result:
        result["total_edges"] = result.pop("total_related_found")
    if isinstance(result.get("root"), dict):
        result["root"] = memory_record(result["root"])
    nodes = result.get("related_memories")
    if isinstance(nodes, list):
        result["related_memories"] = [
            memory_record(node) if isinstance(node, dict) else node for node in nodes
        ]
    return result


def _context_provenance_entry(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    entry = dict(item)
    for old_key, new_key in (
        ("origin_hit_id", "origin_memory_id"),
        ("orphan_entity_id", "orphan_memory_id"),
    ):
        if old_key in entry:
            entry[new_key] = entry.pop(old_key)
    return entry


def _context_memory(memory: dict[str, Any]) -> dict[str, Any]:
    result = dict(memory)
    if "entity_id" in result:
        result["memory_id"] = result.pop("entity_id")
    provenance = result.get("retrieval_provenance")
    if isinstance(provenance, list):
        result["retrieval_provenance"] = [_context_provenance_entry(item) for item in provenance]
    return result


def _context_conflict_set(conflict_set: Any) -> Any:
    if not isinstance(conflict_set, dict):
        return conflict_set
    result = dict(conflict_set)
    members = result.get("members")
    if isinstance(members, list):
        result["members"] = [
            lineage_node(member) if isinstance(member, dict) else member for member in members
        ]
    return result


def _context_lineage_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = dict(entry)
    current = result.get("current")
    if isinstance(current, dict):
        result["current"] = lineage_node(current)
    historical = result.get("historical")
    if isinstance(historical, list):
        result["historical"] = [
            lineage_node(item) if isinstance(item, dict) else item for item in historical
        ]
    return result


def _context_memories(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    return [_context_memory(item) if isinstance(item, dict) else item for item in value]


def _context_lineage(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        head_id: _context_lineage_entry(entry) if isinstance(entry, dict) else entry
        for head_id, entry in value.items()
    }


def context_data(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the complete local retrieve-context payload."""
    result = dict(data)
    if "entity_ids" in result:
        result["memory_ids"] = result.pop("entity_ids")
    if "memories" in result:
        result["memories"] = _context_memories(result["memories"])
    if "lineage" in result:
        result["lineage"] = _context_lineage(result["lineage"])
    if isinstance(result.get("conflict_sets"), list):
        result["conflict_sets"] = [_context_conflict_set(item) for item in result["conflict_sets"]]
    return result
