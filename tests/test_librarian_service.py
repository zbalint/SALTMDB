import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation

class TestLibrarianService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commit_consolidation_soft_archives_parents_and_links_lineage(self):
        res1 = store_memory(title="Parent Fact A", content="Fact A details", owner_id="agent1", skip_duplicate_check=True, db_path=self.db_path)
        id1 = res1.split("ID: ")[1]

        res2 = store_memory(title="Parent Fact B", content="Fact B details", owner_id="agent1", skip_duplicate_check=True, db_path=self.db_path)
        id2 = res2.split("ID: ")[1]

        c_res = commit_consolidation(
            parent_ids=[id1, id2],
            title="Consolidated Overview",
            content="Merged summary of A and B",
            tags=["#summary"],
            owner_id="agent1",
            db_connection=self.conn
        )
        self.assertIn("Successfully committed", c_res)

        # Verify parent status is archived
        p1 = self.conn.execute("SELECT status, embedding_status FROM entities WHERE id = ?", (id1,)).fetchone()
        p2 = self.conn.execute("SELECT status, embedding_status FROM entities WHERE id = ?", (id2,)).fetchone()
        self.assertEqual(p1[0], "archived")
        self.assertEqual(p1[1], "archived")
        self.assertEqual(p2[0], "archived")
        self.assertEqual(p2[1], "archived")

if __name__ == "__main__":
    unittest.main()
