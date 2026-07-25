import unittest
import sqlite3
from saltmdb.db.vector_schema import init_vector_schema

class TestVectorSchema(unittest.TestCase):
    def test_init_vector_schema_creates_table_with_expected_dimensionality(self):
        conn = sqlite3.connect(":memory:")
        try:
            init_vector_schema(conn)

            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'entity_embeddings'"
            ).fetchone()
            self.assertIsNotNone(row, "entity_embeddings virtual table should be created")
            self.assertIn("FLOAT[384]", row[0], "Embedding column should be declared with 384 dimensions (bge-small-en-v1.5)")

            # Table should accept inserts/selects like any vec0 virtual table
            conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                ("probe-entity-1", b"\x00" * (384 * 4))
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

if __name__ == "__main__":
    unittest.main()
