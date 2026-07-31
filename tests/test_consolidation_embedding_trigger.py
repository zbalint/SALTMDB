import unittest
import tempfile
import os
import time
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation


class TestConsolidationEmbeddingTrigger(unittest.TestCase):
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

    def test_commit_consolidation_triggers_embedding_generation(self):
        res1 = store_memory(
            title="Parent Fact A",
            content="Detailed description of Fact A for testing consolidation embedding",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id1 = res1.split("ID: ")[1]

        res2 = store_memory(
            title="Parent Fact B",
            content="Detailed description of Fact B for testing consolidation embedding",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id2 = res2.split("ID: ")[1]

        # Deliberately pass only db_connection (no db_path), matching how
        # bulk_commit_consolidation and other callers invoke this function.
        c_res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Overview",
            content="Merged summary of A and B",
            tags=["#summary"],
            owner_id="agent1",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", c_res)
        consolidated_id = c_res.split("ID: ")[-1].strip()

        # Wait up to 5 seconds for background embedding thread pool execution
        ready = False
        for _ in range(50):
            row = self.conn.execute(
                "SELECT embedding_status FROM entities WHERE id = ?", (consolidated_id,)
            ).fetchone()
            if row and row[0] == "ready":
                ready = True
                break
            time.sleep(0.1)

        self.assertTrue(
            ready, "Consolidated entity's embedding status did not transition to 'ready'"
        )


if __name__ == "__main__":
    unittest.main()
