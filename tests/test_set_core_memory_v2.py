from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
import pytest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.schema import init_db
from saltmdb.domain.services import core_governance_service as cgs
from saltmdb.domain.services.memory_service.write import store_memory

REASON = "A" * 20
EXIT = "B" * 20
RATIONALE = "C" * 20


@pytest.fixture(params=["direct", "coordinator"])
def harness(tmp_path, request, monkeypatch):
    db_path = str(tmp_path / "set-core.db")
    connection = init_db(db_path)
    monkeypatch.setattr(
        "saltmdb.domain.services.embedding_service.enqueue_embedding_jobs_for_entity",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "saltmdb.domain.services.librarian_service.trigger_librarian",
        lambda *_args, **_kwargs: None,
    )
    coordinator = None
    if request.param == "coordinator":
        coordinator = DbWriteCoordinator(db_path)
        coordinator.start()
    yield db_path, connection, coordinator, request.param
    if coordinator is not None:
        coordinator.shutdown(timeout=5)
    connection.close()


def _store(connection, title, **options: Any):
    agent_id = options.pop("agent_id", "owner")
    scope = options.pop("scope", "shared")
    content = options.pop("content", None)
    retrieval_text = options.pop("retrieval_text", None)
    tags = options.pop("tags", None)
    kwargs: dict[str, Any] = {
        "title": title,
        "content": content
        or f"Durable content for {title}. This body is long enough to satisfy the repository quality gate and remains stable for assertions.",
        "agent_id": agent_id,
        "scope": scope,
        "tags": tags,
        "db_connection": connection,
    }
    if retrieval_text is not None:
        kwargs["retrieval_text"] = retrieval_text
    result = store_memory(**kwargs)
    assert result["status"] == "ok", result
    return result["data"]["id"]


def _store_core(connection, title, *, agent_id="owner"):
    result = store_memory(
        title=title,
        content=f"Durable core content for {title}. This body is long enough to satisfy the repository quality gate and remains stable for assertions.",
        agent_id=agent_id,
        scope="shared",
        is_core=True,
        core_reason=REASON,
        core_exit_condition=EXIT,
        db_connection=connection,
    )
    assert result["status"] == "ok", result
    return result["data"]["id"]


def _call(connection, coordinator, **options: Any):
    memory_id = options.pop("memory_id")
    agent_id = options.pop("agent_id", "owner")
    reason = options.pop("reason", REASON)
    exit_condition = options.pop("exit_condition", EXIT)
    detail_memory_ids = options.pop("detail_memory_ids", None)
    include_details = options.pop("include_details", False)
    kwargs: dict[str, Any] = {
        "entity_id": memory_id,
        "agent_id": agent_id,
        "core_reason": reason,
        "core_exit_condition": exit_condition,
    }
    if include_details:
        kwargs["detail_memory_ids"] = detail_memory_ids
    if coordinator is None:
        return cgs.set_core_memory(connection, **kwargs)
    return dispatch_tool("set_core_memory", kwargs, coordinator)


def _snapshot(connection):
    tables = (
        "entities",
        "relations",
        "entity_tags",
        "events",
        "embedding_jobs",
        "retrieval_embedding_jobs",
    )
    return {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in tables
    }


def test_promotes_own_shared_memory_and_preserves_identity(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store(connection, "Promotion Target")
    before = connection.execute(
        "SELECT updated_at, title, full_content, is_core FROM entities WHERE id = ?", (memory_id,)
    ).fetchone()
    result = _call(connection, coordinator, memory_id=memory_id)
    assert result["status"] == "ok", result
    after = connection.execute(
        "SELECT updated_at, title, full_content, is_core, core_reason, core_exit_condition, core_review_after, core_detail_memory_ids FROM entities WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert after[1:4] == (before[1], before[2], 1)
    assert after[4:6] == (REASON, EXIT)
    assert (
        13.5 < (datetime.fromisoformat(after[6]) - datetime.now(UTC)).total_seconds() / 86400 < 14.5
    )
    assert after[7] is None
    assert after[0] != before[0]
    assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert connection.execute(
        "SELECT 1 FROM entity_tags et JOIN tags t ON t.id = et.tag_id WHERE et.entity_id = ? AND t.name = '#core'",
        (memory_id,),
    ).fetchone()


def test_edit_keeps_review_date_and_bumps_timestamp(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store_core(connection, "Edit Target")
    before = connection.execute(
        "SELECT updated_at, core_review_after FROM entities WHERE id = ?", (memory_id,)
    ).fetchone()
    result = _call(
        connection, coordinator, memory_id=memory_id, reason="D" * 20, exit_condition="E" * 20
    )
    assert result["status"] == "ok", result
    after = connection.execute(
        "SELECT updated_at, core_reason, core_exit_condition, core_review_after FROM entities WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert after[1:3] == ("D" * 20, "E" * 20)
    assert after[3] == before[1]
    assert after[0] != before[0]


def test_identical_repeat_is_noop_before_admission(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store_core(connection, "Repeat Target")
    before = connection.execute(
        "SELECT updated_at FROM entities WHERE id = ?", (memory_id,)
    ).fetchone()[0]
    result = _call(connection, coordinator, memory_id=memory_id)
    assert result["status"] == "ok"
    assert result["warnings"][0]["code"] == "ALREADY_DONE"
    assert (
        connection.execute("SELECT updated_at FROM entities WHERE id = ?", (memory_id,)).fetchone()[
            0
        ]
        == before
    )


def test_detail_declaration_set_omitted_kept_and_empty_clears(harness):
    _db_path, connection, coordinator, _mode = harness
    detail_text = "Core governance detail explains the same bootstrap hazard and review boundary."
    detail_id = _store(connection, "Core Governance Detail", content=detail_text)
    core_id = _store(
        connection,
        "Core With Detail",
        content=f"{detail_text}\n\nThe detail title is Core Governance Detail and UUID {detail_id}.",
    )
    result = _call(
        connection,
        coordinator,
        memory_id=core_id,
        detail_memory_ids=[detail_id],
        include_details=True,
    )
    assert result["status"] == "ok", result
    assert connection.execute(
        "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
    ).fetchone()[0] == json.dumps([detail_id])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = 'elaborates_on' AND valid_to IS NULL",
            (detail_id, core_id),
        ).fetchone()[0]
        == 1
    )
    assert _call(connection, coordinator, memory_id=core_id)["status"] == "ok"
    cleared = _call(
        connection, coordinator, memory_id=core_id, detail_memory_ids=[], include_details=True
    )
    assert cleared["status"] == "ok"
    assert (
        connection.execute(
            "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
        ).fetchone()[0]
        is None
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = 'elaborates_on' AND valid_to IS NULL",
            (detail_id, core_id),
        ).fetchone()[0]
        == 0
    )


def test_demote_then_promote_omitted_details_clears_stale_declaration(harness):
    _db_path, connection, coordinator, _mode = harness
    detail_text = (
        "Stale core governance detail explains the same bootstrap hazard and review boundary."
    )
    detail_id = _store(connection, "Stale Core Governance Detail", content=detail_text)
    core_id = _store(
        connection,
        "Stale Core",
        content=f"{detail_text}\n\nThe detail title is Stale Core Governance Detail and UUID {detail_id}.",
    )
    assert (
        _call(
            connection,
            coordinator,
            memory_id=core_id,
            detail_memory_ids=[detail_id],
            include_details=True,
        )["status"]
        == "ok"
    )
    assert (
        cgs.review_core_memory(
            connection,
            entity_id=core_id,
            outcome="demote",
            review_rationale=RATIONALE,
            agent_id="reviewer",
        )["status"]
        == "ok"
    )
    promoted = _call(connection, coordinator, memory_id=core_id)
    assert promoted["status"] == "ok", promoted
    assert (
        connection.execute(
            "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
        ).fetchone()[0]
        is None
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = 'elaborates_on' AND valid_to IS NULL",
            (detail_id, core_id),
        ).fetchone()[0]
        == 0
    )


def test_foreign_shared_is_immutable_private_is_not_found_and_unknown_is_not_found(harness):
    _db_path, connection, coordinator, _mode = harness
    shared_id = _store(connection, "Foreign Shared", agent_id="other")
    private_id = _store(connection, "Foreign Private", agent_id="other", scope="private")
    for memory_id, code in (
        (shared_id, "IMMUTABLE_MEMORY"),
        (private_id, "NOT_FOUND"),
        ("missing", "NOT_FOUND"),
    ):
        before = _snapshot(connection)
        result = _call(connection, coordinator, memory_id=memory_id)
        assert result["status"] == "rejected"
        assert result["errors"][0]["code"] == code
        assert _snapshot(connection) == before


def test_archived_status_and_self_detail_are_zero_write_rejections(harness):
    _db_path, connection, coordinator, _mode = harness
    archived = _store(connection, "Archived Target")
    connection.execute("UPDATE entities SET status = 'archived' WHERE id = ?", (archived,))
    before = _snapshot(connection)
    result = _call(connection, coordinator, memory_id=archived)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CONFLICT"
    assert _snapshot(connection) == before
    raw = _store(connection, "Self Detail Target")
    before = _snapshot(connection)
    result = _call(
        connection, coordinator, memory_id=raw, detail_memory_ids=[raw], include_details=True
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "core"
    assert _snapshot(connection) == before


def test_overdue_new_promotion_rejected_but_identical_repeat_allowed(harness):
    _db_path, connection, coordinator, _mode = harness
    existing = _store_core(connection, "Overdue Existing")
    connection.execute(
        "UPDATE entities SET core_review_after = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(), existing),
    )
    target = _store(connection, "Overdue New")
    before = _snapshot(connection)
    rejected = _call(connection, coordinator, memory_id=target)
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["field"] == "core"
    assert _snapshot(connection) == before
    repeat = _call(connection, coordinator, memory_id=existing)
    assert repeat["status"] == "ok"
    assert repeat["warnings"][0]["code"] == "ALREADY_DONE"


def test_capacity_rejects_sixth_core_and_allows_existing_edit(harness):
    _db_path, connection, coordinator, _mode = harness
    ids = [_store_core(connection, f"Capacity Core {index}") for index in range(5)]
    target = _store(connection, "Sixth Core")
    before = _snapshot(connection)
    rejected = _call(connection, coordinator, memory_id=target)
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "CORE_CAPACITY_EXCEEDED"
    assert _snapshot(connection) == before
    edited = _call(
        connection, coordinator, memory_id=ids[0], reason="D" * 20, exit_condition="E" * 20
    )
    assert edited["status"] == "ok", edited


def test_reconcile_failure_rolls_back_direct_and_coordinator(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store(connection, "Atomic Target")
    before = _snapshot(connection)
    with patch.object(
        cgs, "reconcile_detail_relations", side_effect=RuntimeError("injected reconcile failure")
    ):
        with pytest.raises(RuntimeError, match="injected reconcile failure"):
            _call(connection, coordinator, memory_id=memory_id)
    assert _snapshot(connection) == before


def test_private_content_short_reason_and_bad_detail_are_zero_write_rejections(harness):
    _db_path, connection, coordinator, _mode = harness
    private_id = _store(
        connection,
        "Private Target",
        scope="private",
        content="# Private Target\n\nThis private memory has valid quality content for scope testing.",
    )
    oversized_id = _store(connection, "Oversized Target")
    connection.execute(
        "UPDATE entities SET full_content = ? WHERE id = ?", ("x" * 2501, oversized_id)
    )
    cases = (
        (private_id, REASON, EXIT, "core"),
        (oversized_id, REASON, EXIT, "core"),
        (private_id, "short", EXIT, "core"),
    )
    for memory_id, reason, exit_condition, field in cases:
        before = _snapshot(connection)
        result = _call(
            connection,
            coordinator,
            memory_id=memory_id,
            reason=reason,
            exit_condition=exit_condition,
        )
        assert result["status"] == "rejected"
        assert result["errors"][0]["field"] == field
        assert _snapshot(connection) == before
    before = _snapshot(connection)
    result = _call(
        connection,
        coordinator,
        memory_id=private_id,
        detail_memory_ids=["not-a-uuid"],
        include_details=True,
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "core"
    assert _snapshot(connection) == before


def test_over_2500_core_repeat_and_overdue_enlargement_are_rejected(harness):
    _db_path, connection, coordinator, _mode = harness
    core_id = _store_core(connection, "Oversized Existing")
    connection.execute(
        "UPDATE entities SET full_content = ?, updated_at = ? WHERE id = ?",
        ("x" * 2501, datetime.now(UTC).isoformat(), core_id),
    )
    before = _snapshot(connection)
    result = _call(connection, coordinator, memory_id=core_id)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "core"
    assert _snapshot(connection) == before
    normal_id = _store_core(connection, "Enlargement Existing")
    connection.execute(
        "UPDATE entities SET core_review_after = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(), normal_id),
    )
    before = _snapshot(connection)
    result = _call(connection, coordinator, memory_id=normal_id, reason="D" * 21)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "core"
    assert _snapshot(connection) == before


def test_dispatcher_rejects_review_date_and_preserves_state(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store(connection, "Dispatcher Date Target")
    before = _snapshot(connection)
    kwargs = {
        "entity_id": memory_id,
        "agent_id": "owner",
        "core_reason": REASON,
        "core_exit_condition": EXIT,
        "core_review_after": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    if coordinator is None:
        from saltmdb.daemon import dispatch

        result = dispatch.DISPATCH_TABLE["set_core_memory"](**kwargs)
    else:
        result = dispatch_tool("set_core_memory", kwargs, coordinator)
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "core_review_after"
    assert _snapshot(connection) == before


def test_promotion_leaves_non_core_fields_untouched(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store(
        connection, "Untouched Fields", retrieval_text="retrieval stays", tags=["#keep"]
    )
    before = connection.execute(
        "SELECT retrieval_text, scope, core_last_reviewed_at, core_last_reviewed_by FROM entities WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert _call(connection, coordinator, memory_id=memory_id)["status"] == "ok"
    after = connection.execute(
        "SELECT retrieval_text, scope, core_last_reviewed_at, core_last_reviewed_by FROM entities WHERE id = ?",
        (memory_id,),
    ).fetchone()
    assert after == before
    assert connection.execute(
        "SELECT 1 FROM entity_tags et JOIN tags t ON t.id = et.tag_id WHERE et.entity_id = ? AND t.name = '#keep'",
        (memory_id,),
    ).fetchone()


def test_same_detail_ids_in_different_order_are_an_edit(harness):
    _db_path, connection, coordinator, _mode = harness
    detail_text = "Shared core governance detail explains the bootstrap hazard and review boundary."
    first_text = f"{detail_text} First detail evidence."
    second_text = f"{detail_text} Second detail evidence."
    first_id = _store(connection, "First Governance Detail", content=first_text)
    second_id = _store(connection, "Second Governance Detail", content=second_text)
    core_id = _store(
        connection,
        "Ordered Core",
        content=f"{detail_text}\n\nThe details are First Governance Detail UUID {first_id} and Second Governance Detail UUID {second_id}.",
    )
    first = _call(
        connection,
        coordinator,
        memory_id=core_id,
        detail_memory_ids=[first_id, second_id],
        include_details=True,
    )
    assert first["status"] == "ok", first
    before = connection.execute(
        "SELECT updated_at FROM entities WHERE id = ?", (core_id,)
    ).fetchone()[0]
    second = _call(
        connection,
        coordinator,
        memory_id=core_id,
        detail_memory_ids=[second_id, first_id],
        include_details=True,
    )
    assert second["status"] == "ok", second
    assert second["data"]["message"].startswith(f"Core declaration of '{core_id}' updated")
    assert connection.execute(
        "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
    ).fetchone()[0] == json.dumps([second_id, first_id])
    assert (
        connection.execute("SELECT updated_at FROM entities WHERE id = ?", (core_id,)).fetchone()[0]
        != before
    )


def test_demote_then_promote_with_explicit_details_recreates_declaration(harness):
    _db_path, connection, coordinator, _mode = harness
    detail_text = (
        "Explicit core governance detail explains the bootstrap hazard and review boundary."
    )
    detail_id = _store(connection, "Explicit Governance Detail", content=detail_text)
    core_id = _store(
        connection,
        "Explicit Core",
        content=f"{detail_text}\n\nThe detail title is Explicit Governance Detail and UUID {detail_id}.",
    )
    assert (
        _call(
            connection,
            coordinator,
            memory_id=core_id,
            detail_memory_ids=[detail_id],
            include_details=True,
        )["status"]
        == "ok"
    )
    assert (
        cgs.review_core_memory(
            connection,
            entity_id=core_id,
            outcome="demote",
            review_rationale=RATIONALE,
            agent_id="reviewer",
        )["status"]
        == "ok"
    )
    promoted = _call(
        connection,
        coordinator,
        memory_id=core_id,
        detail_memory_ids=[detail_id],
        include_details=True,
    )
    assert promoted["status"] == "ok", promoted
    assert connection.execute(
        "SELECT core_detail_memory_ids FROM entities WHERE id = ?", (core_id,)
    ).fetchone()[0] == json.dumps([detail_id])


def test_ambiguous_prefix_returns_candidates_without_writes(harness):
    _db_path, connection, coordinator, _mode = harness
    memory_id = _store(connection, "Ambiguous Target")
    before = _snapshot(connection)
    with patch(
        "saltmdb.domain.services.memory_service.lifecycle.resolve_entity_ref",
        return_value=(None, [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}], False),
    ):
        result = _call(connection, coordinator, memory_id=memory_id[:4])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "AMBIGUOUS_ID_PREFIX"
    assert result["errors"][0]["field"] == "entity_id"
    assert _snapshot(connection) == before


def test_direct_mode_capacity_check_sees_previous_promotion(harness):
    _db_path, connection, coordinator, mode = harness
    if mode != "direct":
        pytest.skip("direct transaction coverage")
    for index in range(5):
        assert _store_core(connection, f"Direct Capacity Core {index}")
    target = _store(connection, "Direct Sixth Core")
    rejected = _call(connection, coordinator, memory_id=target)
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "CORE_CAPACITY_EXCEEDED"
