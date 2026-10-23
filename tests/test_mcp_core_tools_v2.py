from __future__ import annotations

import asyncio
import json

import pytest

from saltmdb.mcp import tools
import mcp_contract


@pytest.fixture
def mcp_harness(tmp_path):
    connection, previous_backend, previous_db = mcp_contract.configure(
        tmp_path / "mcp-core.db", "owner"
    )
    yield connection
    mcp_contract.cleanup(connection, previous_backend, previous_db)


def _store_core(title: str) -> str:
    result = tools.store_memory(
        title=title,
        content=f"Distinct MCP core content for {title}.",
        tags=["#mcp-core"],
        core=tools.CoreInput(reason="A" * 20, exit_condition="B" * 20),
    )
    assert result["status"] == "ok", result
    return result["data"]["memory_id"]


def _protocol_json(name: str, arguments: dict):
    response = asyncio.run(mcp_contract.call_protocol(name, arguments))
    return json.loads(response.content[0].model_dump(mode="json")["text"])


@pytest.mark.usefixtures("mcp_harness")
def test_set_and_review_schemas_are_exact_v2():
    schemas = asyncio.run(mcp_contract.mcp.list_tools())
    published = {tool.name: tool.inputSchema for tool in schemas}
    set_schema = published["set_core_memory"]
    assert set_schema["required"] == ["memory_id", "core"]
    assert set_schema["properties"]["memory_id"] == {"title": "Memory Id", "type": "string"}
    assert set(set_schema["$defs"]["SetCoreInput"]["properties"]) == {
        "reason",
        "exit_condition",
        "detail_memory_ids",
    }
    assert set_schema["$defs"]["SetCoreInput"]["additionalProperties"] is False
    assert "review_after" not in set_schema["$defs"]["SetCoreInput"]["properties"]
    review_schema = published["review_core_memory"]
    assert review_schema["required"] == ["memory_id", "outcome", "review_rationale"]
    assert "entity_id" not in review_schema["properties"]
    assert "core_review_after" in review_schema["properties"]


@pytest.mark.usefixtures("mcp_harness")
def test_guard_rejects_removed_and_nested_unknown_fields():
    result = asyncio.run(
        mcp_contract.call(
            "review_core_memory",
            {"entity_id": "removed", "outcome": "retain", "review_rationale": "C" * 20},
        )
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "UNKNOWN_FIELD"
    assert result["errors"][0]["field"] == "entity_id"
    result = asyncio.run(
        mcp_contract.call(
            "set_core_memory",
            {
                "memory_id": "removed",
                "core": {"reason": "A" * 20, "exit_condition": "B" * 20, "review_after": "no"},
            },
        )
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "UNKNOWN_FIELD"
    assert result["errors"][0]["field"] == "core.review_after"


@pytest.mark.usefixtures("mcp_harness")
def test_public_ids_and_errors_use_memory_id():
    memory_id = _store_core("Public ID Core")
    reviewed = tools.review_core_memory(memory_id, "retain", "C" * 20)
    assert reviewed["status"] == "ok"
    assert reviewed["data"]["memory_id"] == memory_id
    assert "id" not in reviewed["data"]
    reviewed_missing = tools.review_core_memory("missing-memory", "retain", "C" * 20)
    assert reviewed_missing["status"] == "rejected"
    assert reviewed_missing["errors"][0]["field"] == "memory_id"
    rejected = tools.set_core_memory(
        "missing-memory", tools.SetCoreInput(reason="A" * 20, exit_condition="B" * 20)
    )
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["field"] == "memory_id"


@pytest.mark.usefixtures("mcp_harness")
def test_public_promote_review_walk_and_idempotent_warnings():
    memory_id = tools.store_memory(
        title="Walk Target",
        content="Distinct walk target content.",
        tags=["#walk"],
    )["data"]["memory_id"]
    promoted = tools.set_core_memory(
        memory_id,
        tools.SetCoreInput(reason="A" * 20, exit_condition="B" * 20),
    )
    assert promoted["status"] == "ok"
    repeated = tools.set_core_memory(
        memory_id,
        tools.SetCoreInput(reason="A" * 20, exit_condition="B" * 20),
    )
    assert repeated["warnings"][0]["code"] == "ALREADY_DONE"
    retained = tools.review_core_memory(memory_id, "retain", "C" * 20)
    assert retained["data"]["message"].startswith(f"Memory '{memory_id}' retained as core")
    demoted = tools.review_core_memory(memory_id, "demote", "C" * 20)
    assert (
        demoted["data"]["message"]
        == "Memory '" + memory_id + "' demoted from core to a normal memory."
    )
    demoted_again = tools.review_core_memory(memory_id, "demote", "C" * 20)
    assert demoted_again["warnings"][0]["code"] == "ALREADY_DONE"
    archived_id = _store_core("Archive Walk Target")
    archived = tools.review_core_memory(archived_id, "archive", "C" * 20)
    assert archived["data"]["message"] == f"Memory '{archived_id}' was reviewed and archived."
    archived_again = tools.review_core_memory(archived_id, "archive", "C" * 20)
    assert archived_again["warnings"][0]["code"] == "ALREADY_DONE"


@pytest.mark.usefixtures("mcp_harness")
def test_missing_required_review_argument_is_contract_rejection():
    result = _protocol_json("review_core_memory", {"memory_id": "missing", "outcome": "retain"})
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "review_rationale"
