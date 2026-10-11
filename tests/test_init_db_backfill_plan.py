from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from saltmdb.db import schema
from saltmdb.db.schema import init_db


_BACKFILL_PLANS = (
    (
        "idx_entities_archived_embedding_pending",
        "UPDATE entities INDEXED BY idx_entities_archived_embedding_pending SET "
        "embedding_status = 'archived' WHERE status = 'archived' "
        "AND embedding_status IS NOT 'archived'",
    ),
    (
        "idx_entities_hash_missing",
        "SELECT id, full_content FROM entities INDEXED BY idx_entities_hash_missing "
        "WHERE content_hash IS NULL OR content_hash = ''",
    ),
    (
        "idx_entities_context_backfill",
        "UPDATE entities INDEXED BY idx_entities_context_backfill SET context_id = project_id "
        "WHERE context_id IS NULL AND project_id IS NOT NULL",
    ),
)


def _plan_details(connection: sqlite3.Connection, statement: str) -> list[str]:
    return [row[-1] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}")]


def test_init_db_backfills_legacy_rows_and_uses_partial_indexes(tmp_path: Path):
    db_path = tmp_path / "backfill.db"
    connection = init_db(str(db_path))
    connection.close()

    raw = sqlite3.connect(db_path)
    raw.executemany(
        """
        INSERT INTO entities (
            id, created_at, updated_at, last_accessed_at, status, title, full_content,
            embedding_status, content_hash, project_id, context_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "archived-pending",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "archived",
                "Archived",
                "Archived body",
                "pending",
                "already-hashed",
                None,
                None,
            ),
            (
                "missing-hash",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "raw",
                "Hash",
                "Hash body",
                "ready",
                None,
                None,
                None,
            ),
            (
                "missing-context",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "raw",
                "Context",
                "Context body",
                "ready",
                "already-hashed",
                "project-1",
                None,
            ),
        ],
    )
    raw.commit()
    raw.close()

    connection = init_db(str(db_path))
    rows = connection.execute(
        "SELECT id, embedding_status, content_hash, context_id FROM entities ORDER BY id"
    ).fetchall()
    assert rows[0][0:2] == ("archived-pending", "archived")
    assert rows[1][0] == "missing-context"
    assert rows[1][3] == "project-1"
    missing_hash = connection.execute(
        "SELECT content_hash FROM entities WHERE id = 'missing-hash'"
    ).fetchone()[0]
    assert missing_hash

    for index_name, statement in _BACKFILL_PLANS:
        details = _plan_details(connection, statement)
        plan = "\n".join(details)
        assert index_name in plan
        assert not any(
            "SCAN entities" in detail and "USING INDEX" not in detail for detail in details
        ), plan
    connection.close()


_EXPECTED_RELATION_INDEX_SQL = (
    "CREATE UNIQUE INDEX idx_relations_unique_edge "
    "ON relations(source_id, target_id, predicate) WHERE valid_to IS NULL"
)


def _relation_index_sql(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_relations_unique_edge'"
    ).fetchone()
    return row[0] if row else None


def _normalized_sql(sql: str | None) -> str:
    return " ".join((sql or "").split())


def test_fresh_init_db_creates_exact_partial_relation_index(tmp_path: Path):
    connection = init_db(str(tmp_path / "fresh.db"))
    try:
        assert _normalized_sql(_relation_index_sql(connection)) == _EXPECTED_RELATION_INDEX_SQL
    finally:
        connection.close()


def test_second_init_db_skips_relation_dedupe_and_index_rebuild(tmp_path: Path):
    db_path = tmp_path / "idempotent.db"
    connection = init_db(str(db_path))
    connection.close()

    statements: list[str] = []
    original_get_connection = schema.get_connection

    def traced_get_connection(path: str):
        traced = original_get_connection(path)
        traced.set_trace_callback(statements.append)
        return traced

    with patch.object(schema, "get_connection", side_effect=traced_get_connection):
        connection = init_db(str(db_path))
        connection.close()

    normalized = [_normalized_sql(statement).upper() for statement in statements]
    assert not any(statement.startswith("DELETE FROM RELATIONS") for statement in normalized)
    assert not any(
        statement.startswith("DROP INDEX IF EXISTS IDX_RELATIONS_UNIQUE_EDGE")
        for statement in normalized
    )


def test_missing_relation_index_migration_deduplicates_active_rows(tmp_path: Path):
    db_path = tmp_path / "missing-index.db"
    connection = init_db(str(db_path))
    connection.close()

    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX idx_relations_unique_edge")
    raw.executemany(
        """
        INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("duplicate-1", "source", "target", "depends_on", "2026-01-01", "2026-01-01", None),
            ("duplicate-2", "source", "target", "depends_on", "2026-01-02", "2026-01-02", None),
        ],
    )
    raw.commit()
    raw.close()

    connection = init_db(str(db_path))
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM relations "
                "WHERE source_id = 'source' AND target_id = 'target' "
                "AND predicate = 'depends_on' AND valid_to IS NULL"
            ).fetchone()[0]
            == 1
        )
        assert _normalized_sql(_relation_index_sql(connection)) == _EXPECTED_RELATION_INDEX_SQL
    finally:
        connection.close()


def test_old_relation_index_migration_rebuilds_partial_definition(tmp_path: Path):
    db_path = tmp_path / "old-index.db"
    connection = init_db(str(db_path))
    connection.close()

    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX idx_relations_unique_edge")
    raw.execute(
        "CREATE UNIQUE INDEX idx_relations_unique_edge "
        "ON relations(source_id, target_id, predicate)"
    )
    raw.commit()
    raw.close()

    connection = init_db(str(db_path))
    try:
        assert _normalized_sql(_relation_index_sql(connection)) == _EXPECTED_RELATION_INDEX_SQL
    finally:
        connection.close()
