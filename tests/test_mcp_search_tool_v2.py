from __future__ import annotations

import asyncio
from typing import Any
import tempfile
from pathlib import Path

import mcp_contract
import pytest

from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class _Backend:
    def __init__(self, result: Any):
        self.result: Any = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool_name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((tool_name, kwargs))
        return self.result


def _item(memory_id: str = "memory-1") -> dict[str, Any]:
    return {
        "id": memory_id,
        "title": "A search result",
        "snippet": "A useful snippet",
        "score": 0.8,
        "weight": 1,
        "is_core": False,
        "memory_type": "fact",
        "created_at": "2026-10-23T00:00:00+00:00",
        "cursor": "offset:2",
        "relevance_preview": "preview",
        "relevance_preview_meta": {"complete": False},
        "trace_evidence_count": 2,
        "related_entities": [{"id": "related", "predicate": "part_of"}],
        "drift_flag": "review",
        "is_superseded": True,
    }


def _call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return asyncio.run(mcp_contract.call(name, arguments))


@pytest.fixture
def backend():
    SESSION_IDENTITY.reset()
    SESSION_IDENTITY.configure_agent_id("search-test-agent")
    previous = tools._set_backend_for_test(_Backend([_item()]))
    yield tools._backend_or_raise()
    tools._set_backend_for_test(previous)
    SESSION_IDENTITY.reset()


def test_search_schema_is_v2():
    properties = asyncio.run(mcp_contract.published_properties("search_memory"))
    assert properties == (
        "query",
        "limit",
        "cursor",
        "context_id",
        "agent_session_id",
        "tags_filter",
        "memory_type_filter",
        "is_core",
        "include_related",
        "mode",
        "detail",
        "return_diagnostics",
    )


def test_compact_item_has_literal_key_set(backend):
    result = tools.search_memory()
    assert result["status"] == "ok"
    assert set(result["data"]["items"][0]) == {
        "memory_id",
        "title",
        "snippet",
        "score",
        "memory_type",
        "is_core",
        "created_at",
        "drift_flag",
    }
    assert "related_entities" not in result["data"]["items"][0]
    assert backend.calls[0][1]["limit"] == 6
    assert backend.calls[0][1]["include_related"] is False


def test_preview_and_related_are_opt_in(backend):
    result = tools.search_memory(detail="preview", include_related=True)
    item = result["data"]["items"][0]
    assert backend.calls[0][1]["include_related"] is True
    assert set(item) == {
        "memory_id",
        "title",
        "snippet",
        "score",
        "memory_type",
        "is_core",
        "created_at",
        "drift_flag",
        "relevance_preview",
        "relevance_preview_meta",
        "trace_evidence_count",
        "related_entities",
    }


def _distinct_item() -> dict[str, Any]:
    return {
        "id": "memory-distinct",
        "title": "Distinct title",
        "snippet": "Distinct snippet",
        "score": 0.37,
        "weight": 9,
        "is_core": True,
        "memory_type": "event",
        "created_at": "2026-01-02T03:04:05+00:00",
        "cursor": "offset:2",
        "drift_flag": "needs-review",
        "is_superseded": True,
        "relevance_preview": "Distinct preview",
        "relevance_preview_meta": {"complete": True, "source": "fixture"},
        "trace_evidence_count": 7,
    }


def test_compact_item_preserves_literal_values(backend):
    backend.result = [_distinct_item()]
    result = tools.search_memory(mode="history")
    assert result["data"]["items"] == [
        {
            "memory_id": "memory-distinct",
            "title": "Distinct title",
            "snippet": "Distinct snippet",
            "score": 0.37,
            "memory_type": "event",
            "is_core": True,
            "created_at": "2026-01-02T03:04:05+00:00",
            "drift_flag": "needs-review",
            "is_superseded": True,
        }
    ]


def test_preview_item_preserves_literal_values(backend):
    backend.result = [_distinct_item()]
    result = tools.search_memory(detail="preview", mode="history")
    assert result["data"]["items"] == [
        {
            "memory_id": "memory-distinct",
            "title": "Distinct title",
            "snippet": "Distinct snippet",
            "score": 0.37,
            "memory_type": "event",
            "is_core": True,
            "created_at": "2026-01-02T03:04:05+00:00",
            "drift_flag": "needs-review",
            "is_superseded": True,
            "relevance_preview": "Distinct preview",
            "relevance_preview_meta": {"complete": True, "source": "fixture"},
            "trace_evidence_count": 7,
        }
    ]


def test_paging_envelope_and_diagnostics_count(backend):
    backend.result = {
        "results": [_item("one"), _item("two"), _item("three")],
        "diagnostics": {"candidate_pool_size": 3, "result_count": 99},
    }
    result = tools.search_memory(limit=2, cursor="offset:4", return_diagnostics=True)
    assert result["data"]["has_more"] is True
    assert result["data"]["next_cursor"] == "offset:6"
    assert len(result["data"]["items"]) == 2
    assert result["data"]["diagnostics"]["result_count"] == 2
    assert backend.calls[0][1]["cursor"] == "offset:4"
    assert backend.calls[0][1]["limit"] == 3


def test_exact_limit_has_no_next_page(backend):
    backend.result = {"results": [_item("one"), _item("two")], "diagnostics": None}
    result = tools.search_memory(limit=2)
    assert result["data"]["has_more"] is False
    assert result["data"]["next_cursor"] is None


@pytest.mark.parametrize("mode", ["broad", "history"])
def test_empty_non_strict_modes_have_no_match_warning(mode: str, backend):
    backend.result = []
    result = tools.search_memory(mode=mode)
    assert result["warnings"] == []


def test_strict_hits_have_no_match_warning(backend):
    backend.result = [_item()]
    result = tools.search_memory(mode="strict")
    assert result["warnings"] == []


@pytest.mark.parametrize(
    "cursor", ["bad", "offset:-1", "offset:abc", "offset:1001", "offset:01", "offset:1\u0662"]
)
def test_invalid_cursor_does_not_dispatch(cursor: str, backend):
    result = tools.search_memory(cursor=cursor)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "cursor"
    assert backend.calls == []


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_limit_is_rejected(limit: int, backend):
    result = _call("search_memory", {"limit": limit})
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "limit"
    assert backend.calls == []


def test_too_many_tags_are_rejected_without_dispatch(backend):
    result = _call("search_memory", {"tags_filter": [f"#tag-{index}" for index in range(21)]})
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "tags_filter"
    assert backend.calls == []


def test_strict_empty_has_no_match_warning(backend):
    backend.result = []
    result = tools.search_memory(mode="strict")
    assert result["data"] == {"items": [], "has_more": False, "next_cursor": None}
    assert result["warnings"][0]["code"] == "NO_MATCH"


def test_service_fault_is_internal_rejection(backend):
    backend.result = {"results": [], "diagnostics": {"error": "backend failed"}}
    result = tools.search_memory(return_diagnostics=True)
    assert result["status"] == "rejected"
    assert result["errors"] == [{"code": "INTERNAL_ERROR", "message": "backend failed"}]
    assert "data" not in result


def test_unknown_query_keywords_has_rename_correction():
    result = _call("search_memory", {"query_keywords": "needle"})
    error = result["errors"][0]
    assert error["code"] == "UNKNOWN_FIELD"
    assert error["field"] == "query_keywords"
    assert error["corrected_call"] == {"query": "needle"}
    assert "renamed to 'query'" in error["message"]


def test_new_query_name_is_accepted(backend):
    result = tools.search_memory(query="needle")
    assert result["status"] == "ok"
    assert backend.calls[0][1]["query_keywords"] == "needle"


def test_service_list_error_is_internal_rejection(backend):
    backend.result = [{"error": "service failed"}]
    result = tools.search_memory()
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "INTERNAL_ERROR"


def test_wire_result_has_expected_envelope_keys(backend):
    payload = _call("search_memory")
    assert backend.calls[0][0] == "search_memory"
    assert set(payload) == {"status", "data", "warnings", "errors"}
    assert set(payload["data"]) == {"items", "has_more", "next_cursor"}


def test_history_preserves_superseded_marker(backend):
    result = tools.search_memory(mode="history")
    assert backend.calls[0][1]["mode"] == "history"
    assert result["data"]["items"][0]["is_superseded"] is True


@pytest.mark.parametrize("tool_name", ["revise_memory", "supersede_memory", "review_core_memory"])
def test_replacement_rename_correction(tool_name):
    result = _call(tool_name, {"entity_id": "old-memory"})
    assert result["errors"][0]["code"] == "UNKNOWN_FIELD"
    assert result["errors"][0]["corrected_call"] == {"memory_id": "old-memory"}


def test_new_name_wins_over_old_rename():
    result = _call("search_memory", {"query": "new", "query_keywords": "old"})
    assert result["errors"][0]["corrected_call"] == {"query": "new"}


def test_real_temp_db_browse_paging_and_exact_title():
    with tempfile.TemporaryDirectory() as temp_dir:
        connection, previous_backend, previous_db_path = mcp_contract.configure(
            Path(temp_dir) / "search.db", agent_id="paging-agent"
        )
        try:
            stored = [
                tools.store_memory(
                    title=f"Paging fixture {index}",
                    content=f"Paging fixture content number {index} has enough detail.",
                )["data"]["memory_id"]
                for index in range(3)
            ]
            first = tools.search_memory(limit=1)
            second = tools.search_memory(limit=2)
            assert first["data"]["items"][0]["memory_id"] == second["data"]["items"][0]["memory_id"]
            seen = []
            cursor = None
            while True:
                page = tools.search_memory(limit=1, cursor=cursor)
                seen.extend(item["memory_id"] for item in page["data"]["items"])
                if not page["data"]["has_more"]:
                    assert page["data"]["next_cursor"] is None
                    break
                cursor = page["data"]["next_cursor"]
            assert len(seen) == len(stored)
            assert set(seen) == set(stored)
            exact = tools.search_memory(query="Paging fixture 1")
            assert exact["status"] == "ok"
            assert exact["data"]["items"][0]["memory_id"] == stored[1]
        finally:
            mcp_contract.cleanup(connection, previous_backend, previous_db_path)
