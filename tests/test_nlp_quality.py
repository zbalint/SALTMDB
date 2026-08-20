import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils import nlp, text
from saltmdb.config import QG_OVERSIZED_PAYLOAD_THRESHOLD


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
            content="ok done", title="Short Fluff", owner_id="test_owner"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "SHORT_LENGTH")

    def test_tc_qual_02_fluff_regex_rejection(self):
        """TC-QUAL-02: Conversational fluff response -> REJECT"""
        res = memory_service.store_memory(
            content="modified the file.", title="Conversational Ack", owner_id="test_owner"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(
            {error["code"] for error in res["errors"]},
            {"SHORT_LENGTH", "CONVERSATIONAL_FLUFF"},
        )

    def test_tc_qual_03_shannon_entropy_rejection(self):
        """TC-QUAL-03: Repetitive string (Entropy < 2.5) -> REJECT"""
        repetitive_content = "test test test test test test test test test test test test test test"
        res = memory_service.store_memory(
            content=repetitive_content, title="Repetitive Loop", owner_id="test_owner"
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["errors"][0]["code"], "EXTREME_GENERATION_LOOP")

    def test_tc_qual_04_high_entropy_warning(self):
        """TC-QUAL-04: High entropy minified Base64 / JSON payload -> WARN status"""
        # Base64 string with high character entropy > 5.3 and > 20 chars
        high_entropy_str = "aB3$kL9#mP0!xZ7@qW2&vR8*tY4^uI1%oO5(pA6)sD7_fF8+gG9=hH0-jJ1~kK2"
        res = nlp.evaluate_memory_quality(high_entropy_str)
        self.assertEqual(res["status"], "WARN")
        self.assertIn("HIGH_ENTROPY", res["quality_flags"])

    def test_oversized_payload_warning_uses_named_threshold_constant(self):
        """Cold-start review Issue F: OVERSIZED_PAYLOAD's threshold moved from a bare inline
        literal to a named config.QG_OVERSIZED_PAYLOAD_THRESHOLD constant -- confirm the warning
        still fires at exactly the same length, unchanged behavior, now driven by the constant."""
        # Well-structured content (multiple headings past the 4000-char tier, list items, no
        # repetition/entropy issues) so only OVERSIZED_PAYLOAD is expected to fire, not the
        # unrelated structural hard-fails.
        section = (
            "## Section Heading\n\n"
            "This is a structured paragraph of explanatory prose describing part of a larger "
            "architecture document, written to be well above the entropy floor and below the "
            "duplication ceiling so no unrelated quality flag fires here.\n\n"
            "- A first list item with concrete detail\n"
            "- A second list item with different concrete detail\n\n"
        )
        repeats = QG_OVERSIZED_PAYLOAD_THRESHOLD // len(section) + 2  # margin, then trim exactly
        just_under = "# Doc\n\n" + section * repeats
        just_under = just_under[: QG_OVERSIZED_PAYLOAD_THRESHOLD - 1]
        just_over = just_under + "x" * 2

        self.assertLessEqual(len(just_under), QG_OVERSIZED_PAYLOAD_THRESHOLD)
        res_under = nlp.evaluate_memory_quality(just_under)
        self.assertNotIn("OVERSIZED_PAYLOAD", res_under.get("quality_flags", []))

        self.assertGreater(len(just_over), QG_OVERSIZED_PAYLOAD_THRESHOLD)
        res_over = nlp.evaluate_memory_quality(just_over)
        self.assertIn("OVERSIZED_PAYLOAD", res_over.get("quality_flags", []))

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
            db_connection=self.conn,
        )
        self.assertEqual(first_store["status"], "ok")

        second_store = memory_service.store_memory(
            content=valid_markdown,
            title="Quality Gate Arch Duplicate",
            owner_id="agent_alpha",
            db_connection=self.conn,
        )
        self.assertEqual(second_store["status"], "rejected")
        self.assertEqual(second_store["errors"][0]["code"], "REJECT_EXACT_DUPLICATE")

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
            db_connection=self.conn,
        )
        self.assertEqual(res["status"], "ok")

        # Inspect database fields
        cursor = self.conn.execute(
            "SELECT quality_score, quality_status, quality_flags, content_hash FROM entities WHERE title = ?",
            ("Technical Implementation Plan",),
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        q_score, q_status, q_flags, c_hash = row
        self.assertGreaterEqual(q_score, 0.80)
        self.assertEqual(q_status, "ACCEPT")
        self.assertIn("HAS_HEADERS", q_flags)
        self.assertIn("HAS_LIST", q_flags)
        self.assertEqual(c_hash, text.compute_content_hash(tech_markdown))


if __name__ == "__main__":
    unittest.main()
