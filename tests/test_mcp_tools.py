import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.mcp import tools

class TestMCPToolsWrapper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_search_memory_alias_resolution(self):
        tools.store_memory(content="Token auth via OAuth2 and JWT", title="Auth Module", owner_id="agent1", skip_duplicate_check=True)
        
        # Test query alias
        res1 = tools.search_memory(query="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res1) > 0)
        self.assertGreater(res1[0]["score"], 0.0)

        # Test q alias
        res2 = tools.search_memory(q="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res2) > 0)
        self.assertGreater(res2[0]["score"], 0.0)

        # Test keywords alias
        res3 = tools.search_memory(keywords="authentication OAuth2", owner_id="agent1")
        self.assertTrue(len(res3) > 0)
        self.assertGreater(res3[0]["score"], 0.0)

    def test_store_memory_alias_resolution(self):
        res = tools.store_memory(text="Some text content", tag="#python", owner="user_test", skip_duplicate_check=True)
        self.assertIn("stored successfully", res)

    def test_log_event_alias_resolution(self):
        res = tools.log_event(agent="test_agent", event_type="decision", description="Decision logged via alias")
        self.assertIn("logged successfully", res)

    def test_get_canonical_tags_alias_resolution(self):
        tools.store_memory(content="Tag test content", title="Tag Test", tags=["#database"], owner_id="user1", skip_duplicate_check=True)
        tags = tools.get_canonical_tags(query="data")
        self.assertIsInstance(tags, list)

    def test_ephemeral_memory_tools(self):
        store_res = tools.store_ephemeral_memory(key="secret_token", value="super_secret_123")
        self.assertIn("stored successfully", store_res)
        
        get_res = tools.get_ephemeral_memory(key="secret_token")
        self.assertEqual(get_res, "super_secret_123")

if __name__ == "__main__":
    unittest.main()
