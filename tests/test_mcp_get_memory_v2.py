from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mcp_contract
import pytest

from saltmdb.daemon import dispatch, protocol
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


def call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(mcp_contract.call(name, arguments))


def store(title: str, content: str | None = None, **kwargs: Any) -> dict[str, Any]:
    body = content or f"# {title}\n\nA durable body for {title} with enough detail."
    result = tools.store_memory(title=title, content=body, **kwargs)
    assert result["status"] == "ok", result
    return result["data"]


@pytest.fixture
def configured(tmp_path: Path):
    connection, previous_backend, previous_db_path = mcp_contract.configure(
        tmp_path / "s4b.db", agent_id="s4b-test-agent"
    )
    yield connection
    mcp_contract.cleanup(connection, previous_backend, previous_db_path)


def test_schemas_are_exact_v2():
    async def schemas():
        listed = {tool.name: tool for tool in await tools.mcp.list_tools()}
        return listed["get_memory"], listed["get_memories"]

    single, batch = asyncio.run(schemas())
    assert tuple(single.inputSchema["properties"]) == (
        "memory_id",
        "detail",
        "include_trace_provenance",
    )
    assert single.inputSchema["required"] == ["memory_id"]
    assert tuple(batch.inputSchema["properties"]) == (
        "memory_ids",
        "detail",
        "include_trace_provenance",
    )
    assert batch.inputSchema["required"] == ["memory_ids"]
    memory_ids = batch.inputSchema["properties"]["memory_ids"]
    assert memory_ids["minItems"] == 1
    assert memory_ids["maxItems"] == 20
    assert memory_ids["items"]["maxLength"] == 200


def test_full_and_summary_records_have_public_shape(configured):
    data = store("Literal record", "# Heading\n\nDistinct body text for the record.")
    full = call("get_memory", {"memory_id": data["memory_id"]})
    summary = call("get_memory", {"memory_id": data["memory_id"], "detail": "summary"})
    assert full["status"] == "ok"
    assert full["data"]["memory_id"] == data["memory_id"]
    assert "id" not in full["data"]
    assert full["data"]["content"] == "# Heading\n\nDistinct body text for the record."
    assert "lineage" in full["data"]
    assert summary["status"] == "ok"
    assert "snippet" in summary["data"]
    assert "content" not in summary["data"]
    assert "lineage" in summary["data"]


def test_literal_record_mapping_pins_distinct_values(configured):
    data = store("Distinct value title", "# Body heading\n\nDistinct literal content.")
    result = call("get_memory", {"memory_id": data["memory_id"]})
    record = result["data"]
    assert record["memory_id"] == data["memory_id"]
    assert record["title"] == "Distinct value title"
    assert record["content"] == "# Body heading\n\nDistinct literal content."
    assert record["status"] == "raw"
    assert record["scope"] == "shared"
    assert record["memory_type"] == "fact"


def test_summary_trace_provenance_is_forwarded(configured):
    data = store("Trace summary", "# Trace\n\nSummary trace body.")
    result = call(
        "get_memory",
        {
            "memory_id": data["memory_id"],
            "detail": "summary",
            "include_trace_provenance": True,
        },
    )
    assert result["status"] == "ok"
    assert "trace_provenance" in result["data"]


def test_batch_identity_injection_overrides_caller_and_reads_own_private(configured):
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id("batch-owner")
    try:
        prepared = tools._rpc_prepare_kwargs(
            "get_memories",
            {"memory_ids": ["ignored"], "agent_id": "spoofed-owner"},
        )
        assert prepared["agent_id"] == "batch-owner"
        private = store("Batch owner private", scope="private")
        result = call("get_memories", {"memory_ids": [private["memory_id"]]})
        assert result["status"] == "ok"
        item = result["data"]["items"][0]
        assert item["outcome"] == "ok"
        assert item["memory"]["memory_id"] == private["memory_id"]
    finally:
        SESSION_IDENTITY.reset()
        SESSION_IDENTITY.configure_agent_id("s4b-test-agent")


def test_batch_summary_routing_and_trace_flag(configured):
    data = store("Batch summary routing")
    summary = call(
        "get_memories",
        {"memory_ids": [data["memory_id"]], "detail": "summary"},
    )
    traced = call(
        "get_memories",
        {
            "memory_ids": [data["memory_id"]],
            "detail": "summary",
            "include_trace_provenance": True,
        },
    )
    assert summary["status"] == "ok"
    assert "content" not in summary["data"]["items"][0]["memory"]
    assert "snippet" in summary["data"]["items"][0]["memory"]
    assert "trace_provenance" not in summary["data"]["items"][0]["memory"]
    assert traced["status"] == "ok"
    assert "content" not in traced["data"]["items"][0]["memory"]
    assert "trace_provenance" in traced["data"]["items"][0]["memory"]


def test_batch_ambiguous_outcome_pins_candidates_and_keys(configured):
    first = store("Ambiguous first")
    second = store("Ambiguous second")
    prefix = first["memory_id"][:8]
    replacement = prefix + second["memory_id"][8:]
    configured.execute("PRAGMA foreign_keys = OFF")
    configured.execute(
        "UPDATE entities SET id = ? WHERE id = ?",
        (replacement, second["memory_id"]),
    )
    configured.commit()
    configured.execute("PRAGMA foreign_keys = ON")
    result = call("get_memories", {"memory_ids": [prefix]})
    assert result["status"] == "ok"
    item = result["data"]["items"][0]
    assert set(item) == {"requested_id", "outcome", "memory", "candidates"}
    assert item["requested_id"] == prefix
    assert item["outcome"] == "ambiguous"
    assert item["memory"] is None
    candidate_ids = [candidate["id"] for candidate in item["candidates"]]
    assert first["memory_id"] in candidate_ids
    assert replacement in candidate_ids


def test_dispatchers_reject_invalid_detail_directly():
    single: Any = dispatch.DISPATCH_TABLE["get_memory"](entity_id="any-id", detail="invalid-detail")
    batch: Any = dispatch.DISPATCH_TABLE["get_memories"](
        memory_ids=["any-id"], detail="invalid-detail"
    )
    assert single["status"] == "rejected"
    assert single["errors"][0]["code"] == "VALIDATION_ERROR"
    assert batch["status"] == "rejected"
    assert batch["errors"][0]["code"] == "VALIDATION_ERROR"


def test_get_memory_service_error_remaps_entity_id_field(configured):
    backend = tools._backend_or_raise()
    with patch.object(
        backend,
        "call",
        return_value={
            "status": "rejected",
            "errors": [
                {
                    "code": "UNKNOWN_ENTITY_ID",
                    "field": "entity_id",
                    "message": "not found",
                }
            ],
        },
    ):
        result = tools.get_memory(memory_id="missing")
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "memory_id"


def test_batch_ambiguous_candidates_truncated_propagates(configured):
    backend = tools._backend_or_raise()
    with patch.object(
        backend,
        "call",
        return_value=[
            {
                "requested_id": "prefix",
                "result": {
                    "status": "rejected",
                    "errors": [
                        {
                            "code": "AMBIGUOUS_ID_PREFIX",
                            "field": "entity_id",
                            "message": "ambiguous",
                            "candidates": ["candidate-a"],
                            "candidates_truncated": True,
                        }
                    ],
                },
            }
        ],
    ):
        result = tools.get_memories(["prefix"])
    item = result["data"]["items"][0]
    assert item["outcome"] == "ambiguous"
    assert item["candidates"] == ["candidate-a"]
    assert item["candidates_truncated"] is True


def test_batch_requested_id_echoes_whitespace_exactly(configured):
    with patch.object(
        dispatch.memory_service,
        "get_memory",
        return_value={"status": "ok", "data": {"id": "resolved-id", "content": "body"}},
    ):
        result = tools.get_memories(["  padded-id  "])
    item = result["data"]["items"][0]
    assert item["requested_id"] == "  padded-id  "
    assert item["memory"]["memory_id"] == "resolved-id"


def test_pre_dumped_large_record_does_not_consume_batch_budget(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("SALTMDB_CONTENT_DUMP_DIR", str(tmp_path / "dumps"))
    body = "# Large record\n\n" + ("large content words " * 1300) + "\n\n# Details\n\nend"
    large = store("Pre-dumped large record", body)
    small_body = "# Small\n\n" + ("inline body words " * 1000) + "\n\n# Details\n\nend"
    small = store("Following inline record", small_body)
    result = call(
        "get_memories",
        {"memory_ids": [large["memory_id"], small["memory_id"]]},
    )
    items = result["data"]["items"]
    assert "content" not in items[0]["memory"]
    assert items[0]["memory"]["content_file_path"]
    assert "content" in items[1]["memory"]
    assert len(items[1]["memory"]["content"]) > 17000
    assert "inline body words" in items[1]["memory"]["content"]


def test_read_registry_classification_is_explicit():
    assert "get_memories" in dispatch.DISPATCH_TABLE
    assert "get_memories" not in dispatch.MUTATING_TOOLS
    assert "get_memories" in protocol.READ_TOOLS
    assert "inspect_memory" not in dispatch.DISPATCH_TABLE
    assert "inspect_memory" not in protocol.READ_TOOLS


def test_batch_order_and_mixed_outcomes(configured):
    first = store("Batch first")
    second = store("Batch second")
    result = call(
        "get_memories",
        {
            "memory_ids": [first["memory_id"], "missing-batch-id", second["memory_id"]],
        },
    )
    assert result["status"] == "ok"
    items = result["data"]["items"]
    assert [item["requested_id"] for item in items] == [
        first["memory_id"],
        "missing-batch-id",
        second["memory_id"],
    ]
    assert [item["outcome"] for item in items] == ["ok", "not_found", "ok"]
    assert items[0]["memory"]["memory_id"] == first["memory_id"]
    assert items[1]["memory"] is None


def test_batch_supports_exact_title_and_prefix_full_uuid_pair(configured):
    exact = store("Exact title lookup target")
    prefixed = store("Prefix lookup target")
    prefix = prefixed["memory_id"][:8]
    result = call(
        "get_memories",
        {
            "memory_ids": [
                "Exact title lookup target",
                prefix,
                prefixed["memory_id"],
            ]
        },
    )
    assert result["status"] == "ok"
    items = result["data"]["items"]
    assert [item["outcome"] for item in items] == ["ok", "ok", "ok"]
    assert items[0]["memory"]["memory_id"] == exact["memory_id"]
    assert items[1]["memory"]["memory_id"] == prefixed["memory_id"]
    assert items[2]["memory"]["memory_id"] == prefixed["memory_id"]


def test_batch_duplicate_and_whitespace_ids_reject_without_service_call(configured):
    service = dispatch.memory_service.get_memory
    with patch.object(dispatch.memory_service, "get_memory", wraps=service) as service_call:
        duplicate = tools.get_memories(["same", " same "])
        whitespace = tools.get_memories([" \t"])
    assert duplicate["status"] == "rejected"
    assert duplicate["errors"][0]["field"] == "memory_ids"
    assert whitespace["status"] == "rejected"
    assert whitespace["errors"][0]["field"] == "memory_ids[0]"
    service_call.assert_not_called()


def test_batch_bounds_are_enforced(configured):
    assert call("get_memories", {"memory_ids": []})["status"] == "rejected"
    assert (
        call("get_memories", {"memory_ids": [str(index) for index in range(21)]})["status"]
        == "rejected"
    )
    service = dispatch.memory_service.get_memory
    with patch.object(dispatch.memory_service, "get_memory", wraps=service) as service_call:
        oversized = tools.get_memories(["x" * 201])
        too_many = tools.get_memories([str(index) for index in range(21)])
    assert oversized["status"] == "rejected"
    assert too_many["status"] == "rejected"
    service_call.assert_not_called()


def test_single_memory_id_boundaries_reject_without_backend(configured):
    service = dispatch.memory_service.get_memory
    with patch.object(dispatch.memory_service, "get_memory", wraps=service) as service_call:
        empty = call("get_memory", {"memory_id": ""})
        oversized = call("get_memory", {"memory_id": "x" * 201})
    assert empty["status"] == "rejected"
    assert oversized["status"] == "rejected"
    service_call.assert_not_called()


def test_exact_two_hundred_character_ids_reach_single_and_batch_dispatch(configured):
    title = "G" * 200
    data = store(title)
    single = call("get_memory", {"memory_id": title})
    batch = call("get_memories", {"memory_ids": [title]})
    assert single["status"] == "ok", single
    assert single["data"]["memory_id"] == data["memory_id"]
    assert batch["status"] == "ok", batch
    assert batch["data"]["items"][0]["memory"]["memory_id"] == data["memory_id"]


def test_empty_batch_memory_id_rejects_through_guard_without_backend(configured):
    service = dispatch.memory_service.get_memory
    with patch.object(dispatch.memory_service, "get_memory", wraps=service) as service_call:
        result = tools.get_memories([""])
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "memory_ids[0]"
    service_call.assert_not_called()


def test_dispatcher_batch_bound_message_is_literal(configured):
    result = dispatch._dispatch_get_memories(
        memory_ids=["x" * 201],
        detail="full",
        include_trace_provenance=False,
        agent_id="s4b-test-agent",
    )
    assert isinstance(result, dict)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "memory_ids[0]"
    assert result["errors"][0]["message"] == (
        "memory_ids entries must be 1 to 200 characters after stripping"
    )


def test_entity_id_rename_and_removed_inspect_tool(configured):
    data = store("Rename target")
    renamed = call("get_memory", {"entity_id": data["memory_id"]})
    assert renamed["status"] == "rejected"
    assert renamed["errors"][0]["code"] == "UNKNOWN_FIELD"
    assert renamed["errors"][0]["corrected_call"] == {"memory_id": data["memory_id"]}
    removed = call("inspect_memory", {"entity_id": data["memory_id"]})
    assert removed["status"] == "rejected"
    assert removed["errors"][0]["code"] == "REMOVED_TOOL"


def test_archived_memory_is_returned_without_redirect(configured):
    data = store("Archive target")
    archived = tools.archive_memory([data["memory_id"]])
    assert archived["status"] == "ok", archived
    result = call("get_memory", {"memory_id": data["memory_id"]})
    assert result["status"] == "ok"
    assert result["data"]["memory_id"] == data["memory_id"]
    assert result["data"]["status"] == "archived"


def test_private_memory_of_other_agent_is_not_found(configured):
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id("owner-a")
    private = store("Private target", scope="private")
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id("owner-b")
    result = call("get_memories", {"memory_ids": [private["memory_id"]]})
    assert result["status"] == "ok"
    assert result["data"]["items"][0]["outcome"] == "not_found"


def test_budget_dumps_later_full_records(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("SALTMDB_CONTENT_DUMP_DIR", str(tmp_path / "dumps"))

    def body_for(label: str) -> str:
        return "# First\n\n" + ("distinct body words " * 897) + f"\n{label}\n\n# Second\n\nend"

    first = store("Budget first", body_for("first"))
    second = store("Budget second", body_for("second"))
    third_body = body_for("third")
    third = store("Budget third", third_body)
    result = call(
        "get_memories",
        {"memory_ids": [first["memory_id"], second["memory_id"], third["memory_id"]]},
    )
    assert result["status"] == "ok"
    items = result["data"]["items"]
    assert "content" in items[0]["memory"]
    assert "content" in items[1]["memory"]
    assert "content" not in items[2]["memory"]
    assert items[2]["memory"]["content_file_path"]
    assert items[2]["memory"]["content_preview"] == third_body[:500]


def test_budget_uses_remaining_as_descriptor_threshold(configured):
    body = "x" * 30000
    with patch.object(
        dispatch.memory_service,
        "get_memory",
        return_value={"status": "ok", "data": {"id": "boundary", "content": body}},
    ):
        result = tools.get_memories(["boundary"])
    assert result["status"] == "ok"
    assert result["data"]["items"][0]["memory"]["content"] == body


def test_batch_read_failure_is_request_error(configured):
    original = dispatch.memory_service.get_memory

    def failing_get_memory(entity_id, **kwargs):
        if entity_id == "second":
            return {
                "status": "rejected",
                "errors": [{"code": "MEMORY_READ_FAILED", "message": "failure"}],
            }
        return original(entity_id=entity_id, **kwargs)

    with patch.object(dispatch.memory_service, "get_memory", side_effect=failing_get_memory):
        result = tools.get_memories(["first", "second", "third"])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "INTERNAL_ERROR"


def test_malformed_batch_result_is_internal_error(configured):
    backend = tools._backend_or_raise()
    with patch.object(
        backend,
        "call",
        return_value=[{"requested_id": "malformed", "result": "not-an-envelope"}],
    ):
        result = tools.get_memories(["malformed"])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "INTERNAL_ERROR"


def test_batch_opens_and_closes_one_connection(configured):
    first = store("Connection first")
    second = store("Connection second")
    calls = []
    closes = []
    connection_module = __import__("saltmdb.db.connection", fromlist=["get_connection"])
    original = connection_module.get_connection
    original_close = connection_module.close_connection

    def counted(path):
        calls.append(path)
        return original(path)

    def counted_close(conn):
        closes.append(conn)
        return original_close(conn)

    with (
        patch("saltmdb.db.connection.get_connection", side_effect=counted),
        patch("saltmdb.db.connection.close_connection", side_effect=counted_close),
    ):
        result = call(
            "get_memories",
            {"memory_ids": [first["memory_id"], second["memory_id"]]},
        )
    assert result["status"] == "ok"
    assert len(calls) == 1
    assert len(closes) == 1
