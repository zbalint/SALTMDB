import unittest
import tempfile
import os
import time
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory
from saltmdb.domain.services.relation_service import store_relation

class TestE2EHybridSearch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_full_hybrid_search_workflow(self):
        # Store two distinct memories
        res1 = store_memory(
            title="Shor Factorization Algorithm",
            content="Shor algorithm factorizes integers in polynomial time using QFT",
            tags=["#quantum", "#crypto"],
            owner_id="user1",
            context_id="ctx_quantum",
            skip_duplicate_check=True,
            db_path=self.db_path
        )
        id1 = res1.split("ID: ")[1]

        res2 = store_memory(
            title="Grover Database Search",
            content="Grover search algorithm provides quadratic speedup for unorganized datasets",
            tags=["#quantum", "#search"],
            owner_id="user1",
            context_id="ctx_quantum",
            skip_duplicate_check=True,
            db_path=self.db_path
        )
        id2 = res2.split("ID: ")[1]

        # Link id1 -> id2
        store_relation(source_id=id1, target_id=id2, predicate="complements", db_connection=self.conn)

        # Wait for background embedding generation
        time.sleep(0.5)

        # Perform hybrid search with include_related=True
        results = search_memory(
            query_keywords="Shor integer factorization QFT",
            owner_id="user1",
            context_id="ctx_quantum",
            include_related=True,
            db_path=self.db_path
        )

        self.assertTrue(len(results) > 0)
        top = results[0]
        self.assertEqual(top["id"], id1)
        self.assertGreater(top["score"], 0.0)
        self.assertIn("related_entities", top)

    def test_explain_mode(self):
        store_memory(title="Explain Test", content="Content for explain mode test", owner_id="user1", skip_duplicate_check=True, db_path=self.db_path)
        explain_res = search_memory(query_keywords="content explain", explain_mode=True, db_path=self.db_path)
        self.assertIn("explain", explain_res)
        self.assertIn("searched_terms_found", explain_res["explain"])

if __name__ == "__main__":
    unittest.main()
