"""Replacement outcomes and transaction races through the public lifecycle seams."""

import json
from unittest.mock import patch

import pytest

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import lifecycle


@pytest.fixture
def replacement_db(tmp_path):
    conn = init_db(str(tmp_path / "replacement.db"))
    yield conn
    conn.close()


def seed(conn, title="Original characterization memory"):
    from saltmdb.domain.services.memory_service import store_memory

    result = store_memory(
        title=title,
        content=f"A complete descriptive original memory for lifecycle characterization: {title}.",
        tags=["#original"],
        agent_id="original-owner",
        context_id="original-context",
        scope="private",
        memory_type="decision",
        metadata={"source": "original"},
        db_connection=conn,
    )
    assert result["status"] == "ok"
    return result["data"]["id"]


def replace(conn, old_id, **overrides):
    arguments = dict(
        entity_id=old_id,
        title="  Corrected characterization memory  ",
        content="A complete descriptive corrected memory for lifecycle characterization.",
        reason="  Corrected representation.  ",
        db_connection=conn,
    )
    arguments.update(overrides)
    return lifecycle.revise_memory(**arguments)


@pytest.mark.parametrize("truncated", [False, True])
def test_ambiguous_reference_preserves_candidate_payload(replacement_db, truncated):
    candidates = [{"id": "fixed-first"}, {"id": "fixed-second"}]
    with patch.object(lifecycle, "resolve_entity_ref", return_value=(None, candidates, truncated)):
        result = replace(replacement_db, "ambiguous")
    expected = {
        "code": "AMBIGUOUS_ID_PREFIX",
        "message": "ID prefix 'ambiguous' matches multiple memories; provide a longer prefix or full UUID.",
        "field": "entity_id",
        "candidates": candidates,
    }
    if truncated:
        expected["candidates_truncated"] = True
    assert result["errors"] == [expected]
    assert result["status"] == "rejected"


@pytest.mark.parametrize("resolved", [None, "missing-resolved-id"])
def test_unknown_reference_rejects_without_writes(replacement_db, resolved):
    with patch.object(lifecycle, "resolve_entity_ref", return_value=(resolved, [], False)):
        result = replace(replacement_db, "missing")
    assert result["errors"] == [
        {
            "code": "UNKNOWN_ENTITY_ID",
            "message": "No memory matches entity_id 'missing'.",
            "field": "entity_id",
        }
    ]
    assert replacement_db.execute("SELECT id FROM entities").fetchall() == []


def test_disappearing_preflight_snapshot_rejects(replacement_db):
    old_id = seed(replacement_db)
    with patch.object(lifecycle, "_replacement_snapshot", return_value=([], {})):
        result = replace(replacement_db, old_id)
    assert result["errors"][0]["code"] == "UNKNOWN_ENTITY_ID"
    assert replacement_db.execute("SELECT id, status FROM entities").fetchall() == [(old_id, "raw")]


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"title": ""}, "MISSING_TITLE"),
        ({"title": "x"}, "INVALID_MEMORY"),
        ({"content": "tiny"}, "MEMORY_QUALITY_REJECTED"),
    ],
)
def test_validation_returns_before_any_replacement(replacement_db, overrides, code):
    old_id = seed(replacement_db)
    result = replace(replacement_db, old_id, **overrides)
    assert result["errors"][0]["code"] == code
    assert replacement_db.execute("SELECT id, status FROM entities").fetchall() == [(old_id, "raw")]


def test_inheritance_and_explicit_overrides_preserve_field_order(replacement_db):
    old_id = seed(replacement_db)
    first = replace(replacement_db, old_id)["data"]
    assert first["inherited"] == {
        "tags": ["#original"],
        "agent_id": "original-owner",
        "context_id": "original-context",
        "scope": "private",
        "memory_type": "decision",
    }
    assert first["inherited_fields"] == ["tags", "agent_id", "context_id", "scope", "memory_type"]
    assert first["changed_fields"] == ["title", "reason", "content"]
    assert first["changed"]["title"] == "Corrected characterization memory"
    assert first["reason"] == "Corrected representation."
    second = replace(
        replacement_db,
        first["new_id"],
        tags=["#explicit"],
        agent_id="new-owner",
        context_id="new-context",
        scope="shared",
        memory_type="fact",
        metadata={"source": "new"},
    )["data"]
    assert second["inherited"] == {}
    assert second["changed_fields"] == [
        "title",
        "reason",
        "tags",
        "agent_id",
        "context_id",
        "scope",
        "memory_type",
        "metadata",
        "content",
    ]
    assert second["changed"]["metadata"] == json.dumps({"source": "new"})
    assert replacement_db.execute(
        "SELECT agent_id, context_id, scope, memory_type, metadata FROM entities WHERE id = ?",
        (second["new_id"],),
    ).fetchone() == ("new-owner", "new-context", "shared", "fact", '{"source": "new"}')


@pytest.mark.parametrize(
    "column",
    [
        "title",
        "full_content",
        "agent_id",
        "context_id",
        "scope",
        "memory_type",
        "created_at",
        "content_hash",
        "metadata",
        "parent_ids",
        "valid_from",
    ],
)
def test_frozen_column_race_aborts_atomically(replacement_db, column):
    old_id = seed(replacement_db)
    original_snapshot = lifecycle._replacement_snapshot
    calls = 0

    def snapshot(conn, entity_id):
        nonlocal calls
        columns, before = original_snapshot(conn, entity_id)
        calls += 1
        if calls == 2:
            before[column] = "concurrent-change"
        return columns, before

    with patch.object(lifecycle, "_replacement_snapshot", side_effect=snapshot):
        result = replace(replacement_db, old_id)
    assert result["errors"] == [
        {
            "code": "TARGET_CHANGED",
            "message": "Target changed while replacement was being prepared; retry after fetching it again.",
            "field": "entity_id",
        }
    ]
    assert replacement_db.execute("SELECT id, status FROM entities").fetchall() == [(old_id, "raw")]
    assert replacement_db.execute("SELECT id FROM relations").fetchall() == []


@pytest.mark.parametrize("status", [None, "archived", "consolidated"])
def test_target_disappears_or_becomes_inactive_under_lock(replacement_db, status):
    old_id = seed(replacement_db)

    def concurrent_write(conn, callback):
        if status is None:
            conn.execute("DELETE FROM entities WHERE id = ?", (old_id,))
        else:
            conn.execute("UPDATE entities SET status = ? WHERE id = ?", (status, old_id))
        return callback(conn)

    with patch.object(lifecycle, "write_transaction_retrying", side_effect=concurrent_write):
        result = replace(replacement_db, old_id)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == (
        "UNKNOWN_ENTITY_ID" if status is None else "INACTIVE_TARGET"
    )
    if status is not None:
        assert result["effective"] == {
            "target_id": old_id,
            "status": status,
            "active_successors": [],
            "lineage": {"ancestors": [], "descendants": []},
        }
    assert replacement_db.execute("SELECT id FROM relations").fetchall() == []


def test_existing_transaction_keeps_control_with_caller(replacement_db):
    old_id = seed(replacement_db)
    replacement_db.execute("BEGIN IMMEDIATE")
    result = replace(replacement_db, old_id, _in_transaction=True)
    assert result["status"] == "ok"
    replacement_db.rollback()
    assert replacement_db.execute("SELECT id, status FROM entities").fetchall() == [(old_id, "raw")]


def test_write_failure_returns_envelope_and_connection_stays_usable(replacement_db):
    old_id = seed(replacement_db)
    with patch.object(
        lifecycle, "write_transaction_retrying", side_effect=RuntimeError("write failed")
    ):
        result = replace(replacement_db, old_id)
    assert result["errors"] == [{"code": "LIFECYCLE_WRITE_FAILED", "message": "write failed"}]
    assert replacement_db.execute("SELECT id, status FROM entities").fetchall() == [(old_id, "raw")]


def test_owned_connection_is_closed_after_replacement(tmp_path):
    path = str(tmp_path / "owned.db")
    conn = init_db(path)
    old_id = seed(conn)
    conn.close()
    real_close = lifecycle.close_connection
    with patch.object(lifecycle, "close_connection", wraps=real_close) as close:
        result = lifecycle.supersede_memory(
            entity_id=old_id,
            title="New owned connection memory",
            content="A complete descriptive replacement using its own connection.",
            reason="New facts.",
            db_path=path,
        )
    assert result["status"] == "ok"
    close.assert_called_once()


def test_replacement_redacts_title_and_content_at_public_seam(replacement_db):
    old_id = seed(replacement_db)
    token = "sk-" + "a" * 48
    result = replace(
        replacement_db,
        old_id,
        title=f"Rotated secret {token}",
        content=f"A complete replacement body containing secret {token} for redaction.",
    )
    assert result["status"] == "ok"
    new_id = result["data"]["new_id"]
    stored_title, stored_content = replacement_db.execute(
        "SELECT title, full_content FROM entities WHERE id = ?", (new_id,)
    ).fetchone()
    assert token not in stored_title
    assert token not in stored_content
    assert stored_title == "Rotated secret [REDACTED_SECRET]"
    assert result["data"]["changed"]["title"] == stored_title
    assert result["data"]["changed"]["content"] == stored_content


def test_supersede_public_seam_redacts_secret_content(replacement_db):
    old_id = seed(replacement_db)
    token = "ghp_" + "b" * 36
    result = lifecycle.supersede_memory(
        entity_id=old_id,
        title="Superseded secret content",
        content=f"A complete superseding body containing token {token}.",
        reason="The prior value was replaced.",
        db_connection=replacement_db,
    )
    assert result["status"] == "ok"
    new_id = result["data"]["new_id"]
    stored_content = replacement_db.execute(
        "SELECT full_content FROM entities WHERE id = ?", (new_id,)
    ).fetchone()[0]
    assert token not in stored_content
    assert "[REDACTED_SECRET]" in stored_content


def test_empty_memory_type_inherits_fact(replacement_db):
    old_id = seed(replacement_db)
    replacement_db.execute("UPDATE entities SET memory_type = NULL WHERE id = ?", (old_id,))
    replacement_db.commit()
    result = replace(replacement_db, old_id)
    assert result["status"] == "ok"
    assert result["data"]["inherited"]["memory_type"] == "fact"
    assert replacement_db.execute(
        "SELECT memory_type FROM entities WHERE id = ?", (result["data"]["new_id"],)
    ).fetchone() == ("fact",)


def test_inherited_tags_are_written_to_successor_exactly(replacement_db):
    old_id = seed(replacement_db)
    result = replace(replacement_db, old_id)
    assert result["status"] == "ok"
    new_tags = replacement_db.execute(
        """
        SELECT t.name
        FROM tags t JOIN entity_tags et ON et.tag_id = t.id
        WHERE et.entity_id = ?
        ORDER BY t.name
        """,
        (result["data"]["new_id"],),
    ).fetchall()
    assert new_tags == [("#original",)]


def test_none_tag_lookup_falls_back_to_empty_successor_tags(replacement_db):
    old_id = seed(replacement_db)
    with patch(
        "saltmdb.domain.services.memory_service.tags.list_entity_tags",
        return_value=None,
    ):
        result = replace(replacement_db, old_id)
    assert result["status"] == "ok"
    assert replacement_db.execute(
        "SELECT COUNT(*) FROM entity_tags WHERE entity_id = ?", (result["data"]["new_id"],)
    ).fetchone() == (0,)


def test_successor_lifecycle_columns_and_quality_are_pinned(replacement_db):
    old_id = seed(replacement_db)
    quality = {
        "status": "WARN",
        "quality_score": 0.75,
        "quality_flags": ["TEST_FLAG"],
    }
    with (
        patch.object(lifecycle, "evaluate_memory_quality", return_value=quality),
        patch(
            "saltmdb.domain.services.embedding_service.cancel_embedding_jobs_for_entity",
            return_value=None,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            return_value=None,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.cancel_retrieval_embedding_jobs_for_entity",
            return_value=None,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity",
            return_value=None,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.enqueue_retrieval_embedding_job_for_entity",
            return_value=None,
        ),
    ):
        result = replace(replacement_db, old_id, agent_session_id="session-123")
    assert result["status"] == "ok"
    new_id = result["data"]["new_id"]
    new_row = replacement_db.execute(
        """
        SELECT created_at, updated_at, last_accessed_at, valid_from, valid_to,
               agent_session_id, last_touched_session_id, quality_score,
               quality_status, quality_flags, embedding_status
        FROM entities WHERE id = ?
        """,
        (new_id,),
    ).fetchone()
    old_row = replacement_db.execute(
        "SELECT updated_at, valid_to, embedding_status FROM entities WHERE id = ?", (old_id,)
    ).fetchone()
    created_at, updated_at, last_accessed_at, valid_from, valid_to = new_row[:5]
    assert created_at == updated_at == last_accessed_at == valid_from
    assert valid_to is None
    assert new_row[5:11] == (
        "session-123",
        "session-123",
        0.75,
        "WARN",
        '["TEST_FLAG"]',
        "pending",
    )
    assert old_row == (created_at, created_at, "archived")


def _insert_relation(conn, relation_id, source_id, target_id, predicate="depends_on"):
    conn.execute(
        "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
        (relation_id, source_id, target_id, predicate),
    )


def test_repointed_relations_follow_relation_id_order(replacement_db):
    old_id = seed(replacement_db)
    neighbors = [seed(replacement_db, f"Neighbor {index}") for index in range(3)]
    for relation_id, neighbor_id in zip(("edge-c", "edge-a", "edge-b"), neighbors):
        _insert_relation(replacement_db, relation_id, old_id, neighbor_id)
    replacement_db.commit()
    result = replace(replacement_db, old_id, repoint_relations=True)
    assert result["status"] == "ok"
    assert [item["old_relation_id"] for item in result["data"]["repointed_relations"]] == [
        "edge-a",
        "edge-b",
        "edge-c",
    ]


def test_orphaned_edges_follow_relation_id_order(replacement_db):
    old_id = seed(replacement_db)
    neighbors = [seed(replacement_db, f"Neighbor {index}") for index in range(3)]
    for relation_id, neighbor_id in zip(("edge-c", "edge-a", "edge-b"), neighbors):
        _insert_relation(replacement_db, relation_id, old_id, neighbor_id)
    replacement_db.commit()
    result = replace(replacement_db, old_id)
    assert result["status"] == "ok"
    assert [item["relation_id"] for item in result["data"]["orphaned_semantic_edges"]] == [
        "edge-a",
        "edge-b",
        "edge-c",
    ]


def test_inactive_target_message_is_exact(replacement_db):
    old_id = seed(replacement_db)
    first = replace(replacement_db, old_id)
    assert first["status"] == "ok"
    result = lifecycle.supersede_memory(
        entity_id=old_id,
        title="Inactive target",
        content="A complete replacement body that must not be written.",
        reason="The target is already archived.",
        db_connection=replacement_db,
    )
    assert result["errors"][0]["message"] == (
        f"Memory '{old_id}' is inactive (archived). Fetch an active successor and inspect it "
        "before retrying; no replacement was created. To correct stale/wrong information on an "
        "inactive memory without reviving it, store a new store_memory fact with the correction, "
        f"then manage_relation(predicate='corrects', source_id=<new memory>, target_id='{old_id}') "
        "-- no revise_memory/supersede_memory call is possible on an inactive target."
    )


def test_lock_time_inactive_message_is_exact(replacement_db):
    old_id = seed(replacement_db)

    def concurrent_archive(conn, callback):
        conn.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (old_id,))
        return callback(conn)

    with patch.object(lifecycle, "write_transaction_retrying", side_effect=concurrent_archive):
        result = replace(replacement_db, old_id)
    assert result["errors"][0]["message"] == (
        f"Memory '{old_id}' became inactive (archived) before replacement; retry against an "
        "active successor. To correct stale/wrong information on an inactive memory without "
        "reviving it, store a new store_memory fact with the correction, then "
        f"manage_relation(predicate='corrects', source_id=<new memory>, target_id='{old_id}') "
        "-- no revise_memory/supersede_memory call is possible on an inactive target."
    )


def test_quality_rejection_message_formats_score_to_two_decimals(replacement_db):
    old_id = seed(replacement_db)
    with patch.object(
        lifecycle,
        "evaluate_memory_quality",
        return_value={"status": "REJECT", "quality_score": 0.5, "reason": "bad quality"},
    ):
        result = replace(replacement_db, old_id)
    assert result["errors"] == [
        {
            "code": "MEMORY_QUALITY_REJECTED",
            "message": "Memory quality check rejected (Score: 0.50). Reason: bad quality",
        }
    ]


def test_none_metadata_inherits_predecessor_metadata(replacement_db):
    old_id = seed(replacement_db)
    old_metadata = replacement_db.execute(
        "SELECT metadata FROM entities WHERE id = ?", (old_id,)
    ).fetchone()[0]
    result = replace(replacement_db, old_id)
    assert result["status"] == "ok"
    assert "metadata" not in result["data"]["inherited"]
    assert "metadata" not in result["data"]["changed"]
    new_metadata = replacement_db.execute(
        "SELECT metadata FROM entities WHERE id = ?", (result["data"]["new_id"],)
    ).fetchone()[0]
    assert new_metadata == old_metadata == '{"source": "original"}'
