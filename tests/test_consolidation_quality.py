import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory
from saltmdb.domain.services.relation_service import commit_consolidation
from saltmdb.utils import text

class TestConsolidationQualityGate(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cons_quality.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tc_cons_01_fluff_rejection(self):
        """TC-CONS-01: Rejection of low-quality fluff content during consolidation"""
        p1 = store_memory(
            title="Raw Fact Alpha",
            content="Detailed raw fact content alpha for consolidation testing",
            owner_id="agent_c",
            db_connection=self.conn
        ).split("ID: ")[1]

        p2 = store_memory(
            title="Raw Fact Beta",
            content="Detailed raw fact content beta for consolidation testing",
            owner_id="agent_c",
            db_connection=self.conn
        ).split("ID: ")[1]

        res = commit_consolidation(
            parent_ids=[p1, p2],
            title="Consolidation Test",
            content="consolidated these files.",
            owner_id="agent_c",
            db_connection=self.conn
        )
        self.assertIn("Error: Consolidation quality check rejected", res)

    def test_tc_cons_02_source_parent_exclusion(self):
        """TC-CONS-02: Source parents excluded during deduplication (no false duplicate rejection against parents)"""
        parent_content = (
            "# System Memory Architecture Specification\n\n"
            "Comprehensive breakdown of memory storage, FTS indexing, and RRF search pipeline.\n"
            "- FTS5 Porter tokenizer\n"
            "- RRF vector dense retrieval"
        )
        p1 = store_memory(
            title="Memory Arch Spec Note",
            content=parent_content,
            owner_id="agent_c",
            db_connection=self.conn,
            skip_duplicate_check=True
        ).split("ID: ")[1]

        # Consolidate with identical content as parent_content
        res = commit_consolidation(
            parent_ids=[p1],
            title="Consolidated Memory Arch Spec",
            content=parent_content,
            owner_id="agent_c",
            db_connection=self.conn
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)

    def test_tc_cons_03_unrelated_exact_collision_rejection(self):
        """TC-CONS-03: Unrelated exact hash duplicate collision rejected during consolidation"""
        existing_markdown = (
            "# Unrelated Independent Module Overview\n\n"
            "This independent module handles background task scheduling and mutex lock acquisition."
        )
        # Existing active memory
        store_memory(
            title="Unrelated Module",
            content=existing_markdown,
            owner_id="agent_c",
            db_connection=self.conn
        )

        p1 = store_memory(
            title="Raw Fact Gamma",
            content="Detailed raw fact content gamma for consolidation testing",
            owner_id="agent_c",
            db_connection=self.conn
        ).split("ID: ")[1]

        # Attempt to consolidate p1 using content that matches the unrelated existing memory
        res = commit_consolidation(
            parent_ids=[p1],
            title="Consolidated Attempt",
            content=existing_markdown,
            owner_id="agent_c",
            db_connection=self.conn
        )
        self.assertIn("REJECT_EXACT_DUPLICATE", res)

    def test_tc_cons_04_successful_consolidation_metadata_storage(self):
        """TC-CONS-04: Successful consolidation saves quality_score, quality_status, quality_flags, and content_hash"""
        p1 = store_memory(
            title="Raw Fact Delta",
            content="Detailed raw fact content delta for consolidation testing",
            owner_id="agent_c",
            db_connection=self.conn
        ).split("ID: ")[1]

        consolidated_md = (
            "# Consolidated System Architecture Overview\n\n"
            "Detailed technical overview combining parent facts.\n\n"
            "## Key Components\n"
            "- SQLite storage engine\n"
            "- ONNX embedding provider (`src/saltmdb/domain/services/embedding_service.py`)\n\n"
            "```python\n"
            "def init_db():\n"
            "    pass\n"
            "```"
        )
        res = commit_consolidation(
            parent_ids=[p1],
            title="Consolidated Overview",
            content=consolidated_md,
            owner_id="agent_c",
            db_connection=self.conn
        )
        self.assertIn("Successfully committed consolidated memory with ID:", res)
        c_id = res.split("ID: ")[1].strip()

        cursor = self.conn.execute(
            "SELECT quality_score, quality_status, quality_flags, content_hash FROM entities WHERE id = ?",
            (c_id,)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        q_score, q_status, q_flags, c_hash = row
        self.assertGreaterEqual(q_score, 0.80)
        self.assertEqual(q_status, "ACCEPT")
        self.assertIn("HAS_HEADERS", q_flags)
        self.assertIn("HAS_CODE", q_flags)
        self.assertEqual(c_hash, text.compute_content_hash(consolidated_md))

if __name__ == "__main__":
    unittest.main()
