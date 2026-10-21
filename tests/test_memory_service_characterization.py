"""Literal contracts for duplicate, lifecycle and tag public service operations."""

import json
import sqlite3
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import numpy as np
import pytest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.schema import init_db
from saltmdb.domain.services import embedding_service, reranker_service
from saltmdb.domain.services.memory_service import duplicates, lifecycle, tags


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "characterization.db"))
    yield conn
    conn.close()


def insert(conn, entity_id="fixed", **values):
    row = {
        "id": entity_id,
        "title": "Needle title",
        "full_content": "Needle body with descriptive information.",
        "created_at": "2001-01-01",
        "updated_at": "2002-01-01",
        "last_accessed_at": "2003-01-01",
        "status": "raw",
        "agent_id": "owner",
        "scope": "private",
    }
    row.update(values)
    conn.execute(
        f"INSERT INTO entities ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    )
    conn.commit()
    return entity_id


def snapshot(conn):
    return conn.execute("SELECT * FROM entities ORDER BY id").fetchall()


def duplicate_row(entity_id, score=4.0, agent_id="owner", scope="private"):
    return {
        "id": entity_id,
        "title": "Needle title",
        "agent_id": agent_id,
        "scope": scope,
        "similarity_score": score,
    }


def test_duplicate_threshold_rounding_stable_order_and_exclusions(db):
    for entity_id in ("z-first", "a-second", "excluded", "below", "highest"):
        insert(db, entity_id)
    model = Mock()
    model.rerank.return_value = [4.0, 4.0, 3.999, 8.12349]
    with patch.object(reranker_service, "get_model", return_value=model):
        result = duplicates.check_duplicate_memories(
            title="Needle", exclude_ids=["", "excluded"], db_connection=db
        )
    assert result == {
        "duplicate_found": True,
        "potential_duplicates": [
            duplicate_row("highest", 8.123),
            duplicate_row("z-first"),
            duplicate_row("a-second"),
        ],
    }


@pytest.mark.parametrize("scalar", [False, True])
def test_duplicate_visibility_context_and_status_filters(db, scalar):
    rows = [
        ("owned", "owner", "private", "context", "raw"),
        ("shared", "other", "shared", None, "raw"),
        ("unowned", None, "private", "context", "consolidated"),
        ("private", "other", "private", "context", "raw"),
        ("wrong-context", "owner", "shared", "elsewhere", "raw"),
        ("archived", "owner", "shared", "context", "archived"),
        ("excluded", "owner", "shared", "context", "raw"),
    ]
    for entity_id, owner, scope, context, status in rows:
        insert(db, entity_id, agent_id=owner, scope=scope, context_id=context, status=status)
    model = Mock()
    model.rerank.side_effect = lambda query, texts: [4.0] * len(texts)
    with patch.object(reranker_service, "get_model", return_value=model):
        if scalar:
            with patch.object(
                duplicates.search_primitives, "_run_fts_search", side_effect=sqlite3.Error("no FTS")
            ):
                result = duplicates.check_duplicate_memories(
                    title="Needle",
                    agent_id="owner",
                    context_id="context",
                    exclude_ids=["excluded"],
                    db_connection=db,
                )
        else:
            result = duplicates.check_duplicate_memories(
                title="Needle",
                agent_id="owner",
                context_id="context",
                exclude_ids=["excluded"],
                db_connection=db,
            )
    ordered = (
        [
            duplicate_row("shared", agent_id="other", scope="shared"),
            duplicate_row("owned"),
            duplicate_row("unowned", agent_id=None),
        ]
        if scalar
        else [
            duplicate_row("owned"),
            duplicate_row("shared", agent_id="other", scope="shared"),
            duplicate_row("unowned", agent_id=None),
        ]
    )
    assert result == {
        "duplicate_found": True,
        "potential_duplicates": ordered,
    }


def test_duplicate_fts_prefilter_uses_or_semantics_and_preserves_candidate_order(db):
    seen = {}
    model = Mock()

    def rerank(query, texts):
        seen["texts"] = list(texts)
        return [4.0]

    model.rerank.side_effect = rerank
    candidate = ("partial", "Alpha only", "alpha", "owner", "private")

    def fake_fts(conn, query, where, params, limit, offset, *, or_only=False, **kwargs):
        seen["or_only"] = or_only
        return [
            (
                candidate[0],
                candidate[1],
                candidate[2],
                "unused",
                "unused",
                "unused",
                "unused",
                "unused",
                candidate[3],
                candidate[4],
            )
        ]

    with (
        patch.object(duplicates.search_primitives, "_run_fts_search", side_effect=fake_fts),
        patch.object(reranker_service, "get_model", return_value=model),
    ):
        result = duplicates.check_duplicate_memories(title="alpha missing", db_connection=db)

    assert seen == {"or_only": True, "texts": ["Alpha only alpha"]}
    assert result == {
        "duplicate_found": True,
        "potential_duplicates": [
            {
                "id": "partial",
                "title": "Alpha only",
                "agent_id": "owner",
                "scope": "private",
                "similarity_score": 4.0,
            }
        ],
    }


def test_duplicate_cross_encoder_caps_candidate_pool_before_model_and_output(db):
    rows = [
        (
            f"id-{index}",
            f"Title {index}",
            f"Body {index}",
            "unused",
            "unused",
            "unused",
            "unused",
            "unused",
            "owner",
            "private",
        )
        for index in range(4)
    ]
    model = Mock()
    model.rerank.return_value = [4.0, 4.0]
    with (
        patch.object(duplicates.search_primitives, "_run_fts_search", return_value=rows),
        patch.object(reranker_service, "get_model", return_value=model),
        patch.object(duplicates, "DEDUP_CROSS_ENCODER_MAX_CANDIDATES", 2),
    ):
        result = duplicates.check_duplicate_memories(title="needle", db_connection=db)

    assert model.rerank.call_args.args[1] == ["Title 0 Body 0", "Title 1 Body 1"]
    assert result == {
        "duplicate_found": True,
        "potential_duplicates": [
            {
                "id": "id-0",
                "title": "Title 0",
                "agent_id": "owner",
                "scope": "private",
                "similarity_score": 4.0,
            },
            {
                "id": "id-1",
                "title": "Title 1",
                "agent_id": "owner",
                "scope": "private",
                "similarity_score": 4.0,
            },
        ],
    }


@pytest.mark.parametrize("scores", [[True], ["4"], [float("nan")], [float("inf")], [], [4, 5]])
def test_malformed_cross_encoder_scores_use_lexical_fallback(db, scores):
    insert(db)
    model = Mock()
    model.rerank.return_value = scores
    with (
        patch.object(reranker_service, "get_model", return_value=model),
        patch("saltmdb.config.is_semantic_search_enabled", return_value=False),
        patch.object(duplicates, "word_sim", return_value=0.4),
    ):
        result = duplicates.check_duplicate_memories(title="Needle", db_connection=db)
    assert result == {
        "duplicate_found": True,
        "potential_duplicates": [duplicate_row("fixed", 0.4)],
    }


@pytest.mark.parametrize(
    "similarity,expected", [(0.4, [duplicate_row("fixed", 0.4)]), (0.3999, [])]
)
def test_lexical_boundary_and_rounding(db, similarity, expected):
    insert(db)
    with (
        patch.object(reranker_service, "get_model", side_effect=RuntimeError("model unavailable")),
        patch("saltmdb.config.is_semantic_search_enabled", return_value=False),
        patch.object(duplicates, "word_sim", return_value=similarity),
    ):
        result = duplicates.check_duplicate_memories(content="Needle", db_connection=db)
    assert result == {"duplicate_found": bool(expected), "potential_duplicates": expected}


@pytest.mark.parametrize(
    "similarity,expected", [(0.75, [duplicate_row("fixed", 0.75)]), (0.7499, [])]
)
def test_cached_semantic_boundary(db, similarity, expected):
    insert(db)
    with (
        patch.object(reranker_service, "get_model", side_effect=RuntimeError("model unavailable")),
        patch("saltmdb.config.is_semantic_search_enabled", return_value=True),
        patch.object(embedding_service, "embed_text", return_value=np.array([1.0, 0.0])),
        patch.object(
            duplicates.search_primitives,
            "_batch_semantic_similarities",
            return_value={"fixed": similarity},
        ),
    ):
        result = duplicates.check_duplicate_memories(title="Needle", db_connection=db)
    assert result == {"duplicate_found": bool(expected), "potential_duplicates": expected}


@pytest.mark.parametrize(
    "vectors,expected",
    [
        ([np.array([1.0, 0.0]), np.array([1.0, 0.0])], [duplicate_row("fixed", 1.0)]),
        ([np.array([0.0, 0.0]), np.array([1.0, 0.0])], []),
        ([np.array([1.0, 0.0]), np.array([0.0, 0.0])], []),
        ([np.array([1.0, 0.0]), RuntimeError("embedding failed")], [duplicate_row("fixed", 0.4)]),
        ([RuntimeError("query failed")], [duplicate_row("fixed", 0.4)]),
        ([None], [duplicate_row("fixed", 0.4)]),
    ],
)
def test_uncached_semantic_and_embedding_failures(db, vectors, expected):
    insert(db)
    with (
        patch.object(reranker_service, "get_model", side_effect=RuntimeError("model unavailable")),
        patch("saltmdb.config.is_semantic_search_enabled", return_value=True),
        patch.object(embedding_service, "embed_text", side_effect=vectors),
        patch.object(duplicates.search_primitives, "_batch_semantic_similarities", return_value={}),
        patch.object(duplicates, "word_sim", return_value=0.4),
    ):
        result = duplicates.check_duplicate_memories(title="Needle", db_connection=db)
    assert result == {"duplicate_found": bool(expected), "potential_duplicates": expected}


def test_duplicate_missing_empty_candidates_and_outer_error(db):
    assert duplicates.check_duplicate_memories(db_connection=db) == {
        "error": "Either title or content is required"
    }
    assert duplicates.check_duplicate_memories(title="Needle", db_connection=db) == {
        "duplicate_found": False,
        "potential_duplicates": [],
    }
    with patch.object(duplicates.search_primitives, "build_fts_terms", return_value=[]):
        assert duplicates.check_duplicate_memories(content=" ! ", db_connection=db) == {
            "duplicate_found": False,
            "potential_duplicates": [],
        }
    with patch.object(
        duplicates.search_primitives, "build_fts_terms", side_effect=RuntimeError("broken terms")
    ):
        assert duplicates.check_duplicate_memories(title="Needle", db_connection=db) == {
            "error": "broken terms"
        }


@pytest.mark.parametrize(
    "overrides,code,message,field",
    [
        ({"entity_id": None}, "MISSING_ENTITY_ID", "entity_id is mandatory.", "entity_id"),
        ({"entity_id": 1}, "MISSING_ENTITY_ID", "entity_id is mandatory.", "entity_id"),
        ({"entity_id": "  "}, "MISSING_ENTITY_ID", "entity_id is mandatory.", "entity_id"),
        ({"title": None}, "MISSING_TITLE", "title is mandatory and cannot be empty.", "title"),
        ({"title": " "}, "MISSING_TITLE", "title is mandatory and cannot be empty.", "title"),
        (
            {"content": None},
            "MISSING_CONTENT",
            "content is mandatory and cannot be empty.",
            "content",
        ),
        (
            {"content": " "},
            "MISSING_CONTENT",
            "content is mandatory and cannot be empty.",
            "content",
        ),
        (
            {"content": "x" * 81},
            "CONTENT_TOO_LONG",
            "content exceeds 80 characters (got 81).",
            "content",
        ),
        ({"reason": None}, "MISSING_REASON", "reason is mandatory and cannot be empty.", "reason"),
        ({"reason": " "}, "MISSING_REASON", "reason is mandatory and cannot be empty.", "reason"),
        (
            {"tags": "tag"},
            "INVALID_TAGS",
            "tags must be a non-empty list of non-empty strings.",
            "tags",
        ),
        (
            {"tags": []},
            "INVALID_TAGS",
            "tags must be a non-empty list of non-empty strings.",
            "tags",
        ),
        (
            {"tags": [1]},
            "INVALID_TAGS",
            "tags must be a non-empty list of non-empty strings.",
            "tags",
        ),
        (
            {"tags": [" "]},
            "INVALID_TAGS",
            "tags must be a non-empty list of non-empty strings.",
            "tags",
        ),
        ({"scope": "bad"}, "INVALID_SCOPE", "scope must be 'private' or 'shared'.", "scope"),
        (
            {"memory_type": "bad"},
            "INVALID_MEMORY_TYPE",
            "memory_type must be one of 'fact', 'event', 'procedure', 'decision', or 'preference'.",
            "memory_type",
        ),
    ],
)
def test_replacement_validation_exact_errors_before_any_write(db, overrides, code, message, field):
    arguments = dict(
        entity_id="missing",
        title="Valid title",
        content="A descriptive replacement body for validation.",
        reason="Correction",
        db_connection=db,
    )
    arguments.update(overrides)
    with patch.object(lifecycle, "MAX_MEMORY_CONTENT_CHARS", 80):
        result = lifecycle.revise_memory(**arguments)
    assert result == {
        "status": "rejected",
        "errors": [{"code": code, "message": message, "field": field}],
        "warnings": [],
    }
    assert snapshot(db) == []


@pytest.mark.parametrize("scope", [None, "private", "shared"])
@pytest.mark.parametrize(
    "memory_type", [None, "fact", "event", "procedure", "decision", "preference"]
)
def test_replacement_validation_accepts_boundary_and_optional_values(db, scope, memory_type):
    with patch.object(lifecycle, "MAX_MEMORY_CONTENT_CHARS", 80):
        result = lifecycle.revise_memory(
            entity_id="missing",
            title="Valid title",
            content="x" * 80,
            reason="Correction",
            tags=["#valid"],
            scope=scope,
            memory_type=memory_type,
            db_connection=db,
        )
    assert result == {
        "status": "rejected",
        "errors": [
            {
                "code": "UNKNOWN_ENTITY_ID",
                "message": "No memory matches entity_id 'missing'.",
                "field": "entity_id",
            }
        ],
        "warnings": [],
    }


@pytest.mark.parametrize("raw", [None, "[]", "null", "7", '{"keep": 1, "nested": {"old": true}}'])
@pytest.mark.parametrize(
    "patch_value,message",
    [
        ({"z": None, "a": {"new": True}}, "Memory 'fixed' metadata updated (2 key(s): a, z)."),
        ({}, "Memory 'fixed' metadata unchanged (empty patch)."),
    ],
)
def test_metadata_fallback_shallow_merge_full_row_and_message(db, raw, patch_value, message):
    insert(
        db,
        metadata=raw,
        is_core=1,
        content_hash="hash",
        parent_ids='["parent"]',
        agent_session_id="created",
    )
    before = snapshot(db)[0]
    frozen = datetime(2020, 1, 2, tzinfo=UTC)
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = frozen
        result = lifecycle.update_memory_metadata(
            "fixed", patch_value, agent_session_id="touched", agent_id="owner", db_connection=db
        )
    assert result == {
        "status": "ok",
        "data": {"id": "fixed", "message": message},
        "warnings": [],
        "errors": [],
    }
    columns = [column[1] for column in db.execute("PRAGMA table_info(entities)")]
    expected = dict(zip(columns, before))
    current = (
        {"keep": 1, "nested": {"old": True}}
        if raw == '{"keep": 1, "nested": {"old": true}}'
        else {}
    )
    current.update(patch_value)
    expected.update(
        metadata=json.dumps(current),
        updated_at="2020-01-02T00:00:00+00:00",
        last_touched_session_id="touched",
    )
    assert snapshot(db) == [tuple(expected[column] for column in columns)]


@pytest.mark.parametrize(
    "owner,scope,caller,allowed",
    [
        ("owner", "private", "owner", True),
        ("owner", "private", None, True),
        ("owner", "private", "other", False),
        ("owner", "shared", "other", False),
        (None, "shared", "other", False),
    ],
)
def test_metadata_owner_gate_even_for_shared_memories(db, owner, scope, caller, allowed):
    insert(db, agent_id=owner, scope=scope)
    before = snapshot(db)
    result = lifecycle.update_memory_metadata(
        "fixed", {"key": 1}, agent_id=caller, db_connection=db
    )
    if allowed:
        assert result == {
            "status": "ok",
            "data": {"id": "fixed", "message": "Memory 'fixed' metadata updated (1 key(s): key)."},
            "warnings": [],
            "errors": [],
        }
    else:
        assert result == {
            "status": "rejected",
            "errors": [
                {
                    "code": "NOT_FOUND",
                    "message": "No memory matches entity_id 'fixed'.",
                    "field": "entity_id",
                }
            ],
            "warnings": [],
        }
        assert snapshot(db) == before


@pytest.mark.parametrize(
    "entity_id,metadata,field,message",
    [
        (None, {}, "entity_id", "entity_id is mandatory."),
        (1, {}, "entity_id", "entity_id is mandatory."),
        ("  ", {}, "entity_id", "entity_id is mandatory."),
        ("fixed", [], "metadata", "metadata must be an object (dict)."),
    ],
)
def test_metadata_validation(db, entity_id, metadata, field, message):
    assert lifecycle.update_memory_metadata(entity_id, metadata, db_connection=db) == {
        "status": "rejected",
        "errors": [{"code": "VALIDATION_ERROR", "message": message, "field": field}],
        "warnings": [],
    }


@pytest.mark.parametrize("truncated", [False, True])
def test_metadata_ambiguity_and_missing(db, truncated):
    candidates = [
        {"id": "first", "title": "First", "status": "raw"},
        {"id": "second", "title": "Second", "status": "archived"},
    ]
    with patch.object(lifecycle, "resolve_entity_ref", return_value=(None, candidates, truncated)):
        result = lifecycle.update_memory_metadata("prefix", {}, db_connection=db)
    error = {
        "code": "AMBIGUOUS_ID_PREFIX",
        "message": "ID prefix 'prefix' matches multiple memories; provide a longer prefix or full UUID.",
        "field": "entity_id",
        "candidates": candidates,
    }
    if truncated:
        error["candidates_truncated"] = True
    assert result == {"status": "rejected", "errors": [error], "warnings": []}
    assert lifecycle.update_memory_metadata("missing", {}, db_connection=db) == {
        "status": "rejected",
        "errors": [
            {
                "code": "NOT_FOUND",
                "message": "No memory matches entity_id 'missing'.",
                "field": "entity_id",
            }
        ],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "mode,message,code",
    [
        ("delete", "Memory 'fixed' not found.", "NOT_FOUND"),
        ("skip", "metadata update for 'fixed' did not complete.", "INTERNAL_ERROR"),
        ("raise", "write unavailable", "INTERNAL_ERROR"),
    ],
)
def test_metadata_write_failure_paths(db, mode, message, code):
    insert(db)

    def transaction(conn, callback):
        if mode == "delete":
            conn.execute("DELETE FROM entities WHERE id = 'fixed'")
            callback(conn)
        elif mode == "raise":
            raise RuntimeError("write unavailable")

    with patch.object(lifecycle, "write_transaction_retrying", side_effect=transaction):
        result = lifecycle.update_memory_metadata("fixed", {}, db_connection=db)
    error = {"code": code, "message": message}
    if mode == "delete":
        error["field"] = "entity_id"
    assert result == {"status": "rejected", "errors": [error], "warnings": []}


@pytest.mark.parametrize("touch", [False, True])
def test_chunk_exact_prefix_archived_empty_body_and_touch(db, touch):
    entity_id = "abcdefab-0000-0000-0000-000000000001"
    insert(db, entity_id, full_content="", status="archived")
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = datetime(2020, 1, 2, tzinfo=UTC)
        assert lifecycle.fetch_memory_chunk(entity_id, db_connection=db, touch=touch) == ""
        assert lifecycle.fetch_memory_chunk("abcdefab", db_connection=db, touch=touch) == ""
    assert db.execute("SELECT last_accessed_at FROM entities").fetchone() == (
        ("2020-01-02T00:00:00+00:00" if touch else "2003-01-01"),
    )


@pytest.mark.parametrize("truncated", [False, True])
def test_chunk_ambiguity_exact_text(db, truncated):
    candidates = [
        {"id": "first", "title": "First", "status": "raw"},
        {"id": "second", "title": "Second", "status": "archived"},
    ]
    with patch.object(lifecycle, "resolve_id_prefix", return_value=(None, candidates, truncated)):
        assert (
            lifecycle.fetch_memory_chunk("prefix", db_connection=db)
            == f"Error: Ambiguous ID prefix 'prefix' matches 2{'+' if truncated else ''} memories:\n  first — 'First' [raw]\n  second — 'Second' [archived]"
        )


def test_chunk_missing_resolution_and_read_failure(db):
    assert lifecycle.fetch_memory_chunk(db_connection=db) == "Error: entity_id is mandatory."
    assert (
        lifecycle.fetch_memory_chunk("missing", db_connection=db)
        == "Memory not found for ID: missing"
    )
    with patch.object(lifecycle, "resolve_entity_id", return_value=None):
        assert (
            lifecycle.fetch_memory_chunk("input", db_connection=db)
            == "Error: Could not resolve memory entity for input 'input'."
        )
    with patch.object(lifecycle, "resolve_id_prefix", return_value=("disappeared", [], False)):
        assert (
            lifecycle.fetch_memory_chunk("prefix", db_connection=db)
            == "Memory not found for ID: prefix"
        )
    with patch.object(lifecycle, "resolve_entity_id", side_effect=RuntimeError("read failed")):
        assert (
            lifecycle.fetch_memory_chunk("input", db_connection=db)
            == "Error fetching memory chunk: read failed"
        )


@pytest.mark.parametrize(
    "parent_json,metadata,flags",
    [(None, None, None), ('["visible", "hidden", "shared"]', '{"key": 1}', '["FLAG"]')],
)
def test_memory_assembly_full_fields_visibility_and_snippet(db, parent_json, metadata, flags):
    insert(db, "visible")
    insert(db, "hidden", agent_id="other")
    insert(db, "shared", agent_id="other", scope="shared")
    insert(
        db,
        parent_ids=parent_json,
        metadata=metadata,
        quality_flags=flags,
        is_core=1,
        valid_from="start",
        valid_to="end",
        context_id="context",
        memory_type="decision",
        quality_score=0.75,
        quality_status="WARN",
        agent_session_id="created",
        last_touched_session_id="touched",
    )
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = datetime(2020, 1, 2, tzinfo=UTC)
        result = lifecycle.get_memory("fixed", db_connection=db, agent_id="owner")
        inspected = lifecycle.inspect_memory("fixed", db_connection=db, agent_id="owner")
    expected = {
        "id": "fixed",
        "title": "Needle title",
        "status": "raw",
        "created_at": "2001-01-01",
        "updated_at": "2002-01-01",
        "last_accessed_at": "2020-01-02T00:00:00+00:00",
        "agent_id": "owner",
        "scope": "private",
        "is_core": True,
        "parent_ids": ["visible", "shared"] if parent_json else [],
        "valid_from": "start",
        "valid_to": "end",
        "metadata": {"key": 1} if metadata else {},
        "context_id": "context",
        "memory_type": "decision",
        "quality_score": 0.75,
        "quality_status": "WARN",
        "quality_flags": ["FLAG"] if flags else [],
        "agent_session_id": "created",
        "last_touched_session_id": "touched",
        "tags": [],
        "content": "Needle body with descriptive information.",
        "lineage": {"ancestors": [], "descendants": []},
    }
    assert result == {"status": "ok", "data": expected, "warnings": [], "errors": []}
    expected.pop("content")
    expected["snippet"] = "Needle body with descriptive information."
    assert inspected == {"status": "ok", "data": expected, "warnings": [], "errors": []}


def test_assembly_unknown_id_returns_none(db):
    assert (
        lifecycle._assemble_memory_record(
            db,
            "missing",
            include_content=True,
            include_lineage=True,
            touch=False,
        )
        is None
    )


def test_get_memory_touch_commits_for_second_connection(tmp_path):
    path = tmp_path / "touch-get.db"
    conn = init_db(str(path))
    insert(conn, last_accessed_at="2003-01-01")
    conn.isolation_level = "DEFERRED"
    observer = sqlite3.connect(path)
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = datetime(2020, 1, 2, tzinfo=UTC)
        lifecycle.get_memory("fixed", db_connection=conn, agent_id="owner")
    assert observer.execute(
        "SELECT last_accessed_at FROM entities WHERE id = 'fixed'"
    ).fetchone() == ("2020-01-02T00:00:00+00:00",)
    observer.close()
    conn.close()


def test_fetch_chunk_touch_commits_and_touch_false_does_not(tmp_path):
    path = tmp_path / "touch-chunk.db"
    conn = init_db(str(path))
    insert(conn, last_accessed_at="2003-01-01")
    conn.isolation_level = "DEFERRED"
    observer = sqlite3.connect(path)
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = datetime(2020, 1, 2, tzinfo=UTC)
        assert lifecycle.fetch_memory_chunk("fixed", db_connection=conn, touch=True).startswith(
            "Needle"
        )
    assert observer.execute(
        "SELECT last_accessed_at FROM entities WHERE id = 'fixed'"
    ).fetchone() == ("2020-01-02T00:00:00+00:00",)
    conn.execute("UPDATE entities SET last_accessed_at = '2003-01-01' WHERE id = 'fixed'")
    conn.commit()
    with patch.object(lifecycle, "datetime") as clock:
        clock.now.return_value = datetime(2021, 1, 2, tzinfo=UTC)
        content = lifecycle.fetch_memory_chunk("fixed", db_connection=conn, touch=False)
        assert content.startswith("Needle")
    assert observer.execute(
        "SELECT last_accessed_at FROM entities WHERE id = 'fixed'"
    ).fetchone() == ("2003-01-01",)
    observer.close()
    conn.close()


@pytest.fixture
def dispatch_db(tmp_path, monkeypatch):
    path = str(tmp_path / "dispatch.db")
    conn = init_db(path)
    insert(conn, "first")
    insert(conn, "second")
    conn.close()
    monkeypatch.setenv("SALTMDB_DB_PATH", path)
    coordinator = DbWriteCoordinator(path)
    coordinator.start()
    yield coordinator
    coordinator.shutdown(2)


def test_bulk_archive_real_dispatch_success_order_and_idempotence(dispatch_db):
    result = dispatch_tool(
        "archive_memory",
        {
            "mode": "bulk",
            "archive_requests": ["second", {"entity_id": "first", "agent_id": "owner"}, "second"],
        },
        dispatch_db,
    )
    assert result == [
        {
            "status": "success",
            "entity_id": "second",
            "result": "Memory 'second' was successfully archived.",
        },
        {
            "status": "success",
            "entity_id": "first",
            "result": "Memory 'first' was successfully archived.",
        },
        {
            "status": "success",
            "entity_id": "second",
            "result": "Memory 'second' is already archived.",
        },
    ]
    assert dispatch_db.submit(
        "read",
        lambda conn: conn.execute(
            "SELECT id, status, embedding_status FROM entities ORDER BY id"
        ).fetchall(),
    ) == [("first", "archived", "archived"), ("second", "archived", "archived")]


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"entity_id": "second", "agent_id": "other"}, "Memory 'second' owner mismatch."),
        ({}, "entity_id parameter is mandatory."),
        ("missing", "Memory 'missing' not found."),
    ],
)
def test_bulk_archive_real_dispatch_failure_preserves_original_coordinator_behavior(
    dispatch_db, bad, message
):
    result = dispatch_tool(
        "archive_memory", {"mode": "bulk", "archive_requests": ["first", bad]}, dispatch_db
    )
    assert result == [
        {"status": "error", "error": f"Bulk archive aborted (all-or-nothing): {message}"}
    ]
    before_rows = dispatch_db.submit(
        "before_rows",
        lambda conn: conn.execute(
            "SELECT id, status, embedding_status FROM entities ORDER BY id"
        ).fetchall(),
    )
    after_rows = dispatch_db.submit(
        "after_rows",
        lambda conn: conn.execute(
            "SELECT id, status, embedding_status FROM entities ORDER BY id"
        ).fetchall(),
    )
    assert after_rows == [
        ("first", "archived", "archived"),
        before_rows[1],
    ]


def test_bulk_archive_direct_mid_batch_failure_rolls_back_all_rows(db):
    insert(db, "first")
    insert(db, "second")
    before = db.execute("SELECT id, status, embedding_status FROM entities ORDER BY id").fetchall()
    result = duplicates.bulk_archive_memory(
        ["first", {"entity_id": "second", "agent_id": "other"}],
        db_connection=db,
    )
    assert result == [
        {
            "status": "error",
            "error": "Bulk archive aborted (all-or-nothing): Memory 'second' owner mismatch.",
        }
    ]
    assert (
        db.execute("SELECT id, status, embedding_status FROM entities ORDER BY id").fetchall()
        == before
    )


def test_bulk_archive_retry_clears_result_accumulator(db):
    insert(db, "first")

    def retry_twice(conn, callback):
        callback(conn)
        conn.execute(
            "UPDATE entities SET status = 'raw', embedding_status = 'pending' WHERE id = 'first'"
        )
        conn.commit()
        callback(conn)

    with patch.object(duplicates, "write_transaction_retrying", side_effect=retry_twice):
        result = duplicates.bulk_archive_memory(["first"], db_connection=db)

    assert result == [
        {
            "status": "success",
            "entity_id": "first",
            "result": "Memory 'first' was successfully archived.",
        }
    ]


@pytest.mark.parametrize("requests", [None, [], {}, "first"])
def test_bulk_archive_invalid_requests(db, requests):
    assert duplicates.bulk_archive_memory(requests, db_connection=db) == [
        {"status": "error", "error": "archive_requests must be a non-empty list"}
    ]


@pytest.mark.parametrize("name", [None, "", " ", "#"])
def test_empty_tags_create_nothing(db, name):
    before = db.execute("SELECT * FROM tags").fetchall()
    assert tags.resolve_or_create_tag(db, name) is None
    assert db.execute("SELECT * FROM tags").fetchall() == before


@pytest.mark.parametrize(
    "name,stored,normalized,events",
    [
        (
            "#Plain",
            "#plain",
            "plain",
            ["Tag name sanitized during resolve_or_create_tag: '#Plain' -> '#plain'"],
        ),
        (
            " a:b ",
            "#a-b",
            "ab",
            ["Tag name sanitized during resolve_or_create_tag: '#a:b' -> '#a-b'"],
        ),
        (
            "#a::b",
            "#ab",
            "ab",
            [
                "Unexpected tag rejection reached resolve_or_create_tag (should have been caught pre-transaction): tag '#a::b' contains adjacent separator characters ('::') -- this usually means a typo; check the intended tag name.",
                "Tag name sanitized during resolve_or_create_tag: '#a::b' -> '#ab'",
            ],
        ),
        (
            "#!!!",
            "#!!!",
            "!!!",
            [
                "Unexpected tag rejection reached resolve_or_create_tag (should have been caught pre-transaction): tag '#!!!' contains adjacent separator characters ('!!!') -- this usually means a typo; check the intended tag name."
            ],
        ),
    ],
)
def test_tag_sanitization_full_row_and_issue_order(db, name, stored, normalized, events):
    tag_id = tags.resolve_or_create_tag(db, name)
    assert tag_id is not None
    full_row = db.execute(
        "SELECT id, name, normalized_name, canonical_id, created_at FROM tags WHERE id = ?",
        (tag_id,),
    ).fetchone()
    assert full_row[:4] == (tag_id, stored, normalized, None)
    assert full_row[4] is not None
    assert db.execute("SELECT agent_id, type, content FROM events ORDER BY rowid").fetchall() == [
        ("system", "issue", text) for text in events
    ]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("#exact", "canonical"),
        ("#Exact", "canonical"),
        ("#normalized", "normal-canonical"),
        ("#computed", "computed"),
        ("#skills", "plural-canonical"),
        ("#books", "books"),
    ],
)
def test_tag_resolution_precedence_aliases_and_legacy_normalization(db, name, expected):
    db.executemany(
        "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, ?)",
        [
            ("canonical", "#target", "target", None),
            ("alias", "#exact", "exact", "canonical"),
            ("normal-canonical", "#normal-target", "normaltarget", None),
            ("normalized", "#legacy", "normalized", "normal-canonical"),
            ("computed", "#com-puted", None, None),
            ("plural-canonical", "#plural-target", "pluraltarget", None),
            ("plural", "#skill", "skill", "plural-canonical"),
            ("books", "#book", None, None),
        ],
    )
    before = db.execute(
        "SELECT id, name, normalized_name, canonical_id FROM tags ORDER BY id"
    ).fetchall()
    assert tags.resolve_or_create_tag(db, name, agent_id="owner") == expected
    assert (
        db.execute(
            "SELECT id, name, normalized_name, canonical_id FROM tags ORDER BY id"
        ).fetchall()
        == before
    )


@pytest.mark.parametrize("existing,incoming", [("#cat", "#cats"), ("#cats", "#cat")])
def test_plural_fallback_length_three_boundary_creates_distinct_tag(db, existing, incoming):
    db.execute(
        "INSERT INTO tags (id, name, normalized_name) VALUES ('existing', ?, ?)",
        (existing, existing[1:]),
    )
    with patch.object(tags.uuid, "uuid4", return_value="new-tag"):
        assert tags.resolve_or_create_tag(db, incoming) == "new-tag"
    assert db.execute(
        "SELECT id, name, normalized_name, canonical_id FROM tags WHERE id = 'new-tag'"
    ).fetchone() == ("new-tag", incoming, incoming[1:], None)


def test_plural_gate_len_four_and_len_three_boundaries(db):
    db.execute(
        "INSERT INTO tags (id, name, normalized_name) VALUES ('plural-existing', '#abcs', 'abcs')"
    )

    class TagGateConnection:
        def __init__(self, real):
            self.real = real

        def execute(self, sql, params=()):
            if "WHERE name = ?" in sql or "normalized_name = ?" in sql:
                return self
            return self.real.execute(sql, params)

        def fetchone(self):
            return None

        def fetchall(self):
            return self.real.execute(
                "SELECT id, name, normalized_name, canonical_id FROM tags"
            ).fetchall()

    proxy = TagGateConnection(db)
    assert tags.resolve_or_create_tag(proxy, "#abcs") == "plural-existing"
    with patch.object(tags.uuid, "uuid4", return_value="new-id"):
        assert tags.resolve_or_create_tag(proxy, "#cat") == "new-id"
