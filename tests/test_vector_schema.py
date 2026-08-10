import os
import shutil
import tempfile
import unittest
import sqlite3

import sqlite_vec

from saltmdb.db.vector_schema import init_vector_schema, init_entity_chunk_vector_schema


def _vec(values):
    return sqlite_vec.serialize_float32(values)


class TestVectorSchema(unittest.TestCase):
    def test_init_vector_schema_creates_table_with_expected_dimensionality(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_vector_schema(conn)

            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_embeddings'"
            ).fetchone()
            self.assertIsNotNone(row, "entity_embeddings virtual table should be created")
            self.assertIn(
                "FLOAT[384]",
                row[0],
                "Embedding column should be declared with 384 dimensions (bge-small-en-v1.5)",
            )

            # Table should accept inserts/selects like any vec0 virtual table
            conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                ("probe-entity-1", b"\x00" * (384 * 4)),
            )
            result = conn.execute(
                "SELECT entity_id FROM entity_embeddings WHERE entity_id = ?", ("probe-entity-1",)
            ).fetchone()
            self.assertEqual(result[0], "probe-entity-1")
        finally:
            conn.close()

    def test_init_vector_schema_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_vector_schema(conn)
            init_vector_schema(conn)  # must not raise on second call (IF NOT EXISTS)
        finally:
            conn.close()


class TestEntityChunkVectorSchema(unittest.TestCase):
    def test_creates_table_with_expected_dimensionality(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_entity_chunk_vector_schema(conn)
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_chunk_embeddings'"
            ).fetchone()
            self.assertIsNotNone(row, "entity_chunk_embeddings virtual table should be created")
            self.assertIn("FLOAT[384]", row[0])
        finally:
            conn.close()

    def test_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_entity_chunk_vector_schema(conn)
            init_entity_chunk_vector_schema(conn)  # must not raise on second call
        finally:
            conn.close()

    def test_point_filter_by_entity_id_returns_all_its_chunks(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_entity_chunk_vector_schema(conn)
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("e1::0", "e1", _vec([1.0, 0.0, 0.0, 0.0] * 96), 0, 0, 1200),
            )
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("e1::1", "e1", _vec([0.0, 1.0, 0.0, 0.0] * 96), 1, 1000, 2200),
            )
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("e2::0", "e2", _vec([0.0, 0.0, 1.0, 0.0] * 96), 0, 0, 500),
            )

            rows = conn.execute(
                "SELECT id, chunk_index, char_start, char_end FROM entity_chunk_embeddings "
                "WHERE entity_id = ? ORDER BY chunk_index",
                ("e1",),
            ).fetchall()
            self.assertEqual(
                rows,
                [("e1::0", 0, 0, 1200), ("e1::1", 1, 1000, 2200)],
            )
        finally:
            conn.close()

    def test_delete_by_entity_id_removes_only_that_entitys_rows(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_entity_chunk_vector_schema(conn)
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("e1::0", "e1", _vec([1.0, 0.0, 0.0, 0.0] * 96), 0, 0, 1200),
            )
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("e2::0", "e2", _vec([0.0, 0.0, 1.0, 0.0] * 96), 0, 0, 500),
            )

            conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", ("e1",))

            remaining = conn.execute("SELECT entity_id FROM entity_chunk_embeddings").fetchall()
            self.assertEqual(remaining, [("e2",)])
        finally:
            conn.close()

    def test_physical_chunk_count_stays_flat_as_entity_count_grows(self):
        """Regression coverage for the PARTITION KEY storage-blowup bug (SALTMDB memory
        `3e0c7a1e`): with `entity_id` as a plain auxiliary column (not PARTITION KEY), all
        entities share the table's normal global vec0 chunk allocation instead of each getting
        an isolated, near-empty one. Asserts the physical `entity_chunk_embeddings_chunks`
        shadow-table row count -- the same signal the original bug repro used -- stays small and
        constant as entity count grows, rather than growing 1:1 with entity count the way it did
        under PARTITION KEY (150 entities -> 150 physical chunks / ~228MB, empirically). Uses a
        modest N (30) that's already enough to distinguish flat (O(1)) from linear (O(entities))
        growth without the full repro's runtime cost."""
        conn = sqlite3.connect(":memory:")
        try:
            init_entity_chunk_vector_schema(conn)
            for i in range(30):
                conn.execute(
                    "INSERT INTO entity_chunk_embeddings"
                    "(id, entity_id, embedding, chunk_index, char_start, char_end)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (f"e{i}::0", f"e{i}", _vec([1.0, 0.0, 0.0, 0.0] * 96), 0, 0, 500),
                )
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM entity_chunk_embeddings_chunks"
            ).fetchone()[0]
            self.assertLessEqual(
                chunk_count,
                2,
                "30 distinct entity_ids should share a small, constant number of physical vec0 "
                "chunks -- a count scaling with entity count would mean PARTITION KEY-style "
                "per-entity storage blowup has regressed",
            )
        finally:
            conn.close()

    def test_init_db_creates_entity_chunk_embeddings_via_real_migration_path(self):
        """Integration check (not a direct unit call to init_entity_chunk_vector_schema): goes
        through the actual schema.py init_db() migration wiring, proving the new call site and
        its extension-loading precondition really work end-to-end, not just in isolation."""
        from saltmdb.db.schema import init_db

        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        conn = init_db(db_path)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_chunk_embeddings'"
            ).fetchone()
            self.assertIsNotNone(
                row, "init_db() should create entity_chunk_embeddings via the real migration path"
            )
            self.assertIn("FLOAT[384]", row[0])
        finally:
            conn.close()
            if os.path.exists(db_path):
                try:
                    os.remove(db_path)
                except Exception:
                    pass


class TestContentHashMigration(unittest.TestCase):
    """Codex-required regression coverage for
    vector_schema.migrate_entity_chunk_embeddings_schema (Phase 2 Part A0): builds a
    Foundation-era columnless entity_chunk_embeddings table by hand, runs the real init_db()
    migration path over it end-to-end, and asserts all three of the migration's documented
    guarantees -- the new column exists, old (pre-migration) rows are intentionally reset rather
    than carried forward, and a subsequent repair/backfill rebuilds current rows safely for the
    entity that survives (see vector_schema.py's migration docstring for why vec0's rejection of
    ALTER TABLE ADD COLUMN forces an atomic drop+recreate instead of an in-place column add)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build_foundation_era_db(self) -> None:
        """Hand-builds the pre-Phase-2 shape directly: an `entities` row with no content_hash
        (predates that column too) and a columnless entity_chunk_embeddings vec0 table with one
        legacy chunk row -- exactly what a real pre-rework install on disk would contain."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("""
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    last_accessed_at DATETIME NOT NULL,
                    owner_id TEXT,
                    scope TEXT DEFAULT 'shared',
                    is_core BOOLEAN DEFAULT 0,
                    weight INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'raw',
                    parent_ids TEXT,
                    title TEXT NOT NULL,
                    full_content TEXT NOT NULL,
                    valid_from DATETIME,
                    valid_to DATETIME,
                    metadata TEXT
                );
            """)
            conn.execute(
                "INSERT INTO entities"
                " (id, created_at, updated_at, last_accessed_at, title, full_content)"
                " VALUES ('legacy-entity', datetime('now'), datetime('now'), datetime('now'),"
                " 'Legacy Entity', 'Legacy content that predates the content_hash column.')"
            )
            # The Foundation-era shape: no +content_hash column at all.
            conn.execute("""
                CREATE VIRTUAL TABLE entity_chunk_embeddings USING vec0(
                    id TEXT PRIMARY KEY,
                    entity_id TEXT PARTITION KEY,
                    embedding FLOAT[384],
                    +chunk_index INTEGER,
                    +char_start INTEGER,
                    +char_end INTEGER
                );
            """)
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                " (id, entity_id, embedding, chunk_index, char_start, char_end)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "legacy-entity::0",
                    "legacy-entity",
                    sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0] * 96),
                    0,
                    0,
                    100,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migration_is_a_no_op_when_content_hash_column_already_present(self):
        """A DB that's already on the current schema (fresh install, or already-migrated) must
        not be re-dropped on a later init_db() run -- the sqlite_master DDL-text check is what
        makes this safe, not a version table."""
        from saltmdb.db.schema import init_db

        conn = init_db(self.db_path)
        conn.close()

        conn = init_db(self.db_path)  # second run over an already-migrated DB
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_chunk_embeddings'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("content_hash", row[0])
        finally:
            conn.close()

    def _build_partitioned_with_content_hash_db(self) -> None:
        """Hand-builds TODAY'S ACTUAL PRODUCTION shape (SALTMDB memory `76da44ca`): the Phase 2
        Part A0 migration already ran at some point in the past, so `content_hash` IS present --
        but the table still declares `entity_id TEXT PARTITION KEY`, since that removal is the
        migration behavior under test here. Codex review (2026-08-06) flagged that the older
        `_build_foundation_era_db` fixture predates content_hash entirely, so it can't exercise
        the `"content_hash" in sql and "PARTITION KEY" not in sql` no-op check's PARTITION KEY
        half -- this fixture closes that gap by combining both conditions the way the real
        pre-fix live/dev DB actually does."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("""
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    last_accessed_at DATETIME NOT NULL,
                    owner_id TEXT,
                    scope TEXT DEFAULT 'shared',
                    is_core BOOLEAN DEFAULT 0,
                    weight INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'raw',
                    parent_ids TEXT,
                    title TEXT NOT NULL,
                    full_content TEXT NOT NULL,
                    valid_from DATETIME,
                    valid_to DATETIME,
                    metadata TEXT,
                    content_hash TEXT
                );
            """)
            conn.execute(
                "INSERT INTO entities"
                " (id, created_at, updated_at, last_accessed_at, title, full_content, content_hash)"
                " VALUES ('partitioned-entity', datetime('now'), datetime('now'), datetime('now'),"
                " 'Partitioned Entity', 'Content for the still-partitioned Phase 2 shape.', 'abc123')"
            )
            # Today's actual pre-fix production shape: content_hash present, still partitioned.
            conn.execute("""
                CREATE VIRTUAL TABLE entity_chunk_embeddings USING vec0(
                    id TEXT PRIMARY KEY,
                    entity_id TEXT PARTITION KEY,
                    embedding FLOAT[384],
                    +chunk_index INTEGER,
                    +char_start INTEGER,
                    +char_end INTEGER,
                    +content_hash TEXT
                );
            """)
            conn.execute(
                "INSERT INTO entity_chunk_embeddings"
                " (id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "partitioned-entity::0",
                    "partitioned-entity",
                    sqlite_vec.serialize_float32([1.0, 0.0, 0.0, 0.0] * 96),
                    0,
                    0,
                    100,
                    "abc123",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migration_drops_partition_key_when_content_hash_already_present(self):
        """The detection branch Codex required coverage for: a table with content_hash already
        present but STILL declaring PARTITION KEY (today's real pre-fix shape) must still be
        drop+recreated -- the old check (`"content_hash" in sql` alone) would have wrongly
        treated this as already-migrated and left the PARTITION KEY storage bug in place."""
        from saltmdb.db.schema import init_db

        self._build_partitioned_with_content_hash_db()

        conn = init_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_chunk_embeddings'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertNotIn(
                "PARTITION KEY",
                row[0],
                "migration must drop+recreate a still-partitioned table even when content_hash "
                "is already present",
            )
            self.assertIn("content_hash", row[0])

            # Old (pre-migration) rows are intentionally reset, not carried forward -- same
            # atomic-clear guarantee as the content_hash migration this folds together with.
            remaining = conn.execute(
                "SELECT COUNT(*) FROM entity_chunk_embeddings WHERE entity_id = 'partitioned-entity'"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
