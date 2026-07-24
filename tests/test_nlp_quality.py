import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils import nlp, text

class TestTextQualityGate(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_quality.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tc_qual_01_short_length_rejection(self):
        """TC-QUAL-01: Short string ('ok done') -> REJECT"""
        res = memory_service.store_memory(
            content="ok done",
            title="Short Fluff",
            owner_id="test_owner"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("below minimum threshold", res)

    def test_tc_qual_02_fluff_regex_rejection(self):
        """TC-QUAL-02: Conversational fluff response -> REJECT"""
        res = memory_service.store_memory(
            content="modified the file.",
            title="Conversational Ack",
            owner_id="test_owner"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("below minimum threshold", res)

    def test_tc_qual_03_shannon_entropy_rejection(self):
        """TC-QUAL-03: Repetitive string (Entropy < 2.5) -> REJECT"""
        repetitive_content = "test test test test test test test test test test test test test test"
        res = memory_service.store_memory(
            content=repetitive_content,
            title="Repetitive Loop",
            owner_id="test_owner"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("entropy too low", res)

    def test_tc_qual_04_high_entropy_warning(self):
        """TC-QUAL-04: High entropy minified Base64 / JSON payload -> WARN status"""
        # Base64 string with high character entropy > 5.3 and > 20 chars
        high_entropy_str = "aB3$kL9#mP0!xZ7@qW2&vR8*tY4^uI1%oO5(pA6)sD7_fF8+gG9=hH0-jJ1~kK2"
        res = nlp.evaluate_memory_quality(high_entropy_str)
        self.assertEqual(res["status"], "WARN")
        self.assertIn("HIGH_ENTROPY", res["quality_flags"])

    def test_tc_qual_05_exact_sha256_hash_collision(self):
        """TC-QUAL-05: Exact match of existing memory -> REJECT_EXACT_DUPLICATE"""
        valid_markdown = (
            "# Architecture Specification\n\n"
            "This document outlines the core architecture of the SALTMDB quality gate subsystem.\n"
            "- Tier 1: Length and fluff filters\n"
            "- Tier 2: Information-theoretic density\n"
            "File path: `src/saltmdb/utils/nlp.py`"
        )
        first_store = memory_service.store_memory(
            content=valid_markdown,
            title="Quality Gate Arch",
            owner_id="agent_alpha",
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", first_store)

        second_store = memory_service.store_memory(
            content=valid_markdown,
            title="Quality Gate Arch Duplicate",
            owner_id="agent_alpha",
            db_connection=self.conn
        )
        self.assertIn("REJECT_EXACT_DUPLICATE", second_store)

    def test_tc_qual_06_technical_markdown_high_quality(self):
        """TC-QUAL-06: Technical Markdown with headers, paths, code fences -> ACCEPT with score >= 0.80"""
        tech_markdown = (
            "# SALTMDB Quality Gate Implementation\n\n"
            "Detailed technical implementation plan for the sub-millisecond quality gate.\n\n"
            "## Subsystem Configuration\n"
            "- Location: `src/saltmdb/domain/services/memory_service.py`\n"
            "- Schema definitions: `src/saltmdb/db/schema.py`\n\n"
            "```python\n"
            "def evaluate_memory_quality(content: str) -> dict:\n"
            "    pass\n"
            "```"
        )
        res = memory_service.store_memory(
            content=tech_markdown,
            title="Technical Implementation Plan",
            owner_id="agent_alpha",
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", res)

        # Inspect database fields
        cursor = self.conn.execute(
            "SELECT quality_score, quality_status, quality_flags, content_hash FROM entities WHERE title = ?",
            ("Technical Implementation Plan",)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        q_score, q_status, q_flags, c_hash = row
        self.assertGreaterEqual(q_score, 0.80)
        self.assertEqual(q_status, "ACCEPT")
        self.assertIn("HAS_HEADERS", q_flags)
        self.assertIn("HAS_CODE", q_flags)
        self.assertIn("HAS_LIST", q_flags)
        self.assertEqual(c_hash, text.compute_content_hash(tech_markdown))

if __name__ == "__main__":
    unittest.main()
