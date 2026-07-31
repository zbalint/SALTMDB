import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service


class TestCrossOwnerDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "true"

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        if "SALTMDB_ENABLE_SEMANTIC" in os.environ:
            del os.environ["SALTMDB_ENABLE_SEMANTIC"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_shared_scope_memory_visible_across_owners(self):
        """A scope='shared' memory stored by one owner must be surfaced as a dedup candidate for a different owner."""
        memory_service.store_memory(
            content="SALTMDB is a local-first MCP memory database enabling cross-agent shared memory across Claude, Antigravity, and Copilot CLI",
            title="SALTMDB Cross-Agent Design Purpose",
            owner_id="agent_a",
            scope="shared",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        dup_check = memory_service.check_duplicate_memories(
            title="SALTMDB Cross-Agent Purpose Restated",
            content="SALTMDB is a local-first MCP memory database that enables cross-agent shared memory across Claude, Antigravity, and Copilot CLI",
            owner_id="agent_b",
            db_connection=self.conn,
        )

        self.assertTrue(
            dup_check.get("duplicate_found"),
            "Shared-scope memory from a different owner should be detected as a duplicate candidate",
        )
        self.assertEqual(dup_check["potential_duplicates"][0]["scope"], "shared")

    def test_private_scope_memory_stays_isolated_across_owners(self):
        """A scope='private' memory stored by one owner must NOT be surfaced as a dedup candidate for a different owner."""
        memory_service.store_memory(
            content="agent_a private scratch note about a local debugging session that nobody else should see",
            title="agent_a Private Debug Note",
            owner_id="agent_a",
            scope="private",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )

        dup_check = memory_service.check_duplicate_memories(
            title="agent_a Private Debug Note",
            content="agent_a private scratch note about a local debugging session that nobody else should see",
            owner_id="agent_b",
            db_connection=self.conn,
        )

        self.assertFalse(
            dup_check.get("duplicate_found"),
            "Private-scope memory from a different owner must stay isolated",
        )


if __name__ == "__main__":
    unittest.main()
