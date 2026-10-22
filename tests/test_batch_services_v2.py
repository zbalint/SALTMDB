"""Atomic batch behavior at the service and real daemon-coordinator seams."""

import uuid
import sqlite3
from unittest.mock import Mock, patch
import pytest
import sqlite_vec

from saltmdb.daemon.db_write_coordinator import DbWriteCoordinator
from saltmdb.daemon.dispatch import dispatch_tool
from saltmdb.daemon import dispatch
from saltmdb.db.connection import open_read_connection
from saltmdb.db.schema import init_db
from saltmdb.domain.services import relation_service
from saltmdb.domain.services.memory_service import lifecycle


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    path = str(tmp_path / "batch.db")
    connection = init_db(path)
    monkeypatch.setenv("SALTMDB_DB_PATH", path)
    yield path, connection
    connection.close()


def add_memory(
    connection, *, memory_id=None, owner: str | None = "owner", status="raw", axis=0, core=False
):
    memory_id = memory_id or str(uuid.uuid4())
    now = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content, "
        "content_hash, agent_id, status, is_core) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            memory_id,
            now,
            now,
            now,
            f"Memory {memory_id}",
            f"Content for {memory_id}",
            memory_id,
            owner,
            status,
            core,
        ),
    )
    vector = [0.0] * 384
    vector[axis] = 1.0
    connection.execute(
        "INSERT INTO entity_chunk_embeddings "
        "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash) "
        "VALUES (?, ?, ?, 0, 0, 10, ?)",
        (f"{memory_id}::0", memory_id, sqlite_vec.serialize_float32(vector), memory_id),
    )
    return memory_id


def snapshot(path):
    connection = open_read_connection(path)
    try:
        tables = ("entities", "relations", "predicates", "events", "embedding_jobs")
        rows = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        rows["fts_count"] = connection.execute("SELECT COUNT(*) FROM entities_fts").fetchone()[0]
        return rows
    finally:
        connection.close()


@pytest.fixture(params=["direct", "coordinator"])
def invoke(batch_db, request):
    path, _ = batch_db
    coordinator = None
    if request.param == "coordinator":
        coordinator = DbWriteCoordinator(path)
        coordinator.start()

    def call(tool, items, agent_id="owner"):
        if coordinator is not None:
            key = "archive_requests" if tool == "archive_memory" else "relations"
            result = dispatch_tool(
                tool, {"mode": "batch", key: items, "agent_id": agent_id}, coordinator
            )
            assert (
                coordinator.submit_maintenance("check-transaction", lambda c: c.in_transaction)
                is False
            )
            return result
        if tool == "archive_memory":
            return lifecycle.archive_memories(items, agent_id=agent_id, db_path=path)
        return relation_service.apply_relations(items, agent_id=agent_id, db_path=path)

    yield call
    if coordinator is not None:
        coordinator.shutdown(timeout=5)


def test_archive_late_unknown_leaves_everything_unchanged(batch_db, invoke):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection)
    before = snapshot(path)
    result = invoke("archive_memory", [first, second, str(uuid.uuid4())])
    assert result["status"] == "rejected"
    assert [(e["code"], e["field"]) for e in result["errors"]] == [("NOT_FOUND", "memory_ids[2]")]
    assert snapshot(path) == before


def test_relation_rejection_rolls_back_prior_and_later_writes(batch_db, invoke):
    path, connection = batch_db
    first = add_memory(connection, axis=0)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    before = snapshot(path)
    result = invoke(
        "manage_relation",
        [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": "missing", "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "part_of"},
        ],
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "UNKNOWN_ENTITY_ID"
    assert result["errors"][0]["field"] == "relations[1]"
    assert snapshot(path) == before


def test_relation_success_commits_items_and_counts(batch_db, invoke):
    path, connection = batch_db
    first = add_memory(connection, axis=0)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    result = invoke(
        "manage_relation",
        [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "part_of"},
        ],
    )
    assert result["status"] == "ok"
    assert result["data"]["created_count"] == 2
    assert result["data"]["invalidated_count"] == 0


def test_relation_non_dict_item_rejects_without_side_effects(batch_db, invoke):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    before = snapshot(path)
    result = invoke(
        "manage_relation",
        [{"source_id": first, "target_id": second, "predicate": "part_of"}, "bad-item"],
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
    assert result["errors"][0]["field"] == "relations[1]"
    assert snapshot(path) == before


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_relation_rejection_after_valid_write_has_zero_changes(batch_db, mode):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    before = snapshot(path)
    coordinator = DbWriteCoordinator(path) if mode == "coordinator" else None
    if coordinator is not None:
        coordinator.start()
    try:
        payload = [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": "missing", "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "depends_on"},
        ]
        if coordinator is None:
            result = relation_service.apply_relations(payload, agent_id="owner", db_path=path)
        else:
            result = dispatch_tool(
                "manage_relation",
                {"mode": "batch", "relations": payload, "agent_id": "owner"},
                coordinator,
            )
        assert result["status"] == "rejected"
        assert [(error["code"], error["field"]) for error in result["errors"]] == [
            ("UNKNOWN_ENTITY_ID", "relations[1]")
        ]
        assert snapshot(path) == before
    finally:
        if coordinator is not None:
            coordinator.shutdown(timeout=5)


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_relation_success_commits_two_edges(batch_db, mode):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    payload = [
        {"source_id": first, "target_id": second, "predicate": "part_of"},
        {"source_id": second, "target_id": third, "predicate": "depends_on"},
    ]
    coordinator = DbWriteCoordinator(path) if mode == "coordinator" else None
    if coordinator is not None:
        coordinator.start()
    try:
        if coordinator is None:
            result = relation_service.apply_relations(payload, agent_id="owner", db_path=path)
        else:
            result = dispatch_tool(
                "manage_relation",
                {"mode": "batch", "relations": payload, "agent_id": "owner"},
                coordinator,
            )
        assert result["status"] == "ok"
        assert result["data"]["created_count"] == 2
        assert snapshot(path)["relations"]
    finally:
        if coordinator is not None:
            coordinator.shutdown(timeout=5)


def test_coordinator_rejection_preserves_unrelated_same_job_write(batch_db):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    coordinator = DbWriteCoordinator(path)
    coordinator.start()
    try:

        def job(conn):
            conn.execute(
                "INSERT INTO events (id, timestamp, agent_id, type, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), "2026-01-01T00:00:00+00:00", "owner", "decision", "unrelated"),
            )
            return relation_service.apply_relations(
                [
                    {"source_id": first, "target_id": second, "predicate": "part_of"},
                    {"source_id": "missing", "target_id": second, "predicate": "part_of"},
                ],
                agent_id="owner",
                db_connection=conn,
                coordinator=coordinator,
            )

        pending = coordinator.submit("composable-batch", job)
        result = pending if isinstance(pending, dict) else pending.result()
        assert result["status"] == "rejected"
        rows = snapshot(path)
        assert any(row[4] == "unrelated" for row in rows["events"])
        assert rows["relations"] == []
        assert (
            coordinator.submit_maintenance("transaction-check", lambda c: c.in_transaction) is False
        )
    finally:
        coordinator.shutdown(timeout=5)


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_relation_operational_error_rolls_back(batch_db, mode, monkeypatch):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    before = snapshot(path)
    original = relation_service.store_relation
    calls = 0

    def failing_store(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected relation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(relation_service, "store_relation", failing_store)
    coordinator = DbWriteCoordinator(path) if mode == "coordinator" else None
    if coordinator is not None:
        coordinator.start()
    try:
        payload = [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "depends_on"},
        ]
        if coordinator is None:
            result = relation_service.apply_relations(payload, agent_id="owner", db_path=path)
            assert result["status"] == "rejected"
        else:
            with pytest.raises(sqlite3.OperationalError):
                dispatch_tool(
                    "manage_relation",
                    {"mode": "batch", "relations": payload, "agent_id": "owner"},
                    coordinator,
                )
        assert snapshot(path) == before
    finally:
        if coordinator is not None:
            coordinator.shutdown(timeout=5)


def test_archive_owner_and_already_archived_order(batch_db):
    path, connection = batch_db
    other_archived = add_memory(connection, owner="other", status="archived")
    other_active = add_memory(connection, owner="other")
    before = snapshot(path)
    result = lifecycle.archive_memories(
        [other_archived, other_active], agent_id="owner", db_path=path
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "CONFLICT"
    assert result["errors"][0]["field"] == "memory_ids[1]"
    assert snapshot(path) == before
    archived = lifecycle.archive_memories([other_archived], agent_id="owner", db_path=path)
    assert archived["status"] == "ok"
    assert archived["data"]["items"] == [{"memory_id": other_archived, "already_archived": True}]


def test_archive_success_writes_active_and_preserves_archived(batch_db, invoke, monkeypatch):
    path, connection = batch_db
    active = add_memory(connection)
    archived = add_memory(connection, status="archived")
    calls = []
    original_archive = lifecycle._archive_entity_unchecked

    def record_archive(conn, memory_id):
        calls.append(memory_id)
        return original_archive(conn, memory_id)

    monkeypatch.setattr(lifecycle, "_archive_entity_unchecked", record_archive)
    archived_before = connection.execute(
        "SELECT updated_at, status FROM entities WHERE id = ?", (archived,)
    ).fetchone()
    result = invoke("archive_memory", [active, archived])
    assert result["status"] == "ok"
    assert result["data"]["archived_count"] == 1
    assert result["data"]["already_archived_count"] == 1
    active_read = lifecycle.inspect_memory(active, db_path=path)
    archived_read = lifecycle.inspect_memory(archived, db_path=path)
    assert active_read["status"] == "ok"
    assert calls == [active]
    assert active_read["data"]["status"] == "archived"
    assert archived_read["data"]["status"] == "archived"
    archived_after = connection.execute(
        "SELECT updated_at, status FROM entities WHERE id = ?", (archived,)
    ).fetchone()
    assert archived_after == archived_before


def test_archive_batch_releases_savepoint_on_rejection(batch_db):
    path, connection = batch_db
    memory_id = add_memory(connection)
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        result = lifecycle.archive_memories(
            [memory_id, str(uuid.uuid4())],
            db_connection=connection,
            db_path=path,
        )
    finally:
        connection.set_trace_callback(None)
    assert result["status"] == "rejected"
    savepoints = [
        statement.split("SAVEPOINT ", 1)[1]
        for statement in statements
        if statement.startswith("SAVEPOINT archive_batch_")
    ]
    assert len(savepoints) == 1
    assert f"ROLLBACK TO {savepoints[0]}" in statements
    assert f"RELEASE {savepoints[0]}" in statements


def test_archive_trigger_once_for_write_and_never_for_rejection(batch_db, invoke, monkeypatch):
    connection = batch_db[1]
    active = add_memory(connection)
    archived = add_memory(connection, status="archived", axis=1)
    community_detection = __import__(
        "saltmdb.domain.services.community_detection_service",
        fromlist=["trigger_community_detection"],
    )
    trigger = Mock(wraps=community_detection.trigger_community_detection)
    monkeypatch.setattr(community_detection, "trigger_community_detection", trigger)
    result = invoke("archive_memory", [active])
    assert result["status"] == "ok"
    assert trigger.call_count == 1
    rejected = invoke("archive_memory", [archived, str(uuid.uuid4())])
    assert rejected["status"] == "rejected"
    assert trigger.call_count == 1


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_relation_internal_error_stops_later_items(batch_db, mode, monkeypatch):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    before = snapshot(path)
    original = relation_service.store_relation
    calls = []

    def failing_store(*args, **kwargs):
        calls.append(kwargs["source_id"])
        if len(calls) == 2:
            return {
                "status": "rejected",
                "errors": [{"code": "INTERNAL_ERROR", "message": "injected"}],
            }
        return original(*args, **kwargs)

    monkeypatch.setattr(relation_service, "store_relation", failing_store)
    coordinator = DbWriteCoordinator(path) if mode == "coordinator" else None
    if coordinator is not None:
        coordinator.start()
    try:
        payload = [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "part_of"},
            {"source_id": third, "target_id": first, "predicate": "part_of"},
        ]
        if coordinator is None:
            result = relation_service.apply_relations(payload, db_path=path)
            assert result["status"] == "rejected"
            assert result["errors"][0]["code"] == "INTERNAL_ERROR"
        else:
            with pytest.raises(RuntimeError, match="injected"):
                dispatch_tool(
                    "manage_relation",
                    {"mode": "batch", "relations": payload},
                    coordinator,
                )
        assert calls == [first, second]
        assert snapshot(path) == before
    finally:
        if coordinator is not None:
            coordinator.shutdown(timeout=5)


def test_dispatch_inner_passes_archive_coordinator(monkeypatch):
    class FakeCoordinator:
        def __init__(self):
            self.calls = []

        def submit(self, key, job, priority):
            self.calls.append((key, priority))
            return job(object())

    coordinator = FakeCoordinator()
    with patch(
        "saltmdb.domain.services.memory_service.lifecycle.archive_memories",
        return_value={"status": "ok"},
    ) as archive:
        result = dispatch._dispatch_tool_inner(
            "archive_memory",
            {
                "mode": "batch",
                "archive_requests": ["memory-id"],
                "agent_id": "owner",
            },
            coordinator,
        )
    assert result["status"] == "ok"
    assert coordinator.calls == [("tool:archive_memory", "foreground")]
    archive.assert_called_once_with(
        archive_requests=["memory-id"], agent_id="owner", coordinator=coordinator
    )


@pytest.mark.parametrize("mode", ["direct", "coordinator"])
def test_archive_unexpected_exception_is_not_swallowed(batch_db, mode, monkeypatch):
    path, connection = batch_db
    memory_id = add_memory(connection)

    def explode(*args, **kwargs):
        raise RuntimeError("injected archive failure")

    monkeypatch.setattr(lifecycle, "_archive_entity_unchecked", explode)
    coordinator = DbWriteCoordinator(path) if mode == "coordinator" else None
    if coordinator is not None:
        coordinator.start()
    try:
        if coordinator is None:
            result = lifecycle.archive_memories([memory_id], db_path=path)
            assert result["status"] == "rejected"
            assert result["errors"][0]["code"] == "INTERNAL_ERROR"
        else:
            with pytest.raises(RuntimeError, match="injected archive failure"):
                dispatch_tool(
                    "archive_memory",
                    {"mode": "batch", "archive_requests": [memory_id]},
                    coordinator,
                )
    finally:
        if coordinator is not None:
            coordinator.shutdown(timeout=5)


def test_archive_reports_all_errors_in_input_order(batch_db):
    path, _ = batch_db
    requests = [str(uuid.uuid4()), "", str(uuid.uuid4())]
    result = lifecycle.archive_memories(requests, db_path=path)
    assert result["status"] == "rejected"
    assert [(error["code"], error["field"]) for error in result["errors"]] == [
        ("NOT_FOUND", "memory_ids[0]"),
        ("VALIDATION_ERROR", "memory_ids[1]"),
        ("NOT_FOUND", "memory_ids[2]"),
    ]


def test_archive_prefix_success_returns_resolved_full_id(batch_db):
    path, connection = batch_db
    memory_id = add_memory(connection)
    result = lifecycle.archive_memories([memory_id[:12]], db_path=path)
    assert result["status"] == "ok"
    assert result["data"]["items"] == [{"memory_id": memory_id, "already_archived": False}]


def test_archive_owner_check_requires_both_owner_values(batch_db):
    path, connection = batch_db
    caller_only = add_memory(connection, owner=None)
    stored_only = add_memory(connection, owner="stored", axis=1)
    both_set = add_memory(connection, owner="stored", axis=2)
    caller_only_result = lifecycle.archive_memories([caller_only], agent_id="caller", db_path=path)
    stored_only_result = lifecycle.archive_memories([stored_only], agent_id=None, db_path=path)
    conflict = lifecycle.archive_memories([both_set], agent_id="caller", db_path=path)
    assert caller_only_result["status"] == "ok"
    assert stored_only_result["status"] == "ok"
    assert conflict["status"] == "rejected"
    assert conflict["errors"][0]["code"] == "CONFLICT"


def test_archive_duplicate_prefix_and_full_id(batch_db):
    path, connection = batch_db
    memory_id = add_memory(connection)
    result = lifecycle.archive_memories([memory_id[:8], memory_id], db_path=path)
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "DUPLICATE_ID"
    assert result["errors"][0]["field"] == "memory_ids[1]"
    assert snapshot(path)["entities"][0][0] == memory_id


def test_relation_trigger_once_only_when_graph_changes(batch_db, invoke, monkeypatch):
    _, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    third = add_memory(connection, axis=2)
    community_detection = __import__(
        "saltmdb.domain.services.community_detection_service",
        fromlist=["trigger_community_detection"],
    )
    trigger = Mock(wraps=community_detection.trigger_community_detection)
    monkeypatch.setattr(community_detection, "trigger_community_detection", trigger)
    created = invoke(
        "manage_relation",
        [
            {"source_id": first, "target_id": second, "predicate": "part_of"},
            {"source_id": second, "target_id": third, "predicate": "part_of"},
        ],
    )
    assert created["status"] == "ok"
    assert created["data"]["created_count"] == 2
    assert trigger.call_count == 1
    duplicate = invoke(
        "manage_relation",
        [{"source_id": first, "target_id": second, "predicate": "part_of"}],
    )
    assert duplicate["status"] == "ok"
    assert duplicate["data"]["duplicate_count"] == 1
    assert trigger.call_count == 1
    rejected = invoke(
        "manage_relation",
        [{"source_id": "missing", "target_id": second, "predicate": "part_of"}],
    )
    assert rejected["status"] == "rejected"
    assert trigger.call_count == 1


def test_archive_rejects_empty_title_and_reports_ambiguous_prefix(batch_db):
    path, connection = batch_db
    first = add_memory(connection, memory_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
    add_memory(connection, memory_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")
    before = snapshot(path)
    empty = lifecycle.archive_memories([], db_path=path)
    assert empty["status"] == "rejected"
    assert empty["errors"][0]["field"] == "memory_ids"
    title = connection.execute("SELECT title FROM entities WHERE id = ?", (first,)).fetchone()[0]
    by_title = lifecycle.archive_memories([title], db_path=path)
    assert by_title["status"] == "rejected"
    assert by_title["errors"][0]["code"] == "NOT_FOUND"
    ambiguous = lifecycle.archive_memories(["aaaaaaaa"], db_path=path)
    assert ambiguous["status"] == "rejected"
    assert ambiguous["errors"][0]["code"] == "AMBIGUOUS_ID_PREFIX"
    assert snapshot(path) == before


def test_relation_collects_multiple_expected_rejections_without_changes(batch_db):
    path, connection = batch_db
    first = add_memory(connection)
    second = add_memory(connection, axis=1)
    before = snapshot(path)
    result = relation_service.apply_relations(
        [
            {"source_id": first, "target_id": second, "predicate": "completely_made_up"},
            {"source_id": "missing", "target_id": second, "predicate": "part_of"},
        ],
        db_path=path,
    )
    assert result["status"] == "rejected"
    assert [error["field"] for error in result["errors"]] == [
        "relations[0].predicate",
        "relations[1]",
    ]
    assert snapshot(path) == before


def test_archive_trigger_failure_does_not_reject_persisted_batch(batch_db, monkeypatch):
    path, connection = batch_db
    memory_id = add_memory(connection)
    community_detection = __import__(
        "saltmdb.domain.services.community_detection_service",
        fromlist=["trigger_community_detection"],
    )

    def raise_trigger(*args, **kwargs):
        raise RuntimeError("injected trigger failure")

    monkeypatch.setattr(community_detection, "trigger_community_detection", raise_trigger)
    with pytest.raises(RuntimeError, match="injected trigger failure"):
        lifecycle.archive_memories([memory_id], db_path=path)
    read_result = lifecycle.inspect_memory(memory_id, db_path=path)
    assert read_result["status"] == "ok"
    assert read_result["data"]["status"] == "archived"
