import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.mcp import tools
from saltmdb.mcp.identity import SESSION_IDENTITY


class TestSearchMemoryDriftFlag(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path
        SESSION_IDENTITY.reset()
        self._prev_backend = tools._set_backend_for_test(tools.DirectDispatchBackend())

    def tearDown(self):
        tools._set_backend_for_test(self._prev_backend)
        SESSION_IDENTITY.reset()
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_search_memory_surfaces_drift_flag_unconditionally(self):
        # Store an initial memory
        res = tools.store_memory(
            title="Drift Flagged Memory Test",
            content="This memory cites file src/foo.py:42 which drifted.",
            tags=["#test"],
            owner_id="user1",
        )
        self.assertEqual(res["status"], "ok")
        entity_id = res["data"]["id"]

        drift_flag_data = {
            "reason": "File line offset changed",
            "cited_ref": "src/foo.py:42",
            "flagged_at": "2026-08-20T00:00:00Z",
            "flagged_by": "agent_hook_driftsweep",
        }

        # Update metadata via administrative store_memory path
        update_res = tools.store_memory(
            entity_id=entity_id,
            title="Drift Flagged Memory Test",
            content="This memory cites file src/foo.py:42 which drifted.",
            tags=["#test"],
            owner_id="user1",
            metadata={"drift_flag": drift_flag_data},
        )
        self.assertEqual(update_res["status"], "ok")

        # Store another memory with no drift_flag in metadata
        clean_res = tools.store_memory(
            title="Clean Memory Test",
            content="This memory has no drift flag in metadata.",
            tags=["#test"],
            owner_id="user1",
        )
        self.assertEqual(clean_res["status"], "ok")
        clean_id = clean_res["data"]["id"]

        # Search mode="broad" (default)
        search_res = tools.search_memory(
            query_keywords="Memory Test", mode="broad", owner_id="user1"
        )
        results_by_id = {item["id"]: item for item in search_res}

        # (a) memory with drift_flag -> item has drift_flag matching input
        self.assertIn(entity_id, results_by_id)
        self.assertEqual(results_by_id[entity_id]["drift_flag"], drift_flag_data)

        # (b) memory without drift_flag -> item has NO drift_flag key at all
        self.assertIn(clean_id, results_by_id)
        self.assertNotIn("drift_flag", results_by_id[clean_id])


if __name__ == "__main__":
    unittest.main()
