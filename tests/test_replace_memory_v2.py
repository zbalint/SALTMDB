from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import patch

import pytest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon import dispatch
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    CoreDeclaration,
    ReplacementRequest,
    replace_memory,
    store_memory,
)


def _store(conn, title: str, *, is_core: bool = False, retrieval_text: str | None = None) -> str:
    if retrieval_text is None:
        result = store_memory(
            title=title,
            content=f"A durable body for {title} with enough descriptive content.",
            agent_id="replace-tests",
            db_connection=conn,
            is_core=is_core,
            core_reason="A sufficiently long core reason for tests" if is_core else None,
            core_exit_condition="A sufficiently long core exit condition for tests"
            if is_core
            else None,
        )
    else:
        result = store_memory(
            title=title,
            content=f"A durable body for {title} with enough descriptive content.",
            agent_id="replace-tests",
            db_connection=conn,
            is_core=is_core,
            core_reason="A sufficiently long core reason for tests" if is_core else None,
            core_exit_condition="A sufficiently long core exit condition for tests"
            if is_core
            else None,
            retrieval_text=retrieval_text,
        )
    return result["data"]["id"]


def _snapshot(conn):
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    names.difference_update({"tool_call_telemetry"})
    names.update(
        {
            "entities",
            "relations",
            "events",
            "entity_tags",
            "embedding_jobs",
            "retrieval_embedding_jobs",
            "entity_embeddings",
            "entity_chunk_embeddings",
        }
    )
    snapshot = {}
    for name in sorted(
        names
        & {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    ):
        snapshot[name] = conn.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
    return snapshot


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_core_replacement_requires_explicit_decision_and_is_atomic(tmp_path, mode):
    db_path = str(tmp_path / f"{mode}.db")
    conn = init_db(db_path)
    old_id = _store(conn, "Core predecessor", is_core=True)
    before = _snapshot(conn)
    coordinator = DbWriteCoordinator(db_path) if mode == "coordinator" else None
    if coordinator:
        coordinator.start()
    try:
        request = ReplacementRequest(
            entity_id=old_id,
            title="Replacement",
            content="A durable replacement body with enough descriptive content.",
            reason="Corrected representation",
            db_connection=conn,
        )
        if coordinator:
            result = dispatch_tool(
                "revise_memory",
                {
                    "entity_id": old_id,
                    "title": "Replacement",
                    "content": request.content,
                    "reason": request.reason,
                },
                coordinator,
            )
        else:
            result = replace_memory("revises", request)
        assert isinstance(result, dict)
        assert result["status"] == "rejected"
        assert result["errors"][0]["code"] == "CORE_DECISION_REQUIRED"
        assert _snapshot(conn) == before
    finally:
        if coordinator:
            coordinator.shutdown(timeout=5)
        conn.close()


def test_core_keep_and_demote_reset_successor_columns(tmp_path):
    for name, declaration, expected_core in (
        (
            "keep",
            CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long successor core reason",
                core_exit_condition="A sufficiently long successor core exit condition",
            ),
            1,
        ),
        ("demote", CoreDeclaration(is_core=False), 0),
    ):
        conn = init_db(str(tmp_path / f"{name}.db"))
        old_id = _store(conn, f"Core {name}", is_core=True, retrieval_text="old retrieval")
        result = replace_memory(
            "revises",
            ReplacementRequest(
                entity_id=old_id,
                title=f"Successor {name}",
                content="A durable successor body with enough descriptive content.",
                reason="Explicit core outcome",
                retrieval_text="new retrieval",
                core=declaration,
                db_connection=conn,
            ),
        )
        assert result["status"] == "ok", result
        new_id = result["data"]["new_id"]
        row = conn.execute(
            "SELECT is_core, core_reason, core_exit_condition, retrieval_text, parent_ids FROM entities WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert row[0] == expected_core and row[3] == "new retrieval" and row[4] is None
        tags = conn.execute(
            "SELECT t.name FROM tags t JOIN entity_tags et ON et.tag_id = t.id WHERE et.entity_id = ?",
            (new_id,),
        ).fetchall()
        if expected_core:
            assert row[1] and row[2] and ("#core",) in tags
            assert result["data"]["changed"]["core"]["reason"]
            assert result["data"]["changed"]["core"]["exit_condition"]
        else:
            assert result["data"]["changed"]["demote_core"] is True
            assert row[1] is None and row[2] is None and ("#core",) not in tags
        conn.close()


def test_replacement_retrieval_text_is_explicit(tmp_path):
    conn = init_db(str(tmp_path / "retrieval.db"))
    old_id = _store(conn, "Retrieval predecessor", retrieval_text="prior text")
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Retrieval successor",
            content="A durable replacement body with enough descriptive content.",
            reason="Corrected representation",
            retrieval_text="  next text  ",
            db_connection=conn,
        ),
    )
    assert result["status"] == "ok", result
    assert (
        conn.execute(
            "SELECT retrieval_text FROM entities WHERE id = ?", (result["data"]["new_id"],)
        ).fetchone()[0]
        == "next text"
    )
    conn.close()


def test_invalid_retrieval_text_rejects_without_side_effects(tmp_path):
    conn = init_db(str(tmp_path / "invalid-retrieval.db"))
    old_id = _store(conn, "Invalid retrieval predecessor")
    before = _snapshot(conn)
    calls = []
    with (
        patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=lambda *args, **kwargs: calls.append(("clear", args, kwargs)),
        ),
        patch(
            "saltmdb.domain.services.embedding_service.enqueue_retrieval_embedding_job_for_entity",
            side_effect=lambda *args, **kwargs: calls.append(("enqueue", args, kwargs)),
        ),
    ):
        result = replace_memory(
            "revises",
            ReplacementRequest(
                entity_id=old_id,
                title="Invalid retrieval successor",
                content="A durable replacement body with enough descriptive content.",
                reason="Invalid retrieval test",
                retrieval_text=cast(Any, 123),
                db_connection=conn,
            ),
        )
    assert calls == []
    assert result["status"] == "rejected"
    assert _snapshot(conn) == before
    conn.close()


def test_coordinator_failure_rolls_back_replacement(tmp_path):
    db_path = str(tmp_path / "atomic.db")
    conn = init_db(db_path)
    old_id = _store(conn, "Atomic predecessor")
    before = _snapshot(conn)
    coordinator = DbWriteCoordinator(db_path)
    coordinator.start()
    try:
        with patch(
            "saltmdb.domain.services.embedding_service.enqueue_retrieval_embedding_job_for_entity",
            side_effect=RuntimeError("injected"),
        ):
            with pytest.raises(RuntimeError, match="injected"):
                dispatch_tool(
                    "revise_memory",
                    {
                        "entity_id": old_id,
                        "title": "Atomic successor",
                        "content": "A durable replacement body with enough descriptive content.",
                        "reason": "Failure test",
                    },
                    coordinator,
                )
        assert _snapshot(conn) == before
    finally:
        coordinator.shutdown(timeout=5)
        conn.close()


def test_core_detail_validation_rejects_without_snapshot_change(tmp_path):
    conn = init_db(str(tmp_path / "detail.db"))
    old_id = _store(conn, "Detail predecessor", is_core=True)
    before = _snapshot(conn)
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Detail successor",
            content="A durable detail successor body with enough descriptive content.",
            reason="Detail validation",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long successor core reason",
                core_exit_condition="A sufficiently long successor core exit condition",
                detail_memory_ids=["missing-detail"],
            ),
            db_connection=conn,
        ),
    )
    assert result["status"] == "rejected"
    assert _snapshot(conn) == before
    conn.close()


def test_overdue_core_source_rejects_keep_and_allows_demote(tmp_path):
    conn = init_db(str(tmp_path / "overdue.db"))
    old_id = _store(conn, "Overdue predecessor", is_core=True)
    overdue = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute("UPDATE entities SET core_review_after = ? WHERE id = ?", (overdue, old_id))
    conn.commit()
    keep = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Overdue keep",
            content="A durable overdue keep body with enough descriptive content.",
            reason="Keep overdue core",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long successor core reason",
                core_exit_condition="A sufficiently long successor core exit condition",
            ),
            db_connection=conn,
        ),
    )
    assert keep["status"] == "rejected"
    demote = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Overdue demote",
            content="A durable overdue demotion body with enough descriptive content.",
            reason="Demote overdue core",
            core=CoreDeclaration(is_core=False),
            db_connection=conn,
        ),
    )
    assert demote["status"] == "ok", demote
    conn.close()


def test_core_repoint_skips_elaborates_on_inbound_edge(tmp_path):
    conn = init_db(str(tmp_path / "repoint.db"))
    old_id = _store(conn, "Repoint core predecessor", is_core=True)
    detail_id = _store(conn, "Repoint detail")
    conn.execute(
        "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) VALUES (?, ?, ?, 'elaborates_on', datetime('now'), datetime('now'), datetime('now'))",
        ("detail-edge", detail_id, old_id),
    )
    conn.commit()
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Repoint core successor",
            content="A durable repoint successor body with enough descriptive content.",
            reason="Repoint core",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long successor core reason",
                core_exit_condition="A sufficiently long successor core exit condition",
            ),
            repoint_relations=True,
            db_connection=conn,
        ),
    )
    assert result["status"] == "ok", result
    new_id = result["data"]["new_id"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
            (detail_id, new_id),
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM relations WHERE id = 'detail-edge' AND valid_to IS NULL"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_core_capacity_rejects_promotion_without_snapshot_change(tmp_path):
    conn = init_db(str(tmp_path / "capacity.db"))
    for index in range(5):
        _store(conn, f"Capacity core {index}", is_core=True)
    source_id = _store(conn, "Capacity source")
    before = _snapshot(conn)
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=source_id,
            title="Capacity successor",
            content="A durable capacity successor body with enough descriptive content.",
            reason="Capacity rejection",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long capacity core reason",
                core_exit_condition="A sufficiently long capacity core exit condition",
            ),
            db_connection=conn,
        ),
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CORE_CAPACITY_EXCEEDED"
    assert _snapshot(conn) == before
    conn.close()


def test_reset_columns_are_present_and_cleared_on_success(tmp_path):
    conn = init_db(str(tmp_path / "columns.db"))
    old_id = _store(conn, "Column predecessor")
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Column successor",
            content="A durable column successor body with enough descriptive content.",
            reason="Reset columns",
            db_connection=conn,
        ),
    )
    assert result["status"] == "ok", result
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()}
    reset = {
        "is_core",
        "core_reason",
        "core_exit_condition",
        "core_review_after",
        "core_detail_memory_ids",
        "core_last_reviewed_at",
        "core_last_reviewed_by",
        "core_review_rationale",
        "retrieval_text",
        "retrieval_text_hash",
        "parent_ids",
    }
    assert reset <= columns
    new_id = result["data"]["new_id"]
    row = conn.execute(
        "SELECT " + ", ".join(sorted(reset)) + " FROM entities WHERE id = ?", (new_id,)
    ).fetchone()
    assert all(value in (None, 0, "[]") for value in row)
    conn.close()


def test_vector_clear_failure_rolls_back_coordinator_write(tmp_path):
    db_path = str(tmp_path / "vector-clear.db")
    conn = init_db(db_path)
    old_id = _store(conn, "Vector predecessor")
    before = _snapshot(conn)
    coordinator = DbWriteCoordinator(db_path)
    coordinator.start()
    try:
        with patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=RuntimeError("clear injected"),
        ):
            with pytest.raises(RuntimeError, match="clear injected"):
                dispatch_tool(
                    "revise_memory",
                    {
                        "entity_id": old_id,
                        "title": "Vector successor",
                        "content": "A durable vector successor body with enough descriptive content.",
                        "reason": "Vector failure",
                    },
                    coordinator,
                )
        assert _snapshot(conn) == before
    finally:
        coordinator.shutdown(timeout=5)
        conn.close()


def test_private_core_keep_rejection_names_scope(tmp_path):
    conn = init_db(str(tmp_path / "private-core.db"))
    old_id = _store(conn, "Private core predecessor", is_core=True)
    conn.execute("UPDATE entities SET scope = 'private' WHERE id = ?", (old_id,))
    conn.commit()
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Private core successor",
            content="A durable private successor body with enough descriptive content.",
            reason="Private core test",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long private core reason",
                core_exit_condition="A sufficiently long private core exit condition",
            ),
            db_connection=conn,
        ),
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["field"] == "scope"
    conn.close()


def test_dispatch_maps_demote_and_keep_core_fields_and_requires_reason():
    with patch(
        "saltmdb.daemon.dispatch.memory_service.replace_memory", return_value={"status": "ok"}
    ) as replace:
        dispatch._dispatch_revise_memory(
            entity_id="old", title="Demote", content="body", reason="reason", demote_core=True
        )
        request = replace.call_args.args[1]
        assert request.core.is_core is False
        dispatch._dispatch_revise_memory(
            entity_id="old",
            title="Keep",
            content="body",
            reason="reason",
            is_core=True,
            core_reason="A sufficiently long reason",
            core_exit_condition="A sufficiently long exit condition",
        )
        request = replace.call_args.args[1]
        assert request.core.is_core is True
        assert request.core.core_reason
    missing = dispatch._dispatch_revise_memory(entity_id="old", title="Missing", content="body")
    assert missing["errors"][0]["field"] == "reason"


def test_retrieval_text_redacts_secrets_and_rejects_over_cap(tmp_path):
    conn = init_db(str(tmp_path / "retrieval-limits.db"))
    old_id = _store(conn, "Retrieval limit predecessor")
    secret_result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Retrieval secret successor",
            content="A durable retrieval successor body with enough descriptive content.",
            reason="Secret retrieval",
            retrieval_text="ABC password: Sup3rSecretValue123",
            db_connection=conn,
        ),
    )
    assert secret_result["status"] == "ok", secret_result
    stored = conn.execute(
        "SELECT retrieval_text FROM entities WHERE id = ?",
        (secret_result["data"]["new_id"],),
    ).fetchone()[0]
    assert stored == "ABC [REDACTED_SECRET]"
    cap_result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=secret_result["data"]["new_id"],
            title="Retrieval over cap",
            content="A durable retrieval over-cap body with enough descriptive content.",
            reason="Over cap",
            retrieval_text="x" * 10001,
            db_connection=conn,
        ),
    )
    assert cap_result["status"] == "rejected"
    assert cap_result["errors"][0]["code"] == "INVALID_RETRIEVAL_TEXT"
    conn.close()


def test_declared_detail_creates_one_detail_to_successor_edge(tmp_path):
    conn = init_db(str(tmp_path / "detail-edge.db"))
    old_id = _store(conn, "Detail core predecessor", is_core=True)
    detail_id = _store(conn, "Declared detail")
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Detail core successor",
            content=f"A durable detail successor body with enough descriptive content. Declared detail {detail_id}.",
            reason="Declared detail",
            core=CoreDeclaration(
                is_core=True,
                core_reason="A sufficiently long declared detail reason",
                core_exit_condition="A sufficiently long declared detail exit condition",
                detail_memory_ids=[detail_id],
            ),
            db_connection=conn,
        ),
    )
    assert result["status"] == "ok", result
    new_id = result["data"]["new_id"]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = 'elaborates_on' AND valid_to IS NULL",
            (detail_id, new_id),
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_savepoint_and_vector_clear_order_and_repoint_rollback(tmp_path):
    db_path = str(tmp_path / "savepoint.db")
    conn = init_db(db_path)
    old_id = _store(conn, "Savepoint predecessor")
    traces = []
    conn.set_trace_callback(traces.append)
    result = replace_memory(
        "revises",
        ReplacementRequest(
            entity_id=old_id,
            title="Savepoint successor",
            content="A durable savepoint successor body with enough descriptive content.",
            reason="Savepoint test",
            db_connection=conn,
        ),
    )
    assert result["status"] == "ok", result
    savepoint_index = next(
        i for i, statement in enumerate(traces) if "SAVEPOINT replacement" in statement
    )
    insert_index = next(
        i for i, statement in enumerate(traces) if "INSERT INTO entities" in statement
    )
    release_index = next(
        i for i, statement in enumerate(traces) if "RELEASE replacement" in statement
    )
    assert savepoint_index < insert_index < release_index
    conn.close()

    conn = init_db(str(tmp_path / "repoint-rollback.db"))
    old_id = _store(conn, "Repoint rollback predecessor")
    coordinator = DbWriteCoordinator(str(tmp_path / "repoint-rollback.db"))
    coordinator.start()
    try:
        with patch(
            "saltmdb.domain.services.memory_service.lifecycle._replacement_repoint_edges",
            side_effect=RuntimeError("repoint injected"),
        ):

            def job(worker_conn):
                worker_conn.execute(
                    "INSERT INTO events (id, agent_id, timestamp, type, content) VALUES (?, ?, datetime('now'), ?, ?)",
                    ("before-repoint", "test-agent", "test", "earlier work"),
                )
                try:
                    replace_memory(
                        "revises",
                        ReplacementRequest(
                            entity_id=old_id,
                            title="Repoint rollback successor",
                            content="A durable repoint rollback body with enough descriptive content.",
                            reason="Rollback test",
                            repoint_relations=True,
                            db_connection=worker_conn,
                            in_transaction=True,
                        ),
                    )
                except RuntimeError:
                    return "caught"
                return "not-caught"

            assert coordinator.submit("repoint-failure", job) == "caught"
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE id = 'before-repoint'").fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        coordinator.shutdown(timeout=5)
        conn.close()


def test_vector_clear_occurs_after_release_and_not_on_rejection(tmp_path):
    conn = init_db(str(tmp_path / "vector-order.db"))
    old_id = _store(conn, "Vector order predecessor")
    order = []
    conn.set_trace_callback(lambda statement: order.append(statement))
    with patch(
        "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
        side_effect=lambda *args, **kwargs: order.append("CLEAR"),
    ):
        result = replace_memory(
            "revises",
            ReplacementRequest(
                entity_id=old_id,
                title="Vector order successor",
                content="A durable vector order successor body with enough descriptive content.",
                reason="Vector ordering",
                db_connection=conn,
            ),
        )
    assert result["status"] == "ok", result
    assert order.index("CLEAR") > next(
        index for index, statement in enumerate(order) if "RELEASE replacement" in statement
    )
    core_id = _store(conn, "Vector rejection core", is_core=True)
    calls = []
    with patch(
        "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
        side_effect=lambda *args, **kwargs: calls.append(args),
    ):
        rejected = replace_memory(
            "revises",
            ReplacementRequest(
                entity_id=core_id,
                title="Vector rejection successor",
                content="A durable vector rejection body with enough descriptive content.",
                reason="Vector rejection",
                db_connection=conn,
            ),
        )
    assert rejected["status"] == "rejected"
    assert calls == []
    conn.close()
