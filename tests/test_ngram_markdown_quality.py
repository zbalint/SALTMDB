import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils import nlp

class TestNGramAndMarkdownQuality(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ngram.db")
        self.conn = init_db(self.db_path)
        os.environ["SALTMDB_DB_PATH"] = self.db_path

    def tearDown(self):
        self.conn.close()
        if "SALTMDB_DB_PATH" in os.environ:
            del os.environ["SALTMDB_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tc_ngram_01_phrase_loop_rejection(self):
        """TC-NGRAM-01: Catch phrase sequence loops (3-gram repetition > 0.30) that pass character entropy"""
        phrase_block = "apple banana cherry date elderberry fig grape honeydew kiwi lemon mango nectarine orange papaya quince raspberry strawberry tangerine ucli vanilla watermelon xigua yuzu"
        # Repeating the phrase block 3 times maintains character entropy ~4.35 bits/char but creates ~66% 3-gram repetition
        repetitive_text = f"# Sequence Loop Test\n\n{phrase_block} {phrase_block} {phrase_block}"
        res = memory_service.store_memory(
            content=repetitive_text,
            title="Sequence Loop Test",
            owner_id="test_agent"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("3-gram sequence repetition detected", res)

    def test_tc_ngram_02_low_ttr_rejection(self):
        """TC-NGRAM-02: Rejection of low TTR (< 0.35) for boilerplate text"""
        # Create a phrase using alternating pairs of words so 3-grams remain unique but overall vocabulary is small
        words_list = ["alpha", "beta", "alpha", "gamma", "beta", "gamma", "delta", "alpha"] * 5
        low_ttr_text = " ".join(words_list)
        res = memory_service.store_memory(
            content=f"# Low TTR Test\n\n{low_ttr_text}",
            title="Low TTR Test",
            owner_id="test_agent"
        )
        self.assertIn("Error: Memory quality check rejected", res)

    def test_tc_markdown_01_unclosed_code_fence_rejection(self):
        """TC-MARKDOWN-01: Rejection of unclosed triple backtick code fences"""
        unclosed_markdown = (
            "# Markdown Syntax Test\n\n"
            "This markdown contains an unclosed code block fence:\n\n"
            "```python\n"
            "def foo():\n"
            "    print('Hello World')\n"
            "# Missing closing fence"
        )
        res = memory_service.store_memory(
            content=unclosed_markdown,
            title="Unclosed Fence Test",
            owner_id="test_agent"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("Unclosed Markdown code block detected", res)

    def test_tc_markdown_02_malformed_table_rejection(self):
        """TC-MARKDOWN-02: Rejection of malformed Markdown table rows"""
        malformed_table = (
            "# Broken Table Test\n\n"
            "| Header 1 | Header 2 |\n"
            "| Row 1 Col 1 |\n" # Insufficient pipe separators
        )
        res = memory_service.store_memory(
            content=malformed_table,
            title="Broken Table Test",
            owner_id="test_agent"
        )
        self.assertIn("Error: Memory quality check rejected", res)
        self.assertIn("Malformed Markdown table row detected", res)

    def test_tc_markdown_03_high_msdi_score_boost(self):
        """TC-MARKDOWN-03: High MSDI (>= 0.35) and annotated code blocks receive score boost and HIGH_MSDI flag"""
        structured_md = (
            "# SALTMDB Architecture Specification\n\n"
            "Comprehensive breakdown of memory storage and vector search.\n\n"
            "## Core Components\n"
            "- SQLite FTS5 database\n"
            "- ONNX vector embedding engine\n"
            "- Mechanical Text Quality Gate\n\n"
            "```python\n"
            "def evaluate_memory_quality(content: str) -> dict:\n"
            "    return {'status': 'ACCEPT'}\n"
            "```"
        )
        res = memory_service.store_memory(
            content=structured_md,
            title="High MSDI Test",
            owner_id="test_agent",
            db_connection=self.conn
        )
        self.assertIn("Knowledge stored successfully", res)

        cursor = self.conn.execute(
            "SELECT quality_score, quality_status, quality_flags FROM entities WHERE title = ?",
            ("High MSDI Test",)
        )
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        q_score, q_status, q_flags = row
        self.assertGreaterEqual(q_score, 0.85)
        self.assertEqual(q_status, "ACCEPT")
        self.assertIn("HIGH_MSDI", q_flags)

if __name__ == "__main__":
    unittest.main()
