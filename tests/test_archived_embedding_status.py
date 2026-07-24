import unittest
import tempfile
import os
import re
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, archive_memory

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
        res = store_memory(title="Test Unique Memory", content="Some unique content to test archived embedding status", owner_id="test_user", skip_duplicate_check=True, db_path=self.db_path)
        match = re.search(r"ID:\s*([a-f0-9\-]+)", res)
        self.assertIsNotNone(match, f"Could not parse entity ID from store_memory result: {res}")
        entity_id = match.group(1)
        
        # Verify status initially raw
        row = self.conn.execute("SELECT status, embedding_status FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "raw")

        # Archive memory
        archive_memory(entity_id=entity_id, owner_id="test_user", db_path=self.db_path)
        
        row = self.conn.execute("SELECT status, embedding_status FROM entities WHERE id = ?", (entity_id,)).fetchone()
        self.assertEqual(row[0], "archived")
        self.assertEqual(row[1], "archived")

    def test_scd2_history_has_archived_embedding_status(self):
        res = store_memory(title="Original Memory Entry", content="Original content text block", owner_id="test_user", skip_duplicate_check=True, db_path=self.db_path)
        match = re.search(r"ID:\s*([a-f0-9\-]+)", res)
        self.assertIsNotNone(match, f"Could not parse entity ID from store_memory result: {res}")
        entity_id = match.group(1)

        # Update memory (triggers SCD2 historical copy)
        store_memory(title="Updated Memory Entry", content="Updated content text block", entity_id=entity_id, owner_id="test_user", skip_duplicate_check=True, db_path=self.db_path)

        rows = self.conn.execute("SELECT id, status, embedding_status FROM entities WHERE status = 'archived'").fetchall()
        self.assertTrue(len(rows) > 0)
        for r in rows:
            self.assertEqual(r[2], "archived")

if __name__ == "__main__":
    unittest.main()
