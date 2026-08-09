import unittest
import tempfile
import os
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

    def test_store_memory_creates_durable_embedding_jobs(self):
        res = store_memory(
            title="Async Embedding Test Memory",
            content="Content for testing async background embedding generation worker pool",
            owner_id="user1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        entity_id = res.split("ID: ")[1]

        states = self.conn.execute(
            "SELECT job_kind, state FROM embedding_jobs WHERE entity_id=? ORDER BY job_kind", (entity_id,)
        ).fetchall()
        self.assertEqual(states, [("chunk", "queued"), ("entity", "queued")])


if __name__ == "__main__":
    unittest.main()
