from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from saltmdb.daemon import dispatch
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    SearchCriteria,
    SearchTuning,
    orchestrator,
    search_memory,
)


@pytest.fixture
def search_db(tmp_path: Path):
    path = str(tmp_path / "signature.db")
    conn = init_db(path)
    conn.execute(
        "INSERT INTO entities (id,title,full_content,content_hash,agent_id,scope,status,"
        "memory_type,is_core,created_at,updated_at,last_accessed_at,metadata) "
        "VALUES (?,?,?,?,?,?,?,?,?,'2020','2020',?,?)",
        (
            "e1",
            "Needle title",
            "needle evidence",
            "hash",
            "owner",
            "private",
            "raw",
            "fact",
            1,
            "2020",
            "{}",
        ),
    )
    conn.execute("INSERT INTO tags (id,name) VALUES ('tag','#needle')")
    conn.execute("INSERT INTO entity_tags (entity_id,tag_id) VALUES ('e1','tag')")
    conn.commit()
    yield conn, path
    conn.close()


def test_signature_parameter_limit_and_rejects_removed_names():
    assert len(inspect.signature(search_memory).parameters) == 12
    values = {
        "context_id": "ctx",
        "agent_session_id": "session",
        "tags_filter": ["#tag"],
        "tag_operator": "AND",
        "metadata_filter": {"key": "value"},
        "memory_type_filter": "fact",
        "is_core": True,
        "cross_encoder_candidate_cap": 10,
        "cross_encoder_text_cap_chars": 1000,
        "use_chunk_candidates": True,
        "oversampling_multiplier": 4,
        "candidate_window": 20,
        "chunk_weight": 1.0,
        "use_retrieval_text_candidates": True,
        "retrieval_fts_weight": 1.0,
        "retrieval_vector_weight": 1.0,
        "disable_semantic": True,
        "prefer_durable_types": True,
        "demote_superseded": True,
        "collapse_supersedes_families": True,
    }
    search = cast(Any, search_memory)
    for name, value in values.items():
        with pytest.raises(TypeError):
            search(**{name: value})
    with pytest.raises(TypeError):
        search(None)


def test_group_defaults_match_signature_defaults():
    signature = inspect.signature(search_memory)
    assert signature.parameters["criteria"].default == SearchCriteria()
    assert signature.parameters["tuning"].default == SearchTuning()
    assert signature.parameters["agent_id"].default is None
    assert signature.parameters["query_keywords"].default is None
    assert signature.parameters["explain_mode"].default is False
    assert signature.parameters["limit"].default == 5
    assert signature.parameters["cursor"].default is None
    assert signature.parameters["include_related"].default is True
    assert signature.parameters["return_diagnostics"].default is False
    assert signature.parameters["mode"].default == "broad"
    assert signature.parameters["db_connection"].default is None
    assert signature.parameters["db_path"].default is None


def test_grouped_forms_match_recorded_query_literals(search_db, monkeypatch):
    conn, path = search_db
    monkeypatch.setenv("SALTMDB_ENABLE_SEMANTIC", "true")

    seen = []

    def fake_pool(_conn, filters, controls, _diagnostics, _offset, _limit):
        clauses = tuple(filters.where_clauses)
        seen.append((controls.mode, controls.chunk_window, clauses))
        filtered_request = "e.is_core = ?" in clauses and len(clauses) >= 3
        score = (
            3.0
            if controls.mode == "strict" and controls.chunk_window == 20
            else 2.0
            if filtered_request
            else 1.0
        )
        return {
            "fts_rows": [("e1", 0, 0, 0, 0, "needle evidence")],
            "cross_encoder_scores_map": {},
            "superseded_ids": set(),
            "rrf_score_map": {"e1": score},
            "ordered_ids": ["e1"],
            "exhausted": True,
        }

    monkeypatch.setattr(orchestrator, "_search_window_pool", fake_pool)
    monkeypatch.setattr(orchestrator, "_fetch_search_previews", lambda *args: {})
    expected_plain = [
        {
            "created_at": 2020,
            "cursor": "offset:5",
            "id": "e1",
            "is_core": True,
            "memory_type": "fact",
            "score": 1.0,
            "snippet": "needle evidence",
            "title": "Needle title",
            "trace_evidence_count": 0,
            "weight": 1,
        }
    ]
    expected_filtered = [dict(expected_plain[0], score=2.0)]
    expected_strict = [dict(expected_plain[0], score=3.0)]
    plain = search_memory(
        db_connection=conn,
        db_path=path,
        query_keywords="needle",
        include_related=False,
    )
    filtered = search_memory(
        db_connection=conn,
        db_path=path,
        query_keywords="needle",
        include_related=False,
        criteria=SearchCriteria(tags_filter=["#needle"], tag_operator="AND", is_core=True),
    )
    strict = search_memory(
        db_connection=conn,
        db_path=path,
        query_keywords="needle",
        include_related=False,
        mode="strict",
        tuning=SearchTuning(candidate_window=20),
    )
    assert seen[0][0] == "broad"
    assert seen[1][0] == "broad"
    assert "e.is_core = ?" in seen[1][2]
    assert seen[2][:2] == ("strict", 20)
    assert json.loads(json.dumps(plain)) == expected_plain
    assert json.loads(json.dumps(filtered)) == expected_filtered
    assert json.loads(json.dumps(strict)) == expected_strict


def test_dispatch_groups_one_field_from_each_group():
    with patch("saltmdb.daemon.dispatch.memory_service.search_memory", return_value=[]) as search:
        result = dispatch._dispatch_search_memory(
            agent_id="requester",
            query_keywords=None,
            tags_filter=["#tag"],
            is_core=True,
            disable_semantic=True,
        )
    assert result == []
    kwargs = search.call_args.kwargs
    assert kwargs["agent_id"] == "requester"
    assert kwargs["criteria"] == SearchCriteria(tags_filter=["#tag"], is_core=True)
    assert kwargs["tuning"] == SearchTuning(disable_semantic=True)


def test_dispatch_preserves_tag_operator_in_criteria():
    with patch("saltmdb.daemon.dispatch.memory_service.search_memory", return_value=[]) as search:
        dispatch._dispatch_search_memory(
            query_keywords="needle",
            tags_filter=["#a", "#b"],
            tag_operator="OR",
        )
        assert search.call_args.kwargs["criteria"].tag_operator == "OR"
        dispatch._dispatch_search_memory(query_keywords="needle", tags_filter=["#a", "#b"])
        assert search.call_args.kwargs["criteria"].tag_operator == "AND"


def test_grouped_defaults_are_value_equal():
    assert SearchCriteria() == SearchCriteria(
        context_id=None,
        agent_session_id=None,
        tags_filter=None,
        tag_operator="AND",
        metadata_filter=None,
        memory_type_filter=None,
        is_core=None,
    )
    assert SearchTuning() == SearchTuning(
        cross_encoder_candidate_cap=None,
        cross_encoder_text_cap_chars=None,
        use_chunk_candidates=False,
        oversampling_multiplier=None,
        candidate_window=None,
        chunk_weight=None,
        use_retrieval_text_candidates=False,
        retrieval_fts_weight=None,
        retrieval_vector_weight=None,
        disable_semantic=False,
        prefer_durable_types=False,
        demote_superseded=False,
        collapse_supersedes_families=False,
    )
