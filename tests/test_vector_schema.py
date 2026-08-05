import os
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

    def test_knn_scoped_to_partition_only_returns_that_entitys_chunks(self):
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
                ("e2::0", "e2", _vec([1.0, 0.0, 0.0, 0.0] * 96), 0, 0, 500),
            )

            query_vec = _vec([1.0, 0.0, 0.0, 0.0] * 96)
            rows = conn.execute(
                "SELECT id FROM entity_chunk_embeddings "
                "WHERE embedding MATCH ? AND k = 5 AND entity_id = ? ORDER BY distance",
                (query_vec, "e1"),
            ).fetchall()
            self.assertEqual(rows, [("e1::0",)])
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


if __name__ == "__main__":
    unittest.main()
