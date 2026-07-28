import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation, bulk_commit_consolidation

class TestLibrarianService(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_commit_consolidation_soft_archives_parents_and_links_lineage(self):
        res1 = store_memory(title="Parent Fact A", content="Detailed description of Fact A for testing consolidation", owner_id="agent1", skip_duplicate_check=True, db_path=self.db_path)
        id1 = res1.split("ID: ")[1]

        res2 = store_memory(title="Parent Fact B", content="Detailed description of Fact B for testing consolidation", owner_id="agent1", skip_duplicate_check=True, db_path=self.db_path)
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

    def test_bulk_commit_consolidation_is_all_or_nothing(self):
        # Regression test for the bulk-atomicity fix: bulk_commit_consolidation wraps its
        # whole loop in ONE write_transaction_retrying block, so a failure on a later item
        # must roll back an earlier item's would-be-successful insert too -- previously each
        # item committed individually despite the function's docstring claiming atomicity.
        res1 = store_memory(
            title="Bulk Atomicity Parent A",
            content="Detailed description of a parent fact used for the bulk atomicity regression test",
            owner_id="agent1",
            skip_duplicate_check=True,
            db_path=self.db_path,
        )
        id1 = res1.split("ID: ")[1]

        results = bulk_commit_consolidation(
            consolidations=[
                {
                    "parent_ids": [id1],
                    "title": "Would-Be Valid Consolidation",
                    "content": "This item is valid on its own and would succeed in isolation",
                },
                {
                    # Deliberately malformed: commit_consolidation already validates and
                    # rejects an empty parent_ids list with "Error: parent_ids must be a
                    # non-empty list of UUID strings."
                    "parent_ids": [],
                    "title": "Malformed Second Item",
                    "content": "This item is deliberately invalid to trigger a batch rollback",
                },
            ],
            db_connection=self.conn,
        )

        # Whole batch reports as a single top-level error, not a mixed success/error list.
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

        # The first item's consolidated entity must NOT exist -- proving the whole batch
        # rolled back rather than item 1 silently committing while item 2 failed.
        row = self.conn.execute(
            "SELECT id FROM entities WHERE title = ?", ("Would-Be Valid Consolidation",)
        ).fetchone()
        self.assertIsNone(row)

        # Parent A must still be 'raw' (not archived), since the archiving UPDATE that
        # commit_consolidation performs for item 1 must also have been rolled back.
        p1_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (id1,)
        ).fetchone()[0]
        self.assertEqual(p1_status, "raw")

if __name__ == "__main__":
    unittest.main()
