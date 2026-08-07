import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services import memory_service
from saltmdb.utils import nlp


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
            db_connection=self.conn,
        )
        self.assertIn("Knowledge stored successfully", res)

    def test_tc_adv_03_idempotent_auto_formatting(self):
        """TC-ADV-03: auto_format_markdown auto-annotates untyped code blocks and is idempotent f(f(x)) = f(x)"""
        untyped_md = "# Code Block Test\n\n```\ndef calculate_total(a, b):\n    return a + b\n```"
        formatted_once = nlp.auto_format_markdown(untyped_md)
        self.assertIn("```python", formatted_once)

        formatted_twice = nlp.auto_format_markdown(formatted_once)
        self.assertEqual(formatted_once, formatted_twice)

    def test_tc_adv_04_calibrated_auto_supersession(self):
        """TC-ADV-04 (Track A successor, see scratch/plans/track_a_disposition_detailed.md): a
        near-duplicate write (similarity >= 0.88) no longer auto-persists at all -- it is flagged
        REVIEW_REQUIRED before persistence, never auto-linked/auto-weight-demoted either way (that
        was never this codebase's actual behavior even pre-Track-A -- see the retired
        _handle_supersession_candidate's own docstring: the supersedes-edge decision was always
        left to whoever reviews the flag, not automatic)."""
        original_content = "SALTMDB memory server default port is set to 8080 and database path defaults to saltmdb db system configuration file settings."
        res1 = memory_service.store_memory(
            content=original_content,
            title="SALTMDB Core Architecture Spec",
            owner_id="test_agent",
            weight=5.0,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertIn("Knowledge stored successfully", res1)
        entity_id_1 = res1.split("ID: ")[1].strip()

        # Near duplicate text with 1 word added out of 10 stemmed tokens (Jaccard sim = 9/10 = 0.90 >= 0.88)
        updated_content = "SALTMDB memory server default port is set to 8080 and database path defaults to saltmdb db system configuration file settings extra."
        res2 = memory_service.store_memory(
            content=updated_content,
            title="SALTMDB Core Architecture Spec Revision",
            owner_id="test_agent",
            skip_duplicate_check=False,
            db_connection=self.conn,
        )
        self.assertIsInstance(res2, dict)
        self.assertEqual(res2["status"], "REVIEW_REQUIRED")
        self.assertEqual(res2["candidates"][0]["target_entity_id"], entity_id_1)

        # Original memory's weight is untouched -- no auto-demotion on an unreviewed signal.
        row = self.conn.execute(
            "SELECT weight FROM entities WHERE id = ?", (entity_id_1,)
        ).fetchone()
        self.assertEqual(row[0], 5)

    def test_tc_adv_05_non_technical_prose_not_penalized(self):
        """TC-ADV-05: Long, code-free narrative prose (e.g. a story/roleplay excerpt) is not
        structurally disadvantaged by the quality gate relative to technical content — validates
        the generalization fix that removed code-density/technical-vocabulary scoring bias."""
        narrative = (
            "# The Lighthouse Keeper's Daughter\n\n"
            "Mira had lived beside the sea for as long as she could remember, and every evening "
            "she climbed the spiral stairs to help her father light the lamp before the fog rolled "
            "in from the northern cliffs. Tonight the wind carried a strange, low hum across the "
            "water, unlike anything she had heard before, and the gulls that usually wheeled and "
            "cried above the rocks had gone silent, perched in a long unmoving row along the "
            "weathered fence as if waiting for something to arrive. Her father noticed it too, "
            "pausing with his hand on the brass housing of the lamp, his eyes fixed on a point far "
            "out where the horizon should have been but where instead a deeper darkness seemed to "
            "be gathering itself, patient and enormous, just beneath the surface of the waves.\n\n"
            "She wanted to ask him what it was, but something in his stillness told her the answer "
            "would not be a comfortable one, so instead she simply stood beside him, matching his "
            "silence, and watched the strange dark shape continue to grow at the edge of the world."
        )
        q_res = nlp.evaluate_memory_quality(narrative)
        self.assertEqual(q_res["status"], "ACCEPT")
        self.assertNotIn("LOW_SPECIFICITY", q_res["quality_flags"])
        self.assertNotIn("HAS_CODE", q_res["quality_flags"])
        self.assertGreaterEqual(q_res["quality_score"], 0.5)


if __name__ == "__main__":
    unittest.main()
