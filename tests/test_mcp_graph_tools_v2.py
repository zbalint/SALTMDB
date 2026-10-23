from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mcp_contract
import pytest

from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


@pytest.fixture(autouse=True)
def configured(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection, previous_backend, previous_db_path = mcp_contract.configure(
        tmp_path / "graph-v2.db", agent_id="graph-owner"
    )
    yield connection
    mcp_contract.cleanup(connection, previous_backend, previous_db_path)


def call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(mcp_contract.call(name, arguments))


def test_graph_schemas_are_exact_v2():
    async def schemas():
        listed = {tool.name: tool for tool in await tools.mcp.list_tools()}
        return listed["get_lineage"], listed["get_related_memories"], listed["retrieve_context"]

    lineage, related, context = asyncio.run(schemas())
    assert tuple(lineage.inputSchema["properties"]) == ("memory_id", "direction", "max_depth")
    assert lineage.inputSchema["required"] == ["memory_id"]
    assert lineage.inputSchema["properties"]["memory_id"]["maxLength"] == 200
    assert lineage.inputSchema["properties"]["max_depth"]["minimum"] == 1
    assert lineage.inputSchema["properties"]["max_depth"]["maximum"] == 10
    assert tuple(related.inputSchema["properties"]) == (
        "memory_id",
        "max_depth",
        "direction",
        "include_summary",
    )
    assert tuple(context.inputSchema["properties"]) == ("memory_ids", "budget_tokens")
    assert context.inputSchema["required"] == ["memory_ids"]
    assert context.inputSchema["properties"]["memory_ids"]["minItems"] == 1
    assert context.inputSchema["properties"]["memory_ids"]["maxItems"] == 20
    assert context.inputSchema["properties"]["memory_ids"]["items"]["maxLength"] == 200
    budget_schema = context.inputSchema["properties"]["budget_tokens"]
    assert any(branch.get("minimum") == 1 for branch in budget_schema.get("anyOf", [budget_schema]))


def test_graph_bounds_reject_before_backend():
    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    previous = tools._set_backend_for_test(FailBackend())
    try:
        assert call("get_lineage", {"memory_id": "x", "max_depth": 0})["status"] == "rejected"
        assert call("get_lineage", {"memory_id": "x", "max_depth": 11})["status"] == "rejected"
        assert (
            call("get_related_memories", {"memory_id": "x", "max_depth": 0})["status"] == "rejected"
        )
        assert (
            call("get_related_memories", {"memory_id": "x", "max_depth": 11})["status"]
            == "rejected"
        )
        assert call("retrieve_context", {"memory_ids": []})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": ["x"] * 21})["status"] == "rejected"
        assert (
            call("retrieve_context", {"memory_ids": ["x"], "budget_tokens": 0})["status"]
            == "rejected"
        )
        assert call("get_lineage", {"memory_id": ""})["status"] == "rejected"
        assert call("get_related_memories", {"memory_id": ""})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": [""]})["status"] == "rejected"
        assert call("get_lineage", {"memory_id": "x" * 201})["status"] == "rejected"
        assert call("get_related_memories", {"memory_id": "x" * 201})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": ["x" * 201]})["status"] == "rejected"
    finally:
        tools._set_backend_for_test(previous)


def test_retrieve_context_forwards_internal_local_request():
    class CaptureBackend:
        def __init__(self):
            self.calls = []

        def call(self, name, kwargs):
            self.calls.append((name, kwargs))
            return {
                "entity_ids": kwargs["entity_ids"],
                "memories": [],
                "edges": [],
                "lineage": {},
                "conflict_sets": [],
                "metadata": {},
            }

    backend = CaptureBackend()
    previous = tools._set_backend_for_test(backend)
    try:
        result = tools.retrieve_context(memory_ids=["anchor"], budget_tokens=5)
    finally:
        tools._set_backend_for_test(previous)
    assert result["status"] == "ok"
    assert result["data"]["memory_ids"] == ["anchor"]
    assert backend.calls == [
        (
            "retrieve_context",
            {
                "entity_ids": ["anchor"],
                "query": None,
                "budget_tokens": 5,
                "strategy": "local",
                "agent_id": "graph-owner",
            },
        )
    ]


def test_retrieve_context_adapter_result_branches():
    branches = [
        (
            {
                "entity_ids": ["anchor"],
                "memories": [],
                "edges": [],
                "lineage": {},
                "conflict_sets": [],
                "metadata": {},
            },
            {
                "status": "ok",
                "data": {
                    "memory_ids": ["anchor"],
                    "memories": [],
                    "edges": [],
                    "lineage": {},
                    "conflict_sets": [],
                    "metadata": {},
                },
                "warnings": [],
                "errors": [],
            },
        ),
        (
            {
                "status": "rejected",
                "errors": [{"code": "UNKNOWN_ENTITY_ID", "field": "entity_ids"}],
                "warnings": [],
            },
            {
                "status": "rejected",
                "errors": [{"code": "UNKNOWN_ENTITY_ID", "field": "memory_ids"}],
                "warnings": [],
            },
        ),
        (
            {"status": "DAEMON_STARTING", "tool": "retrieve_context"},
            {"status": "DAEMON_STARTING", "tool": "retrieve_context"},
        ),
        (
            None,
            {
                "status": "rejected",
                "errors": [
                    {"code": "INTERNAL_ERROR", "message": "Tool execution failed unexpectedly."}
                ],
                "warnings": [],
            },
        ),
    ]
    for backend_result, expected in branches:
        previous = tools._set_backend_for_test(
            type("Backend", (), {"call": lambda *_: backend_result})()
        )
        try:
            assert tools.retrieve_context(memory_ids=["anchor"]) == expected
        finally:
            tools._set_backend_for_test(previous)


def test_identity_preparation_overrides_caller_for_all_graph_tools():
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id("graph-owner")
    try:
        for name, kwargs in (
            ("get_lineage", {"entity_id": "anchor", "agent_id": "spoofed"}),
            ("get_related_memories", {"entity_id": "anchor", "agent_id": "spoofed"}),
            ("retrieve_context", {"entity_ids": ["anchor"], "agent_id": "spoofed"}),
        ):
            prepared = tools._rpc_prepare_kwargs(name, kwargs)
            assert prepared["agent_id"] == "graph-owner"
    finally:
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_agent_id("graph-owner")


def test_guard_renames_old_graph_fields():
    lineage = call("get_lineage", {"entity_id": "anchor"})
    assert lineage["status"] == "rejected"
    assert lineage["errors"][0]["corrected_call"] == {"memory_id": "anchor"}
    related = call("get_related_memories", {"entity_id": "anchor", "include_inspect": True})
    assert related["status"] == "rejected"
    assert related["errors"][0]["corrected_call"] == {
        "memory_id": "anchor",
        "include_summary": True,
    }
    strategy = call("retrieve_context", {"strategy": "local"})
    assert strategy["status"] == "rejected"
    assert "anchors only" in strategy["errors"][0]["message"]


def _assert_no_keys(value: object, forbidden: set[str]) -> None:
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_keys(child, forbidden)
    elif isinstance(value, list):
        for child in value:
            _assert_no_keys(child, forbidden)


def test_live_graph_records_use_memory_id_shape():
    stored_root = tools.store_memory(
        title="Graph shape root",
        content="# Graph shape root\n\nA shared root for the graph shape contract.",
    )
    stored_neighbor = tools.store_memory(
        title="Graph shape neighbor",
        content="# Graph shape neighbor\n\nA shared neighbor for the graph shape contract.",
    )
    assert stored_root["status"] == "ok"
    assert stored_neighbor["status"] == "ok"
    root_id = stored_root["data"]["memory_id"]
    neighbor_id = stored_neighbor["data"]["memory_id"]
    relation = tools.manage_relation(
        relations=[{"source_id": root_id, "target_id": neighbor_id, "predicate": "related_to"}]
    )
    assert relation["status"] == "ok"
    related = tools.get_related_memories(memory_id=root_id, include_summary=True)
    assert related["status"] == "ok", related
    assert set(related["data"]) == {
        "memory_id",
        "root",
        "related_memories",
        "edges",
        "total_edges",
        "graph_exhausted",
        "point_in_time",
        "max_depth",
    }
    assert related["data"]["total_edges"] == len(related["data"]["edges"])
    _assert_no_keys(
        related["data"], {"id", "parent_ids", "total_related_found", "total_dependencies_found"}
    )
    revised = tools.revise_memory(
        memory_id=root_id,
        title="Graph shape successor",
        content="# Graph shape successor\n\nA shared successor for the graph shape contract.",
        reason="Exercise public lineage shape",
    )
    assert revised["status"] == "ok", revised
    successor_id = revised["data"]["successor_memory_id"]
    lineage = tools.get_lineage(memory_id=successor_id)
    assert lineage["status"] == "ok", lineage
    assert lineage["data"]["total_edges"] == len(lineage["data"]["edges"])
    assert "total" not in lineage["data"]
    _assert_no_keys(lineage["data"], {"id", "entity_id", "total"})
    record = tools.get_memory(memory_id=successor_id)
    assert record["status"] == "ok", record
    _assert_no_keys(record["data"], {"id", "parent_ids"})


def test_two_hundred_character_exact_title_is_accepted():
    title = "T" * 200
    stored = tools.store_memory(
        title=title,
        content="# Long title anchor\n\nA body for the exact two hundred character title.",
    )
    assert stored["status"] == "ok", stored
    memory_id = stored["data"]["memory_id"]
    assert call("get_lineage", {"memory_id": title})["status"] == "ok"
    assert call("get_related_memories", {"memory_id": title})["status"] == "ok"
    assert call("retrieve_context", {"memory_ids": [title]})["status"] == "ok"
    assert memory_id


def test_retrieve_context_two_anchors_preserves_conflict_shape():
    first = tools.store_memory(
        title="Context conflict first",
        content="# Context conflict first\n\nFirst anchor for the contradiction context contract.",
    )
    second = tools.store_memory(
        title="Context conflict second",
        content="# Context conflict second\n\nSecond anchor for the contradiction context contract.",
    )
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    first_id = first["data"]["memory_id"]
    second_id = second["data"]["memory_id"]
    relation = tools.manage_relation(
        relations=[{"source_id": first_id, "target_id": second_id, "predicate": "contradicts"}]
    )
    assert relation["status"] == "ok", relation
    result = tools.retrieve_context(memory_ids=[first_id, second_id])
    assert result["status"] == "ok", result
    assert result["data"]["memory_ids"] == [first_id, second_id]
    assert result["data"]["conflict_sets"]
    _assert_no_keys(
        result["data"], {"entity_id", "entity_ids", "origin_hit_id", "orphan_entity_id"}
    )


def test_retrieve_context_edges_preserve_relation_records():
    first = tools.store_memory(
        title="Context edge first",
        content="# Context edge first\n\nFirst context edge anchor.",
    )
    second = tools.store_memory(
        title="Context edge second",
        content="# Context edge second\n\nSecond context edge anchor.",
    )
    assert first["status"] == "ok", first
    assert second["status"] == "ok", second
    first_id = first["data"]["memory_id"]
    second_id = second["data"]["memory_id"]
    relation = tools.manage_relation(
        relations=[{"source_id": first_id, "target_id": second_id, "predicate": "depends_on"}]
    )
    assert relation["status"] == "ok", relation
    result = tools.retrieve_context(memory_ids=[first_id, second_id])
    assert result["status"] == "ok", result
    assert result["data"]["memory_ids"] == [first_id, second_id]
    assert len(result["data"]["edges"]) == 1
    edge = result["data"]["edges"][0]
    assert set(edge) == {"relation_id", "source_id", "target_id", "predicate"}
    assert edge["source_id"] == first_id
    assert edge["target_id"] == second_id
    assert edge["predicate"] == "depends_on"
    _assert_context_no_legacy_keys(result["data"])


def test_retrieve_context_expansion_provenance_uses_memory_id():
    root = tools.store_memory(
        title="Context provenance root",
        content="# Context provenance root\n\nRoot anchor for expansion provenance.",
    )
    neighbor = tools.store_memory(
        title="Context provenance neighbor",
        content="# Context provenance neighbor\n\nNeighbor for expansion provenance.",
    )
    assert root["status"] == "ok"
    assert neighbor["status"] == "ok"
    root_id = root["data"]["memory_id"]
    neighbor_id = neighbor["data"]["memory_id"]
    relation = tools.manage_relation(
        relations=[{"source_id": root_id, "target_id": neighbor_id, "predicate": "depends_on"}]
    )
    assert relation["status"] == "ok"
    result = tools.retrieve_context(memory_ids=[root_id])
    assert result["status"] == "ok", result
    expansion = next(
        item for item in result["data"]["memories"] if item["memory_id"] == neighbor_id
    )
    assert expansion["retrieval_provenance"][0]["origin_memory_id"] == root_id
    _assert_context_no_legacy_keys(result["data"])
    _assert_no_legacy_keys(expansion)
    _assert_no_keys(expansion, {"entity_id", "origin_hit_id", "orphan_entity_id"})


def _assert_no_legacy_keys(
    value: object,
    *,
    edge_record: bool = False,
    edge_keys: set[str] | None = None,
) -> None:
    forbidden = {
        "id",
        "parent_ids",
        "entity_id",
        "entity_ids",
        "total",
        "total_related_found",
        "total_dependencies_found",
    }
    default_edge_keys = {
        "relation_id",
        "source_id",
        "target_id",
        "predicate",
        "depth",
        "source_title",
        "source_status",
        "target_title",
        "target_status",
    }
    allowed_edge_keys = edge_keys or default_edge_keys
    if isinstance(value, dict):
        if edge_record:
            assert set(value).issubset(allowed_edge_keys)
        for key, child in value.items():
            if key in {"source_id", "target_id"}:
                assert edge_record
            else:
                assert key not in forbidden
            _assert_no_legacy_keys(
                child,
                edge_record=key == "edges",
                edge_keys=allowed_edge_keys,
            )
    elif isinstance(value, list):
        for child in value:
            _assert_no_legacy_keys(
                child,
                edge_record=edge_record,
                edge_keys=allowed_edge_keys,
            )


def test_recursive_edge_scanner_rejects_novel_edge_keys():
    with pytest.raises(AssertionError):
        _assert_no_legacy_keys({"edges": [{"unexpected_edge_key": "bad"}]})


def _assert_context_no_legacy_keys(value: object) -> None:
    assert isinstance(value, dict)
    for key, child in value.items():
        assert key not in {
            "id",
            "parent_ids",
            "entity_id",
            "entity_ids",
            "total",
            "total_related_found",
            "total_dependencies_found",
        }
        if key == "conflict_sets":
            assert isinstance(child, list)
            for conflict_set in child:
                assert isinstance(conflict_set, dict)
                assert conflict_set["id"].startswith("cs-")
                without_set_id = dict(conflict_set)
                without_set_id.pop("id")
                _assert_no_legacy_keys(without_set_id)
        else:
            _assert_no_legacy_keys(
                child,
                edge_record=key == "edges",
                edge_keys={"relation_id", "source_id", "target_id", "predicate"}
                if key == "edges"
                else None,
            )


def _assert_graph_error(result: dict[str, Any], identifier: str) -> None:
    assert result["status"] == "rejected", result
    error = result["errors"][0]
    assert error["code"] == "VALIDATION_ERROR"
    assert "field" not in error
    assert error["message"] == f"Could not resolve entity '{identifier}'"


def test_lineage_ancestors_and_descendants_have_literal_v2_records():
    original = tools.store_memory(
        title="Direction chain original",
        content="# Direction chain original\n\nOriginal direction-chain memory.",
    )
    assert original["status"] == "ok", original
    revised = tools.revise_memory(
        memory_id=original["data"]["memory_id"],
        title="Direction chain revised",
        content="# Direction chain revised\n\nRevised direction-chain memory.",
        reason="Exercise both lineage directions",
    )
    assert revised["status"] == "ok", revised
    original_id = original["data"]["memory_id"]
    revised_id = revised["data"]["successor_memory_id"]
    expected_data = {
        "memory_id",
        "direction",
        "root",
        "nodes",
        "edges",
        "total_edges",
        "total_nodes",
        "graph_exhausted",
        "point_in_time",
        "max_depth",
    }
    ancestors = tools.get_lineage(memory_id=revised_id, direction="ancestors")
    descendants = tools.get_lineage(memory_id=original_id, direction="descendants")
    for result, direction, root_id in (
        (ancestors, "ancestors", revised_id),
        (descendants, "descendants", original_id),
    ):
        assert result["status"] == "ok", result
        data = result["data"]
        assert set(data) == expected_data
        assert set(data["root"]) == {
            "memory_id",
            "title",
            "status",
            "agent_id",
            "updated_at",
            "depth",
            "generation_depth",
        }
        assert data["root"]["memory_id"] == root_id
        assert data["root"]["title"] == (
            "Direction chain revised" if direction == "ancestors" else "Direction chain original"
        )
        expected_root_status = "raw" if root_id == revised_id else "archived"
        assert data["root"]["status"] == expected_root_status
        assert data["root"]["agent_id"] == "graph-owner"
        assert data["root"]["depth"] == 0
        assert data["root"]["generation_depth"] == 0
        assert isinstance(data["root"]["updated_at"], str) and data["root"]["updated_at"]
        for node in data["nodes"]:
            expected_id = (
                revised_id if direction == "ancestors" and node["depth"] == 0 else original_id
            )
            if direction == "ancestors":
                expected_id = revised_id if node["depth"] == 0 else original_id
            else:
                expected_id = original_id if node["depth"] == 0 else revised_id
            expected_title = (
                "Direction chain revised"
                if expected_id == revised_id
                else "Direction chain original"
            )
            assert set(node) == {
                "memory_id",
                "title",
                "status",
                "agent_id",
                "updated_at",
                "depth",
                "generation_depth",
            }
            assert {key: value for key, value in node.items() if key != "updated_at"} == {
                "memory_id": expected_id,
                "title": expected_title,
                "status": "raw" if expected_id == revised_id else "archived",
                "agent_id": "graph-owner",
                "depth": 0 if expected_id == root_id else 1,
                "generation_depth": 0 if expected_id == root_id else 1,
            }
            assert isinstance(node["updated_at"], str) and node["updated_at"]
        edge = data["edges"][0]
        assert set(edge) == {
            "relation_id",
            "source_id",
            "target_id",
            "predicate",
            "depth",
            "source_title",
            "source_status",
            "target_title",
            "target_status",
        }
        assert {key: value for key, value in edge.items() if key != "relation_id"} == {
            "source_id": revised_id,
            "target_id": original_id,
            "predicate": "revises",
            "depth": 1,
            "source_title": "Direction chain revised",
            "source_status": "raw",
            "target_title": "Direction chain original",
            "target_status": "archived",
        }
        assert isinstance(edge["relation_id"], str) and edge["relation_id"]
        assert data["total_edges"] == len(data["edges"])
        assert data["total_nodes"] == len(data["nodes"])


def test_memory_and_batch_records_have_recursive_public_keys():
    original = tools.store_memory(
        title="Recursive record original",
        content="# Recursive record original\n\nOriginal recursive record.",
    )
    assert original["status"] == "ok", original
    revised = tools.revise_memory(
        memory_id=original["data"]["memory_id"],
        title="Recursive record revised",
        content="# Recursive record revised\n\nRevised recursive record.",
        reason="Exercise nested record conversion",
    )
    assert revised["status"] == "ok", revised
    memory_id = revised["data"]["successor_memory_id"]
    full = tools.get_memory(memory_id=memory_id)
    batch = tools.get_memories([memory_id])
    assert full["status"] == "ok", full
    assert batch["status"] == "ok", batch
    _assert_no_legacy_keys(full["data"])
    _assert_no_legacy_keys(batch["data"])


def test_related_data_recursive_keys_and_count_for_both_summary_modes():
    source = tools.store_memory(
        title="Recursive related source",
        content="# Recursive related source\n\nSource for related recursive scan.",
    )
    target = tools.store_memory(
        title="Recursive related target",
        content="# Recursive related target\n\nTarget for related recursive scan.",
    )
    assert source["status"] == "ok", source
    assert target["status"] == "ok", target
    source_id = source["data"]["memory_id"]
    target_id = target["data"]["memory_id"]
    relation = tools.manage_relation(
        relations=[{"source_id": source_id, "target_id": target_id, "predicate": "related_to"}]
    )
    assert relation["status"] == "ok", relation
    for include_summary in (False, True):
        result = tools.get_related_memories(
            memory_id=source_id, direction="outbound", include_summary=include_summary
        )
        assert result["status"] == "ok", result
        data = result["data"]
        assert set(data["root"]) == {"memory_id", "title", "status"}
        assert data["root"]["memory_id"] == source_id
        depth_zero = [item for item in data["related_memories"] if item["depth"] == 0]
        assert len(depth_zero) == 1
        assert depth_zero[0]["memory_id"] == source_id
        assert data["total_edges"] == len(data["edges"])
        assert "total_related_found" not in str(result)
        assert "total_dependencies_found" not in str(result)
        _assert_no_legacy_keys(data)


def test_graph_unknown_and_ambiguous_ids_keep_validation_error_contract(
    configured: sqlite3.Connection,
):
    unknown = "unknown graph identifier"
    _assert_graph_error(tools.get_lineage(memory_id=unknown), unknown)
    _assert_graph_error(tools.get_related_memories(memory_id=unknown), unknown)
    first = tools.store_memory(
        title="Ambiguous graph first",
        content="# Ambiguous graph first\n\nFirst ambiguous graph record.",
    )
    second = tools.store_memory(
        title="Ambiguous graph second",
        content="# Ambiguous graph second\n\nSecond ambiguous graph record.",
    )
    assert first["status"] == "ok", first
    assert second["status"] == "ok", second
    first_id = first["data"]["memory_id"]
    second_id = second["data"]["memory_id"]
    prefix = first_id[:8]
    forced_second = prefix + second_id[8:]
    configured.execute("UPDATE entities SET id = ? WHERE id = ?", (forced_second, second_id))
    configured.commit()
    _assert_graph_error(tools.get_lineage(memory_id=prefix), prefix)
    _assert_graph_error(tools.get_related_memories(memory_id=prefix), prefix)
    unknown_result = tools.retrieve_context(memory_ids=[unknown])
    ambiguous_result = tools.retrieve_context(memory_ids=[prefix])
    assert unknown_result["status"] == "rejected", unknown_result
    assert unknown_result["errors"][0]["field"] == "memory_ids"
    assert unknown_result["errors"][0]["message"] == (
        "No memory matches entity_id 'unknown graph identifier'."
    )
    assert ambiguous_result["status"] == "rejected", ambiguous_result
    assert ambiguous_result["errors"][0]["field"] == "memory_ids"
    assert ambiguous_result["errors"][0]["message"] == (
        f"ID prefix '{prefix}' matches multiple memories; provide a longer prefix or full UUID."
    )


def test_each_single_id_tool_rejects_bounds_before_backend():
    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    original = tools._set_backend_for_test(FailBackend())
    try:
        for name, arguments in (
            ("get_lineage", {"memory_id": "", "max_depth": 0}),
            ("get_related_memories", {"memory_id": "", "max_depth": 0}),
        ):
            assert call(name, arguments)["status"] == "rejected"
        assert call("get_lineage", {"memory_id": "x", "max_depth": 11})["status"] == "rejected"
        assert (
            call("get_related_memories", {"memory_id": "x", "max_depth": 11})["status"]
            == "rejected"
        )
    finally:
        tools._set_backend_for_test(original)


def test_retrieve_context_each_bound_rejects_before_backend():
    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    original = tools._set_backend_for_test(FailBackend())
    try:
        assert call("retrieve_context", {"memory_ids": []})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": ["x"] * 21})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": [""]})["status"] == "rejected"
        assert call("retrieve_context", {"memory_ids": ["x" * 201]})["status"] == "rejected"
        assert (
            call("retrieve_context", {"memory_ids": ["x"], "budget_tokens": 0})["status"]
            == "rejected"
        )
    finally:
        tools._set_backend_for_test(original)


def test_retrieve_context_unknown_fields_and_corrected_calls_are_literal():
    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    original = tools._set_backend_for_test(FailBackend())
    try:
        query = call("retrieve_context", {"query": "q"})
        strategy = call("retrieve_context", {"strategy": "local"})
        both = call("retrieve_context", {"strategy": "local", "entity_ids": ["x"]})
    finally:
        tools._set_backend_for_test(original)
    assert query["status"] == "rejected"
    assert "anchors only" in query["errors"][0]["message"]
    assert query["errors"][0]["corrected_call"] == {}
    assert strategy["status"] == "rejected"
    assert "anchors only" in strategy["errors"][0]["message"]
    assert strategy["errors"][0]["corrected_call"] == {}
    assert both["status"] == "rejected"
    assert both["errors"][0]["corrected_call"] == {"memory_ids": ["x"]}


def test_rpc_identity_and_descriptions_are_literal_for_all_graph_tools():
    assert " ".join((inspect.getdoc(tools.get_lineage) or "").split()).startswith(
        "Use this to see where a memory came from or what replaced it (revision, supersession, consolidation history); use get_related_memories for manage_relation edges."
    )
    assert " ".join((inspect.getdoc(tools.get_related_memories) or "").split()).startswith(
        "Use this to walk the relations you created with manage_relation from one memory; use get_lineage for revision history and retrieve_context for a ready budgeted bundle."
    )
    assert " ".join((inspect.getdoc(tools.retrieve_context) or "").split()).startswith(
        "Use this when you already hold anchor ids and want their connected context (graph neighbours, contradictions, lifecycle history) packed into a token budget in one call; use search_memory first to find the anchors."
    )
    for name, kwargs in (
        ("get_lineage", {"entity_id": "anchor", "agent_id": "spoofed"}),
        ("get_related_memories", {"entity_id": "anchor", "agent_id": "spoofed"}),
        ("retrieve_context", {"entity_ids": ["anchor"], "agent_id": "spoofed"}),
    ):
        prepared = tools._rpc_prepare_kwargs(name, kwargs)
        assert prepared["agent_id"] == "graph-owner"


def test_rpc_backend_call_injects_identity_for_all_graph_tools(monkeypatch):
    captured = []

    def dispatch(_db_path, tool_name, kwargs):
        captured.append((tool_name, kwargs))
        return {"status": "ok"}

    monkeypatch.setattr(tools, "_rpc_dispatch_with_recovery", dispatch)
    backend = tools.RpcBackend()
    for name, kwargs in (
        ("get_lineage", {"entity_id": "anchor", "agent_id": "spoofed"}),
        ("get_related_memories", {"entity_id": "anchor", "agent_id": "spoofed"}),
        ("retrieve_context", {"entity_ids": ["anchor"], "agent_id": "spoofed"}),
    ):
        assert backend.call(name, kwargs) == {"status": "ok"}
    assert [name for name, _kwargs in captured] == [
        "get_lineage",
        "get_related_memories",
        "retrieve_context",
    ]
    assert all(kwargs["agent_id"] == "graph-owner" for _name, kwargs in captured)


def test_lineage_title_boundaries_are_literal_and_reject_before_backend():
    title = "L" * 200
    stored = tools.store_memory(title=title, content="# Lineage title boundary\n\nExact title.")
    assert stored["status"] == "ok", stored
    assert tools.get_lineage(memory_id=title)["status"] == "ok"

    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    original = tools._set_backend_for_test(FailBackend())
    try:
        assert call("get_lineage", {"memory_id": "L" * 201})["status"] == "rejected"
    finally:
        tools._set_backend_for_test(original)


def test_related_title_boundaries_are_literal_and_reject_before_backend():
    title = "R" * 200
    stored = tools.store_memory(title=title, content="# Related title boundary\n\nExact title.")
    assert stored["status"] == "ok", stored
    assert tools.get_related_memories(memory_id=title)["status"] == "ok"

    class FailBackend:
        def call(self, *_args, **_kwargs):
            raise AssertionError("service must not be called")

    original = tools._set_backend_for_test(FailBackend())
    try:
        assert call("get_related_memories", {"memory_id": "R" * 201})["status"] == "rejected"
    finally:
        tools._set_backend_for_test(original)


def test_graph_depth_and_direction_forwarding_are_literal():
    root = tools.store_memory(
        title="Depth chain zero",
        content="# Depth chain zero\n\nDepth chain root.",
    )
    assert root["status"] == "ok", root
    ids = [root["data"]["memory_id"]]
    for index in range(1, 4):
        revised = tools.revise_memory(
            memory_id=ids[-1],
            title=f"Depth chain {index}",
            content=f"# Depth chain {index}\n\nDepth chain revision {index}.",
            reason="Exercise max depth",
        )
        assert revised["status"] == "ok", revised
        ids.append(revised["data"]["successor_memory_id"])
    for depth, expected_edges, exhausted in ((1, 1, False), (2, 2, False), (5, 3, True)):
        result = tools.get_lineage(memory_id=ids[-1], direction="ancestors", max_depth=depth)
        assert result["status"] == "ok", result
        assert result["data"]["total_edges"] == expected_edges
        assert len(result["data"]["nodes"]) == expected_edges + 1
        assert result["data"]["graph_exhausted"] is exhausted
    descendants = tools.get_lineage(memory_id=ids[0], direction="descendants", max_depth=1)
    assert descendants["status"] == "ok", descendants
    assert descendants["data"]["direction"] == "descendants"
    assert descendants["data"]["nodes"][1]["memory_id"] == ids[1]

    related_ids = []
    for index in range(4):
        stored = tools.store_memory(
            title=f"Relation depth {index}",
            content=f"# Relation depth {index}\n\nRelation depth node.",
        )
        assert stored["status"] == "ok", stored
        related_ids.append(stored["data"]["memory_id"])
    for source_id, target_id in zip(related_ids, related_ids[1:]):
        relation = tools.manage_relation(
            relations=[{"source_id": source_id, "target_id": target_id, "predicate": "depends_on"}]
        )
        assert relation["status"] == "ok", relation
    for depth, expected_edges, exhausted in ((1, 1, False), (2, 2, False), (5, 3, True)):
        result = tools.get_related_memories(
            memory_id=related_ids[0], direction="outbound", max_depth=depth
        )
        assert result["status"] == "ok", result
        assert result["data"]["total_edges"] == expected_edges
        assert result["data"]["graph_exhausted"] is exhausted
    inbound = tools.get_related_memories(memory_id=related_ids[1], direction="inbound", max_depth=1)
    both = tools.get_related_memories(memory_id=related_ids[1], direction="both", max_depth=1)
    assert inbound["status"] == "ok" and both["status"] == "ok"
    assert inbound["data"]["edges"][0]["source_id"] == related_ids[0]
    assert both["data"]["total_edges"] == 2


def test_graph_wrappers_forward_max_depth_and_direction_to_backend():
    class CaptureBackend:
        def __init__(self):
            self.calls = []

        def call(self, name, kwargs):
            self.calls.append((name, kwargs))
            return {}

    backend = CaptureBackend()
    previous = tools._set_backend_for_test(backend)
    try:
        tools.get_lineage(memory_id="anchor", direction="descendants", max_depth=2)
        tools.get_related_memories(memory_id="anchor", direction="inbound", max_depth=3)
    finally:
        tools._set_backend_for_test(previous)
    assert backend.calls == [
        (
            "get_lineage",
            {
                "entity_id": "anchor",
                "direction": "descendants",
                "max_depth": 2,
                "agent_id": "graph-owner",
            },
        ),
        (
            "get_related_memories",
            {
                "entity_id": "anchor",
                "max_depth": 3,
                "direction": "inbound",
                "include_inspect": False,
                "agent_id": "graph-owner",
            },
        ),
    ]
