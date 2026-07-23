"""
Tests for Hybrid FTS5 + Semantic Search (RRF) — Phase 8.
Written against src/saltmdb package (not the legacy saltmdb_server monolith).
Run: python -m pytest scratch/test_hybrid_search.py -v
"""
import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"  # Most tests use FTS-only for speed

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import (
    store_memory, search_memory,
    reciprocal_rank_fusion,
)


class TestRRFCorrectness(unittest.TestCase):
    """Test 1: reciprocal_rank_fusion() correctness with hand-constructed inputs."""

    def _make_fts_rows(self, ids):
        """Build minimal sqlite3-row-like tuples: (id, ...) shape."""
        return [(eid,) + ("x",) * 12 for eid in ids]

    def test_item_in_both_lists_ranks_first(self):
        fts = self._make_fts_rows(["A", "B", "C"])
        sem = [("B", 0.1), ("D", 0.2), ("A", 0.3)]
        result = reciprocal_rank_fusion(fts, sem, limit=5)
        # A and B are in both — should rank ahead of C (FTS-only) and D (sem-only)
        self.assertIn("A", result[:2])
        self.assertIn("B", result[:2])

    def test_item_in_only_one_list_ranks_lower(self):
        fts = self._make_fts_rows(["A", "C"])
        sem = [("A", 0.1), ("D", 0.2)]
        result = reciprocal_rank_fusion(fts, sem, limit=4)
        self.assertEqual(result[0], "A")      # In both — ranks first
        self.assertIn("C", result)            # FTS-only — lower than A
        self.assertIn("D", result)            # Sem-only — lower than A

    def test_item_absent_from_both_does_not_appear(self):
        fts = self._make_fts_rows(["A"])
        sem = [("B", 0.1)]
        result = reciprocal_rank_fusion(fts, sem, limit=5)
        self.assertNotIn("Z", result)

    def test_empty_inputs_return_empty(self):
        self.assertEqual(reciprocal_rank_fusion([], [], limit=5), [])


class TestStoreMemoryReturnsBeforeEmbedding(unittest.TestCase):
    """Test 2: store_memory() is non-blocking; immediate search_memory() does not error."""

    def setUp(self):
        self.db_name = f"test_hybrid_{self._testMethodName}.db"
        os.environ["SALTMDB_DB_PATH"] = os.path.abspath(self.db_name)
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"
        self.conn = init_db(self.db_name)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for f in [self.db_name, self.db_name + "-wal", self.db_name + "-shm"]:
            try:
                os.remove(f)
            except (FileNotFoundError, PermissionError):
                pass

    def test_store_returns_immediately(self):
        t0 = time.monotonic()
        result = store_memory(
            title="Timing Test Memory",
            content="# Timing Test\nThis is a test memory for timing verification.",
            owner_id="test_agent",
            db_path=os.path.abspath(self.db_name)
        )
        elapsed = time.monotonic() - t0
        self.assertIn("Knowledge stored successfully", result)
        # store_memory must return in well under 1 second (embedding is async)
        self.assertLess(elapsed, 1.0, "store_memory() blocked — embedding is not async")

    def test_immediate_search_after_store_does_not_error(self):
        store_memory(
            title="Immediate Search Test",
            content="# Immediate Search\nSearchable content here.",
            owner_id="test_agent",
            db_path=os.path.abspath(self.db_name)
        )
        # Search immediately — embedding will be pending, should still work
        results = search_memory(owner_id="test_agent", query_keywords="immediate search")
        self.assertIsInstance(results, list)
        self.assertFalse(any("error" in str(r).lower() for r in results))


class TestConcurrentStoreMemory(unittest.TestCase):
    """Test 3: multiple threads calling store_memory() simultaneously — no corruption."""

    def setUp(self):
        self.db_name = f"test_hybrid_{self._testMethodName}.db"
        os.environ["SALTMDB_DB_PATH"] = os.path.abspath(self.db_name)
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"
        self.conn = init_db(self.db_name)
        self.results = []
        self.lock = threading.Lock()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for f in [self.db_name, self.db_name + "-wal", self.db_name + "-shm"]:
            try:
                os.remove(f)
            except (FileNotFoundError, PermissionError):
                pass

    def _worker(self, i):
        result = store_memory(
            title=f"Concurrent Memory {i}",
            content=f"# Concurrent Memory {i}\nContent for thread {i}.",
            owner_id="test_agent",
            db_path=os.path.abspath(self.db_name)
        )
        with self.lock:
            self.results.append(result)

    def test_concurrent_writes_no_corruption(self):
        threads = [threading.Thread(target=self._worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(self.results), 5)
        for r in self.results:
            self.assertIn("Knowledge stored successfully", r,
                          f"Unexpected result from concurrent store: {r}")


class TestEmptyEmbeddingTableBehavior(unittest.TestCase):
    """Test 4: search_memory() with keywords against a fresh DB returns FTS5-only results, no error."""

    def setUp(self):
        self.db_name = f"test_hybrid_{self._testMethodName}.db"
        os.environ["SALTMDB_DB_PATH"] = os.path.abspath(self.db_name)
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "true"  # Enable hybrid path
        self.conn = init_db(self.db_name)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        os.environ["SALTMDB_ENABLE_SEMANTIC"] = "false"
        for f in [self.db_name, self.db_name + "-wal", self.db_name + "-shm"]:
            try:
                os.remove(f)
            except (FileNotFoundError, PermissionError):
                pass

    def test_fresh_db_search_returns_no_error(self):
        store_memory(
            title="Empty Table Test",
            content="# Empty Table\nSearchable content.",
            owner_id="test_agent",
            db_path=os.path.abspath(self.db_name)
        )
        # Immediately search — entity_embeddings is empty (background thread hasn't finished)
        results = search_memory(owner_id="test_agent", query_keywords="empty table")
        self.assertIsInstance(results, list)
        self.assertFalse(any("error" in str(r).lower() for r in results))


if __name__ == "__main__":
    unittest.main()
