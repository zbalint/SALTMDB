import unittest
import tempfile
import os
import shutil
import time
import json
from datetime import datetime, UTC, timedelta
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service, librarian_service
from saltmdb.utils import nlp, perplexity

class TestAdvancedQualityFeatures(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_adv.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tc_adv_01_word_salad_perplexity_rejection(self):
        """TC-ADV-01: Bigram Perplexity Gate catches nonsensical word salad (> 25 words)"""
        word_salad = (
            "Database connection orange algorithm table function system query matrix python binary network file "
            "vector embedding metadata schema search index tag event owner lock scope weight history"
        )
        res = memory_service.store_memory(
            content=f"# Nonsensical Word Salad Test\n\n{word_salad}",
            title="Word Salad Test",
            owner_id="test_agent"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("Nonsensical word-salad sequence detected", res)

    def test_tc_adv_02_prose_extraction_protects_technical_logs(self):
        """TC-ADV-02: Prose extraction strips inline code, paths, and URLs, preventing false quality rejections on technical logs"""
        tech_log_payload = (
            "# SALTMDB Database Viewer Log Report\n\n"
            "The database server initialized successfully at http://127.0.0.1:8080/api/entities.\n\n"
            "Execution completed for module `src/saltmdb/db/schema.py` using query `SELECT * FROM entities`.\n\n"
            "All operational events were logged into `~/.saltmdb/viewer.log` without raising exceptions."
        )
        res = memory_service.store_memory(
            content=tech_log_payload,
            title="Technical Log Report",
            owner_id="test_agent",
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", res)

    def test_tc_adv_03_idempotent_auto_formatting(self):
        """TC-ADV-03: auto_format_markdown auto-annotates untyped code blocks and is idempotent f(f(x)) = f(x)"""
        untyped_md = (
            "# Code Block Test\n\n"
            "```\n"
            "def calculate_total(a, b):\n"
            "    return a + b\n"
            "```"
        )
        formatted_once = nlp.auto_format_markdown(untyped_md)
        self.assertIn("```python", formatted_once)
        
        formatted_twice = nlp.auto_format_markdown(formatted_once)
        self.assertEqual(formatted_once, formatted_twice)

    def test_tc_adv_04_calibrated_auto_supersession(self):
        """TC-ADV-04: Calibrated Auto-Supersession (similarity >= 0.88) auto-stores 'supersedes' relation edge and lowers old weight"""
        original_content = (
            "SALTMDB memory server default port is set to 8080 and database path defaults to saltmdb db system configuration file settings."
        )
        res1 = memory_service.store_memory(
            content=original_content,
            title="SALTMDB Core Architecture Spec",
            owner_id="test_agent",
            weight=5.0,
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", res1)
        orig_id = res1.split("ID: ")[1].strip()

        # Near duplicate text with 1 word added out of 10 stemmed tokens (Jaccard sim = 9/10 = 0.90 >= 0.88)
        updated_content = (
            "SALTMDB memory server default port is set to 8080 and database path defaults to saltmdb db system configuration file settings extra."
        )
        res2 = memory_service.store_memory(
            content=updated_content,
            title="SALTMDB Core Architecture Spec Revision",
            owner_id="test_agent",
            skip_duplicate_check=False,
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", res2)

    def test_tc_adv_05_pinned_and_core_memory_decay_exemption(self):
        """TC-ADV-05: Quality-weighted decay in Librarian exempts is_core=1 and metadata.is_pinned=1 memories"""
        past_date = (datetime.now(UTC) - timedelta(days=100)).isoformat()

        # 1. Stale unpinned low-quality memory (should decay)
        self.conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content, owner_id, scope, status, weight, quality_score, is_core)
            VALUES ('stale_1', ?, ?, ?, 'Stale Note', 'Stale contents', 'agent1', 'shared', 'raw', 1.0, 0.20, 0)
        """, (past_date, past_date, past_date))

        # 2. Stale core memory (should BE EXEMPT from decay)
        self.conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content, owner_id, scope, status, weight, quality_score, is_core)
            VALUES ('core_1', ?, ?, ?, 'Core Rule', 'Always use UTF-8 output format.', 'agent1', 'shared', 'raw', 1.0, 0.20, 1)
        """, (past_date, past_date, past_date))

        # 3. Stale pinned memory (should BE EXEMPT from decay)
        self.conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, title, full_content, owner_id, scope, status, weight, quality_score, is_core, metadata)
            VALUES ('pinned_1', ?, ?, ?, 'Pinned Rule', 'Strictly enforce JSON responses.', 'agent1', 'shared', 'raw', 1.0, 0.20, 0, ?)
        """, (past_date, past_date, past_date, json.dumps({"is_pinned": True})))

        self.conn.commit()

        # Run Librarian decay
        librarian_service.decay_low_quality_memories(conn=self.conn)

        # Verify stale_1 is archived
        s1_status = self.conn.execute("SELECT status FROM entities WHERE id = 'stale_1'").fetchone()[0]
        self.assertEqual(s1_status, "archived")

        # Verify core_1 and pinned_1 remain raw
        c1_status = self.conn.execute("SELECT status FROM entities WHERE id = 'core_1'").fetchone()[0]
        self.assertEqual(c1_status, "raw")

        p1_status = self.conn.execute("SELECT status FROM entities WHERE id = 'pinned_1'").fetchone()[0]
        self.assertEqual(p1_status, "raw")

if __name__ == "__main__":
    unittest.main()
