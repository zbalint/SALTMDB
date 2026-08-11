import unittest
import tempfile
import os
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

    def test_commit_consolidation_creates_durable_embedding_jobs(self):
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

        states = self.conn.execute(
            "SELECT job_kind, state FROM embedding_jobs WHERE entity_id=? ORDER BY job_kind",
            (consolidated_id,),
        ).fetchall()
        self.assertEqual(states, [("chunk", "queued"), ("entity", "queued")])


if __name__ == "__main__":
    unittest.main()
