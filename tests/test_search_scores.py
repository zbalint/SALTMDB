import unittest
import tempfile
import os
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory, reciprocal_rank_fusion

class TestSearchScores(unittest.TestCase):
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

    def test_fts_search_score_non_zero(self):
        store_memory(title="Authentication Module", content="Handles OAuth2 and JWT token authentication", owner_id="user1", skip_duplicate_check=True, db_path=self.db_path)
        store_memory(title="Database Backup Service", content="Performs hourly PostgreSQL and SQLite snapshot backups", owner_id="user1", skip_duplicate_check=True, db_path=self.db_path)
        
        results = search_memory(query_keywords="authentication OAuth2", owner_id="user1", db_path=self.db_path)
        self.assertTrue(len(results) > 0)
        self.assertGreater(results[0]["score"], 0.0)

    def test_reciprocal_rank_fusion_returns_scores(self):
        fts = [("id1", "title1")]
        semantic = [("id1", 0.1), ("id2", 0.2)]
        fused = reciprocal_rank_fusion(fts, semantic, limit=5)
        self.assertIn("id1", fused)
        self.assertGreater(fused["id1"], 0.0)

if __name__ == "__main__":
    unittest.main()
