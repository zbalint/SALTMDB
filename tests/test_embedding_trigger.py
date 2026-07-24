import unittest
import tempfile
import os
import time
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory

class TestEmbeddingTrigger(unittest.TestCase):
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

    def test_store_memory_triggers_embedding_generation(self):
        res = store_memory(
            title="Async Embedding Test Memory",
            content="Content for testing async background embedding generation worker pool",
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path
        )
        entity_id = res.split("ID: ")[1]

        # Wait up to 5 seconds for background embedding thread pool execution
        ready = False
        for _ in range(50):
            row = self.conn.execute("SELECT embedding_status FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if row and row[0] == "ready":
                ready = True
                break
            time.sleep(0.1)

        self.assertTrue(ready, "Embedding status did not transition to 'ready'")

if __name__ == "__main__":
    unittest.main()
