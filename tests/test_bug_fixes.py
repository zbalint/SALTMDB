import unittest
import os
import tempfile
import sqlite3

from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.nlp import (
    evaluate_memory_quality,
    calculate_coleman_liau_index,
    validate_markdown_structure,
)
from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory


class TestBugFixes(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.db_path = self.temp_db.name
        self.temp_db.close()

        init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_p0_redaction_secret_bypass(self):
        """P0-1 Fix Verification: Ensure Discord tokens and generic API secrets without sk-/ghp_ prefixes get redacted."""
        discord_token = "MTA1OTM4MjkwNzYxNDMwOTI0OA.G8Xy9Z.aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789a"
        generic_secret = (
            "client_id_1234567890123:client_secret_abcdefghijklmnopqrstuvwxyz0123456789"
        )

        redacted_discord = redact_secrets(f"My token is {discord_token}")
        self.assertNotIn(discord_token, redacted_discord)
        self.assertIn("[REDACTED_SECRET]", redacted_discord)

        redacted_generic = redact_secrets(f"Credentials: {generic_secret}")
        self.assertNotIn(generic_secret, redacted_generic)
        self.assertIn("[REDACTED_SECRET]", redacted_generic)

    def test_p1_non_english_and_bullet_quality_gate(self):
        """P1-1 & P1-2 Fix Verification: Ensure non-English and unpunctuated bullet list documentation are ACCEPTED."""
        # Non-English Hungarian text
        hungarian_text = "# Adatbázis Konfiguráció\n\nEz egy teljesen valid magyar nyelvű dokumentáció az adatbázis beállításokról és a kapcsolatok kezeléséről."
        q_hu = evaluate_memory_quality(hungarian_text)
        self.assertEqual(q_hu["status"], "ACCEPT")

        # Unpunctuated bulleted technical documentation
        bullet_doc = "# Technical Features\n\n- Multi-threaded TCP web server execution\n- Hybrid vector search with Reciprocal Rank Fusion\n- SQLite FTS5 full text indexing engine\n- Idempotent Markdown formatting pipeline\n- Calibrated graph auto-supersession logic"
        cli = calculate_coleman_liau_index(bullet_doc)
        self.assertLess(cli, 26.0)
        q_bullet = evaluate_memory_quality(bullet_doc)
        self.assertEqual(q_bullet["status"], "ACCEPT")

    def test_p1_markdown_structure_heuristics(self):
        """P1-3 Fix Verification: Ensure indented code fences pass structure check."""
        indented_md = "Here is an indented block:\n  ```docker-compose\n  version: '3.8'\n  ```"
        struct_res = validate_markdown_structure(indented_md)
        self.assertTrue(struct_res["is_valid"])

    def test_p2_search_tag_operator_and_vs_or(self):
        """P2-1 Fix Verification: Ensure tag_operator='AND' strictly matches entities possessing ALL specified tags."""
        store_memory(
            "Memory A content text for testing multi-tag matching.",
            tags=["#tagAlpha", "#tagBeta"],
            owner_id="agent1",
            scope="shared",
            title="Memory A",
            db_path=self.db_path,
        )
        store_memory(
            "Memory B content text for testing single-tag matching.",
            tags=["#tagAlpha"],
            owner_id="agent1",
            scope="shared",
            title="Memory B",
            db_path=self.db_path,
            skip_duplicate_check=True,
        )

        res_or = search_memory(
            tags_filter=["#tagAlpha", "#tagBeta"],
            tag_operator="OR",
            owner_id="agent1",
            db_path=self.db_path,
        )
        self.assertEqual(len(res_or), 2)

        res_and = search_memory(
            tags_filter=["#tagAlpha", "#tagBeta"],
            tag_operator="AND",
            owner_id="agent1",
            db_path=self.db_path,
        )
        self.assertEqual(len(res_and), 1)
        self.assertEqual(res_and[0]["title"], "Memory A")

    def test_p2_exact_hash_shared_deduplication(self):
        """P2-2 Fix Verification: Ensure shared memory exact hash deduplication catches cross-agent duplicates."""
        shared_text = "# Global Architecture Policy\n\nAll services must communicate exclusively via gRPC interfaces."
        res1 = store_memory(
            shared_text, owner_id="AgentA", scope="shared", title="Policy", db_path=self.db_path
        )
        self.assertIn("Knowledge stored successfully", res1)

        res2 = store_memory(
            shared_text,
            owner_id="AgentB",
            scope="shared",
            title="Policy Copy",
            db_path=self.db_path,
        )
        self.assertIn("REJECT_EXACT_DUPLICATE", res2)

    def test_is_core_flag_persistence(self):
        """Fix Verification: Ensure is_core=True sets is_core=1, and updating without specifying is_core preserves is_core=1."""
        mem_text = (
            "# Core System Guideline\n\nMust enforce strict circuit breaker on tool failures."
        )
        res = store_memory(
            mem_text,
            owner_id="AgentA",
            scope="shared",
            is_core=True,
            title="Core Guideline",
            db_path=self.db_path,
            core_reason="Test fixture core reason for the is_core flag persistence regression test.",
            core_exit_condition="Test fixture exit condition: this regression test tears down its temp DB.",
        )
        self.assertIn("Knowledge stored successfully", res)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id, is_core FROM entities WHERE title = 'Core Guideline'"
        ).fetchone()
        self.assertIsNotNone(row)
        entity_id, is_core = row
        self.assertEqual(is_core, 1)

        # Administrative update keeps frozen content byte-identical while omitting is_core.
        updated_text = mem_text
        res_update = store_memory(
            updated_text,
            entity_id=entity_id,
            owner_id="AgentA",
            scope="shared",
            title="Core Guideline",
            db_path=self.db_path,
            skip_duplicate_check=True,
        )
        self.assertIn("Knowledge stored successfully", res_update)

        row_updated = conn.execute(
            "SELECT is_core FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row_updated[0], 1)


if __name__ == "__main__":
    unittest.main()
