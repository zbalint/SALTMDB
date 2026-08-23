import unittest
import tempfile
import os
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import archive_memory, revise_memory, store_memory


class TestArchivedEmbeddingStatus(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_archived_memory_has_archived_embedding_status(self):
        res = store_memory(
            title="Test Unique Memory",
            content="Some unique content to test archived embedding status",
            owner_id="test_user",
            db_path=self.db_path,
        )
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]

        zero_vector = b"\x00" * (384 * 4)
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id,embedding) VALUES (?,?)",
            (entity_id, zero_vector),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings "
            "(id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"{entity_id}::0", entity_id, zero_vector, 0, 0, 4, "hash"),
        )

        # Verify status initially raw
        row = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "raw")

        # Archive memory
        archive_memory(entity_id=entity_id, owner_id="test_user", db_path=self.db_path)

        row = self.conn.execute(
            "SELECT status, embedding_status FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "archived")
        self.assertEqual(row[1], "archived")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_chunk_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )

    def test_revision_predecessor_has_archived_embedding_status(self):
        res = store_memory(
            title="Original Memory Entry",
            content="Original content text block",
            owner_id="test_user",
            db_path=self.db_path,
        )
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]

        zero_vector = b"\x00" * (384 * 4)
        self.conn.execute(
            "INSERT INTO entity_embeddings (entity_id,embedding) VALUES (?,?)",
            (entity_id, zero_vector),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings "
            "(id,entity_id,embedding,chunk_index,char_start,char_end,content_hash) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"{entity_id}::0", entity_id, zero_vector, 0, 0, 4, "hash"),
        )

        # Immutable revision archives the predecessor and creates a new ID.
        result = revise_memory(
            title="Updated Memory Entry",
            content="Updated content text block with enough descriptive detail.",
            tags=["#updated"],
            reason="Correct the representation.",
            entity_id=entity_id,
            owner_id="test_user",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "ok")

        rows = self.conn.execute(
            "SELECT id, status, embedding_status FROM entities WHERE status = 'archived'"
        ).fetchall()
        self.assertTrue(len(rows) > 0)
        for r in rows:
            self.assertEqual(r[2], "archived")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entity_chunk_embeddings WHERE entity_id=?", (entity_id,)
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
