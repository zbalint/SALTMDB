"""Read-only corpus snapshot service tests using temporary databases only."""

from __future__ import annotations

from pathlib import Path

import pytest

from saltmdb.db.connection import close_connection, get_connection, write_transaction_retrying
from saltmdb.db.schema import init_db
from saltmdb.domain.services.corpus_snapshot_service import (
    CorpusSnapshotError,
    SnapshotChangedError,
    export_corpus_snapshot_page,
    iter_corpus_snapshot_pages,
)


def _insert_entity(
    conn,
    entity_id: str,
    *,
    status: str = "raw",
    title: str | None = None,
    owner_id: str = "snapshot-owner",
    scope: str = "private",
) -> None:
    value = title or f"Title {entity_id}"
    write_transaction_retrying(
        conn,
        lambda c: c.execute(
            """
            INSERT INTO entities (
                id, created_at, updated_at, last_accessed_at, owner_id, scope, status,
                title, full_content, content_hash, metadata, context_id, memory_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
                owner_id,
                scope,
                status,
                value,
                f"Body for {entity_id}",
                f"source-{entity_id}",
                '{"source_path":"fixtures/' + entity_id + '.md"}',
                "snapshot-context",
                "fact",
            ),
        ),
    )


@pytest.fixture
def snapshot_db(tmp_path: Path):
    path = tmp_path / "snapshot.db"
    conn = init_db(str(path))
    _insert_entity(conn, "entity-b")
    _insert_entity(conn, "entity-a")
    _insert_entity(conn, "entity-z", status="archived")
    try:
        yield path, conn
    finally:
        close_connection(conn)


def test_snapshot_pages_are_keyset_ordered_and_provenance_bound(snapshot_db):
    path, conn = snapshot_db
    first = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=1, db_connection=conn
    )

    assert [entity["id"] for entity in first["entities"]] == ["entity-a"]
    assert first["next_cursor"] == "entity-a"
    assert first["has_more"] is True
    assert first["entity_count"] == 2
    assert first["schema_hash"] == first["provenance"]["schema_hash"]
    assert first["database_hash"] == first["provenance"]["database_hash"]
    assert len(first["snapshot_hash"]) == 64
    assert first["entities"][0]["body"] == "Body for entity-a"
    assert first["entities"][0]["source_metadata"] == '{"source_path":"fixtures/entity-a.md"}'
    assert first["entities"][0]["source_hash"] == "source-entity-a"

    second = export_corpus_snapshot_page(
        owner_id="snapshot-owner",
        page_size=1,
        cursor=first["next_cursor"],
        snapshot_hash=first["snapshot_hash"],
        db_connection=conn,
    )
    assert [entity["id"] for entity in second["entities"]] == ["entity-b"]
    assert second["next_cursor"] is None
    assert second["has_more"] is False
    assert second["snapshot_hash"] == first["snapshot_hash"]
    assert path.exists()


def test_snapshot_can_include_archived_entities_without_mixing_modes(snapshot_db):
    _, conn = snapshot_db
    active = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=10, db_connection=conn
    )
    all_rows = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=10, include_archived=True, db_connection=conn
    )

    assert [entity["id"] for entity in active["entities"]] == ["entity-a", "entity-b"]
    assert [entity["id"] for entity in all_rows["entities"]] == [
        "entity-a",
        "entity-b",
        "entity-z",
    ]
    assert active["snapshot_hash"] != all_rows["snapshot_hash"]


def test_snapshot_scopes_private_entities_to_owner_but_includes_shared_entities(snapshot_db):
    _, conn = snapshot_db
    _insert_entity(conn, "other-private", owner_id="other-owner", scope="private")
    _insert_entity(conn, "other-shared", owner_id="other-owner", scope="shared")

    page = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=20, db_connection=conn
    )

    assert [entity["id"] for entity in page["entities"]] == [
        "entity-a",
        "entity-b",
        "other-shared",
    ]
    assert page["owner_id"] == "snapshot-owner"
    assert page["provenance"]["owner_id"] == "snapshot-owner"


def test_snapshot_requires_owner_id(snapshot_db):
    _, conn = snapshot_db
    with pytest.raises(CorpusSnapshotError, match="owner_id is mandatory"):
        export_corpus_snapshot_page(db_connection=conn)
    with pytest.raises(CorpusSnapshotError, match="owner_id is mandatory"):
        export_corpus_snapshot_page(owner_id="", db_connection=conn)


def test_snapshot_exports_current_supersedes_edges_only_between_visible_entities(snapshot_db):
    _, conn = snapshot_db
    _insert_entity(conn, "other-private", owner_id="other-owner", scope="private")
    _insert_entity(conn, "other-shared", owner_id="other-owner", scope="shared")
    write_transaction_retrying(
        conn,
        lambda c: c.executemany(
            """
            INSERT INTO relations
                (id, source_id, target_id, predicate, valid_from, valid_to, valid_at, invalid_at)
            VALUES (?, ?, ?, 'supersedes', ?, ?, ?, ?)
            """,
            [
                ("rel-visible", "entity-b", "entity-a", None, None, None, None),
                ("rel-hidden", "other-private", "entity-a", None, None, None, None),
                (
                    "rel-future",
                    "entity-a",
                    "entity-b",
                    "2999-01-01T00:00:00+00:00",
                    None,
                    None,
                    None,
                ),
            ],
        ),
    )

    page = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=20, db_connection=conn
    )

    assert [edge["id"] for edge in page["supersedes_edges"]] == ["rel-visible"]
    assert page["relations"] == page["supersedes_edges"]
    assert page["relation_count"] == 1
    assert page["relation_root_hash"] == page["relations_hash"]
    assert len(page["relation_root_hash"]) == 64


def test_snapshot_fails_closed_when_corpus_changes_between_pages(snapshot_db):
    _, conn = snapshot_db
    first = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=1, db_connection=conn
    )
    _insert_entity(conn, "entity-c")

    with pytest.raises(SnapshotChangedError, match="changed"):
        export_corpus_snapshot_page(
            owner_id="snapshot-owner",
            page_size=1,
            cursor=first["next_cursor"],
            snapshot_hash=first["snapshot_hash"],
            db_connection=conn,
        )


def test_iter_pages_enforces_provenance_between_calls(snapshot_db):
    path, conn = snapshot_db
    pages = iter_corpus_snapshot_pages(
        owner_id="snapshot-owner", page_size=1, db_connection=conn
    )
    first = next(pages)
    external = get_connection(str(path))
    try:
        _insert_entity(external, "entity-c")
    finally:
        close_connection(external)

    with pytest.raises(SnapshotChangedError, match="changed"):
        list(pages)
    assert first["snapshot_hash"]


def test_schema_change_is_detected_between_pages(snapshot_db):
    _, conn = snapshot_db
    first = export_corpus_snapshot_page(
        owner_id="snapshot-owner", page_size=1, db_connection=conn
    )
    write_transaction_retrying(
        conn,
        lambda c: c.execute("ALTER TABLE entities ADD COLUMN snapshot_test_marker TEXT"),
    )

    with pytest.raises(SnapshotChangedError, match="changed"):
        export_corpus_snapshot_page(
            owner_id="snapshot-owner",
            page_size=1,
            cursor=first["next_cursor"],
            snapshot_hash=first["snapshot_hash"],
            db_connection=conn,
        )


def test_snapshot_rejects_open_transactions_and_invalid_page_requests(snapshot_db):
    _, conn = snapshot_db
    conn.execute("BEGIN")
    with pytest.raises(CorpusSnapshotError, match="open transaction"):
        export_corpus_snapshot_page(owner_id="snapshot-owner", db_connection=conn)
    conn.execute("ROLLBACK")

    with pytest.raises(CorpusSnapshotError, match="between 1"):
        export_corpus_snapshot_page(owner_id="snapshot-owner", page_size=0, db_connection=conn)
    with pytest.raises(CorpusSnapshotError, match="lowercase SHA-256"):
        export_corpus_snapshot_page(
            owner_id="snapshot-owner", snapshot_hash="not-a-hash", db_connection=conn
        )


def test_snapshot_read_transaction_has_no_write_statements(snapshot_db):
    _, conn = snapshot_db
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        export_corpus_snapshot_page(
            owner_id="snapshot-owner", page_size=1, db_connection=conn
        )
    finally:
        conn.set_trace_callback(None)

    assert any(statement.startswith("BEGIN") for statement in statements)
    assert any(statement.startswith("COMMIT") for statement in statements)
    assert not any(
        statement.lstrip().upper().startswith(prefix)
        for statement in statements
        for prefix in ("INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE")
    )
