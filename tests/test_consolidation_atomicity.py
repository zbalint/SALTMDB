"""Consolidation row writes roll back atomically under direct and coordinator seams."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.db.connection import open_read_connection, write_transaction
from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import store_memory


@pytest.fixture(params=["direct", "coordinator"])
def consolidation_harness(tmp_path, request, monkeypatch):
    db_path = str(tmp_path / "consolidation.db")
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


def _parent(connection, title: str) -> str:
    result = store_memory(
        title=title,
        content=f"Detailed durable source content for {title}.",
        agent_id="test-owner",
        db_connection=connection,
    )
    assert result["status"] == "ok", result
    return result["data"]["id"]


def _cohesion(_ids, connection):
    placeholders = ",".join("?" for _ in _ids)
    observed = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            f"SELECT id, content_hash, status FROM entities WHERE id IN ({placeholders})", _ids
        ).fetchall()
    }
    return {entity_id: [1.0, 0.0] for entity_id in _ids}, {}, observed


def _snapshot(db_path: str):
    reader = open_read_connection(db_path)
    try:
        tables = (
            "entities",
            "relations",
            "events",
            "entity_tags",
            "embedding_jobs",
            "retrieval_embedding_jobs",
        )
        snapshot = {
            table: reader.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        snapshot["fts_count"] = reader.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        return snapshot
    finally:
        reader.close()


def _single_call(connection, coordinator, parent_ids):
    arguments = {
        "parent_ids": parent_ids,
        "title": "Atomic canonical result",
        "content": "A durable canonical result for atomicity testing.",
    }
    if coordinator is None:
        return relation_service.consolidate_memories(**arguments, db_connection=connection)
    return dispatch_tool("consolidate_memories", arguments, coordinator)


def _bulk_call(connection, coordinator, consolidations):
    if coordinator is None:
        return relation_service.bulk_commit_consolidation(
            consolidations=consolidations, db_connection=connection
        )
    return dispatch_tool("consolidate_memories", {"consolidations": consolidations}, coordinator)


def test_single_failure_after_rows_write_rolls_back(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_ids = [
        _parent(connection, "Single Failure Parent A"),
        _parent(connection, "Single Failure Parent B"),
    ]
    before = _snapshot(db_path)
    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(
            relation_service,
            "_consolidation_insert_lineage",
            side_effect=sqlite3.OperationalError("injected failure"),
        ),
    ):
        result = _single_call(connection, coordinator, parent_ids)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CONSOLIDATION_FAILED"
    assert "injected failure" in result["errors"][0]["message"]
    assert _snapshot(db_path) == before


def test_bulk_quality_failure_after_item_zero_rolls_back(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_groups = [
        [
            _parent(connection, f"Bulk Parent {index} A"),
            _parent(connection, f"Bulk Parent {index} B"),
        ]
        for index in range(3)
    ]
    consolidations = [
        {
            "parent_ids": parent_groups[0],
            "title": "Bulk canonical zero",
            "content": "A valid durable canonical result for item zero.",
        },
        {
            "parent_ids": parent_groups[1],
            "title": "Bulk rejected one",
            "content": "test " * 40,
        },
        {
            "parent_ids": parent_groups[2],
            "title": "Bulk canonical two",
            "content": "A valid durable canonical result for item two.",
        },
    ]
    before = _snapshot(db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _bulk_call(connection, coordinator, consolidations)
    assert len(result) == 1
    assert result[0]["status"] == "error"
    assert _snapshot(db_path) == before


def test_bulk_success_clears_vectors_once_after_last_item(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_groups = [
        [
            _parent(connection, f"Vector Bulk Parent {index} A"),
            _parent(connection, f"Vector Bulk Parent {index} B"),
        ]
        for index in range(3)
    ]
    items = [
        {
            "parent_ids": parents,
            "title": f"Vector bulk result {index}",
            "content": f"A durable vector bulk result for item {index}.",
        }
        for index, parents in enumerate(parent_groups)
    ]
    observed: list[tuple[str, str]] = []
    if coordinator is None:
        connection.set_trace_callback(lambda statement: observed.append(("sql", statement)))
    else:
        coordinator.submit(
            "install-bulk-trace",
            lambda conn: conn.set_trace_callback(
                lambda statement: observed.append(("sql", statement))
            ),
        )
    cleared: list[str] = []

    def record_retrieval(_conn, _entity_id, **kwargs):
        observed.append(("retrieval", str(kwargs["clear_vector"])))

    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=lambda _conn, entity_id, **_kwargs: cleared.append(entity_id),
        ),
        patch(
            "saltmdb.domain.services.embedding_service.cancel_retrieval_embedding_jobs_for_entity",
            side_effect=record_retrieval,
        ),
    ):
        result = _bulk_call(connection, coordinator, items)
    if coordinator is None:
        connection.set_trace_callback(None)
    else:
        coordinator.submit("remove-bulk-trace", lambda conn: conn.set_trace_callback(None))

    assert [item["status"] for item in result] == ["success", "success", "success"]
    assert cleared == [parent_id for group in parent_groups for parent_id in group]
    release_index = next(
        index
        for index, (kind, statement) in enumerate(observed)
        if kind == "sql" and statement.startswith("RELEASE bulk_consolidation_")
    )
    retrieval_flags_before = [
        value == "True"
        for index, (kind, value) in enumerate(observed)
        if kind == "retrieval" and index < release_index
    ]
    retrieval_flags_after = [
        value == "True"
        for index, (kind, value) in enumerate(observed)
        if kind == "retrieval" and index > release_index
    ]
    assert retrieval_flags_before == [False] * 6
    assert retrieval_flags_after == [True] * 6
    assert _snapshot(db_path)["relations"]


def test_coordinator_job_can_continue_after_failed_bulk_savepoint(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    if coordinator is None:
        pytest.skip("composability seam requires the real coordinator")
    failed_parents = [
        _parent(connection, "Composability Failed Parent A"),
        _parent(connection, "Composability Failed Parent B"),
        _parent(connection, "Composability Failed Parent C"),
        _parent(connection, "Composability Failed Parent D"),
    ]
    valid_parents = [
        _parent(connection, "Composability Valid Parent A"),
        _parent(connection, "Composability Valid Parent B"),
    ]

    def job(conn):
        conn.execute(
            "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
            ("unrelated-event", "owner", "test", "must survive"),
        )
        failed = relation_service.bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": failed_parents[:2],
                    "title": "Failed bulk item zero",
                    "content": "A valid durable item before the rejection.",
                },
                {
                    "parent_ids": failed_parents[2:],
                    "title": "Failed bulk item one",
                    "content": "test " * 40,
                },
            ],
            db_connection=conn,
        )
        valid = relation_service.consolidate_memories(
            parent_ids=valid_parents,
            title="Valid after failed bulk",
            content="A valid durable result after a rolled-back bulk operation.",
            db_connection=conn,
        )
        return failed, valid

    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        failed, valid = coordinator.submit("composable-consolidation", job)
    assert failed[0]["status"] == "error"
    assert valid["status"] == "ok"
    reader = open_read_connection(db_path)
    try:
        assert reader.execute("SELECT id FROM events WHERE id = ?", ("unrelated-event",)).fetchone()
        assert (
            reader.execute(
                "SELECT id FROM entities WHERE title = ?", ("Failed bulk item zero",)
            ).fetchone()
            is None
        )
        assert reader.execute(
            "SELECT id FROM entities WHERE title = ?", ("Valid after failed bulk",)
        ).fetchone()
    finally:
        reader.close()


def test_single_success_clears_parent_vectors_after_savepoint_release(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_ids = [
        _parent(connection, "Vector Order Parent A"),
        _parent(connection, "Vector Order Parent B"),
    ]
    observed: list[tuple[str, str]] = []
    if coordinator is None:
        connection.set_trace_callback(lambda statement: observed.append(("sql", statement)))
    else:
        coordinator.submit(
            "install-trace",
            lambda conn: conn.set_trace_callback(
                lambda statement: observed.append(("sql", statement))
            ),
        )
    cleared: list[str] = []

    def record_clear(_conn, entity_id, **kwargs):
        cleared.append(entity_id)
        observed.append(("retrieval", str(kwargs["clear_vector"])))

    def record_vector_clear(_conn, entity_id, **_kwargs):
        observed.append(("clear", entity_id))

    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=record_vector_clear,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.cancel_retrieval_embedding_jobs_for_entity",
            side_effect=record_clear,
        ),
    ):
        result = _single_call(connection, coordinator, parent_ids)
    if coordinator is None:
        connection.set_trace_callback(None)
    else:
        coordinator.submit("remove-trace", lambda conn: conn.set_trace_callback(None))

    assert result["status"] == "ok"
    assert len(cleared) == 4
    assert sum(kind == "clear" for kind, _value in observed) == 2
    release_index = next(
        index
        for index, (kind, statement) in enumerate(observed)
        if kind == "sql" and statement.startswith("RELEASE consolidation_")
    )
    retrieval_flags_before = [
        value == "True"
        for index, (kind, value) in enumerate(observed)
        if kind == "retrieval" and index < release_index
    ]
    retrieval_flags_after = [
        value == "True"
        for index, (kind, value) in enumerate(observed)
        if kind == "retrieval" and index > release_index
    ]
    assert retrieval_flags_before == [False] * 2
    assert retrieval_flags_after == [True] * 2


def test_savepoint_rejects_connection_without_outer_transaction(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        with pytest.raises(RuntimeError, match="active transaction"):
            from saltmdb.db.connection import savepoint

            with savepoint(connection, "outside"):
                pass
    finally:
        connection.set_trace_callback(None)
        connection.close()
    assert not any(statement.startswith("SAVEPOINT outside_") for statement in statements)


def test_savepoint_rolls_back_only_nested_writes_and_preserves_original_exception(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
        ("outer-event", "owner", "test", "outer"),
    )
    from saltmdb.db.connection import savepoint

    original = ValueError("original")
    with pytest.raises(ValueError) as caught:
        with savepoint(connection, "nested"):
            connection.execute(
                "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                ("nested-event", "owner", "test", "nested"),
            )
            raise original
    assert caught.value is original
    assert connection.in_transaction
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    connection.close()


def test_savepoint_preserves_exception_when_outer_transaction_dies(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    connection.execute("BEGIN IMMEDIATE")
    from saltmdb.db.connection import savepoint

    original = sqlite3.OperationalError("injected")
    with (
        patch("saltmdb.db.connection.rollback_to_and_release") as rollback_to_release,
        pytest.raises(sqlite3.OperationalError) as caught,
    ):
        with savepoint(connection, "dead"):
            connection.execute("ROLLBACK")
            raise original
    assert caught.value is original
    assert not connection.in_transaction
    rollback_to_release.assert_not_called()
    connection.close()


def test_savepoint_fallback_rollback_preserves_original_exception(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    connection.execute("BEGIN IMMEDIATE")
    from saltmdb.db.connection import savepoint

    original = ValueError("original")
    with (
        patch(
            "saltmdb.db.connection.rollback_to_and_release",
            side_effect=sqlite3.OperationalError("cleanup failed"),
        ),
        pytest.raises(ValueError) as caught,
    ):
        with savepoint(connection, "fallback"):
            connection.execute(
                "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
                ("fallback-event", "owner", "test", "fallback"),
            )
            raise original
    assert caught.value is original
    assert not connection.in_transaction
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    connection.close()


def test_savepoint_rejects_outer_transaction_death_on_normal_exit(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    connection.execute("BEGIN IMMEDIATE")
    from saltmdb.db.connection import savepoint

    with pytest.raises(RuntimeError, match="outer transaction ended"):
        with savepoint(connection, "ended"):
            connection.execute("ROLLBACK")
    connection.close()


def test_single_positive_control_archives_parents_and_links_lineage(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_ids = [
        _parent(connection, "Positive Parent A"),
        _parent(connection, "Positive Parent B"),
    ]
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _single_call(connection, coordinator, parent_ids)
    assert result["status"] == "ok"
    reader = open_read_connection(db_path)
    try:
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM entities WHERE status = 'consolidated'"
            ).fetchone()[0]
            == 1
        )
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM entities WHERE id IN (?, ?) AND status = 'archived'",
                parent_ids,
            ).fetchone()[0]
            == 2
        )
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM relations WHERE predicate = 'consolidated_from'"
            ).fetchone()[0]
            == 2
        )
    finally:
        reader.close()


def test_bulk_lineage_failure_after_item_zero_rolls_back(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    parent_groups = [
        [
            _parent(connection, f"Lineage Parent {index} A"),
            _parent(connection, f"Lineage Parent {index} B"),
        ]
        for index in range(2)
    ]
    items = [
        {
            "parent_ids": parents,
            "title": f"Lineage result {index}",
            "content": f"A durable lineage result for item {index}.",
        }
        for index, parents in enumerate(parent_groups)
    ]
    before = _snapshot(db_path)
    original = relation_service._consolidation_insert_lineage
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected second-item failure")
        return original(*args)

    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(relation_service, "_consolidation_insert_lineage", side_effect=fail_second),
    ):
        result = _bulk_call(connection, coordinator, items)
    assert result[0]["status"] == "error"
    assert _snapshot(db_path) == before


def test_bulk_shared_parent_rejection_rolls_back(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    shared = _parent(connection, "Shared Parent")
    first_other = _parent(connection, "Shared First Other")
    second_other = _parent(connection, "Shared Second Other")
    items = [
        {
            "parent_ids": [shared, first_other],
            "title": "Shared parent result one",
            "content": "A durable shared-parent result for item one.",
        },
        {
            "parent_ids": [shared, second_other],
            "title": "Shared parent result two",
            "content": "A durable shared-parent result for item two.",
        },
    ]
    before = _snapshot(db_path)
    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = _bulk_call(connection, coordinator, items)
    assert result[0]["status"] == "error"
    assert _snapshot(db_path) == before


def test_coordinator_unrelated_event_survives_rejected_bulk(consolidation_harness):
    db_path, connection, coordinator, _mode = consolidation_harness
    if coordinator is None:
        pytest.skip("composability seam requires the real coordinator")
    parents = [
        _parent(connection, "Standalone Failed Parent A"),
        _parent(connection, "Standalone Failed Parent B"),
        _parent(connection, "Standalone Rejected Parent A"),
        _parent(connection, "Standalone Rejected Parent B"),
    ]

    def job(conn):
        conn.execute(
            "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
            ("standalone-event", "owner", "test", "must survive"),
        )
        return relation_service.bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": parents[:2],
                    "title": "Standalone failed item zero",
                    "content": "A valid durable item before the rejection.",
                },
                {
                    "parent_ids": parents[2:],
                    "title": "Standalone rejected item one",
                    "content": "test " * 40,
                },
            ],
            db_connection=conn,
        )

    with patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion):
        result = coordinator.submit("standalone-consolidation", job)
    assert result[0]["status"] == "error"
    reader = open_read_connection(db_path)
    try:
        assert reader.execute(
            "SELECT id FROM events WHERE id = ?", ("standalone-event",)
        ).fetchone()
        assert (
            reader.execute(
                "SELECT id FROM entities WHERE title = ?", ("Standalone failed item zero",)
            ).fetchone()
            is None
        )
    finally:
        reader.close()


def test_coordinator_transaction_death_preserves_injected_failure(consolidation_harness):
    _db_path, connection, coordinator, _mode = consolidation_harness
    if coordinator is None:
        pytest.skip("transaction-death seam requires the real coordinator")
    parent_ids = [
        _parent(connection, "Dead Transaction Parent A"),
        _parent(connection, "Dead Transaction Parent B"),
    ]

    def die(conn, *_args):
        conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("transaction died")

    cleared: list[str] = []

    def invoke_without_outer_cleanup(conn, callback):
        conn.execute("BEGIN IMMEDIATE")
        return callback(conn)

    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(relation_service, "_consolidation_insert_lineage", side_effect=die),
        patch.object(
            relation_service,
            "write_transaction_retrying",
            side_effect=invoke_without_outer_cleanup,
        ),
        patch(
            "saltmdb.domain.services.embedding_service.clear_embedding_vectors_for_entity",
            side_effect=lambda _conn, entity_id, **_kwargs: cleared.append(entity_id),
        ),
    ):
        result = relation_service.consolidate_memories(
            parent_ids=parent_ids,
            title="Atomic canonical result",
            content="A durable canonical result for atomicity testing.",
            db_connection=connection,
        )
    assert result["status"] == "rejected"
    assert "transaction died" in result["errors"][0]["message"]
    assert cleared == []


def test_savepoint_releases_successful_nested_writes(tmp_path):
    connection = init_db(str(tmp_path / "savepoint.db"))
    connection.execute("BEGIN IMMEDIATE")
    from saltmdb.db.connection import savepoint

    with savepoint(connection, "success"):
        connection.execute(
            "INSERT INTO events (id, agent_id, type, content) VALUES (?, ?, ?, ?)",
            ("success-event", "owner", "test", "success"),
        )
    assert connection.in_transaction
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM events WHERE id = ?", ("success-event",)
        ).fetchone()[0]
        == 1
    )
    connection.execute("ROLLBACK")
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    connection.close()


def test_single_rows_closure_rerun_deduplicates_orphaned_worklist(consolidation_harness):
    db_path, connection, coordinator, mode = consolidation_harness
    if mode != "direct":
        pytest.skip("retry-closure seam is direct-mode only")
    parent_ids = [_parent(connection, "Rerun Parent A"), _parent(connection, "Rerun Parent B")]
    external_id = _parent(connection, "Rerun External Parent")
    relation_result = relation_service.store_relation(
        source_id=parent_ids[0],
        target_id=external_id,
        predicate="related_to",
        db_connection=connection,
    )
    assert relation_result["status"] == "ok", relation_result
    attempts = 0

    def retry_twice(conn, callback):
        nonlocal attempts
        for attempt in range(2):
            try:
                with write_transaction(conn) as transaction:
                    result = callback(transaction)
                    attempts += 1
                    if attempt == 0:
                        raise sqlite3.OperationalError("database is locked")
                return result
            except sqlite3.OperationalError:
                if attempt == 1:
                    raise
        raise AssertionError("retry loop did not return")

    with (
        patch.object(relation_service, "get_fresh_entity_centroids", side_effect=_cohesion),
        patch.object(relation_service, "write_transaction_retrying", side_effect=retry_twice),
    ):
        result = relation_service.consolidate_memories(
            parent_ids=parent_ids,
            title="Rerun orphan worklist result",
            content="A durable result whose closure is retried.",
            db_connection=connection,
        )

    assert attempts == 2
    assert result["status"] == "ok"
    assert len(result["data"]["orphaned_edges"]) == 1
    assert result["data"]["orphaned_edges"][0]["other_endpoint"] == external_id
