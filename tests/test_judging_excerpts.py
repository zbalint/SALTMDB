"""Contract tests for deterministic public judging excerpts."""

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
MODULE_PATH = ROOT / "scripts" / "benchmarking" / "judging_excerpts.py"
SPEC = importlib.util.spec_from_file_location("judging_excerpts", MODULE_PATH)
ex = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ex)


class JudgingExcerptTests(unittest.TestCase):
    def test_query_centered_selection_is_deterministic_and_bounded(self):
        body = "Introductory context. Cache eviction uses a bounded LRU policy. Unrelated tail."
        first = ex.build_query_centered_excerpt(
            "Cache decision", body, "LRU eviction", max_chars=64
        )
        second = ex.build_query_centered_excerpt(
            "Cache decision", body, "LRU eviction", max_chars=64
        )
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.rendered), 64)
        self.assertIn("Cache decision", first.rendered)
        self.assertIn("LRU", first.rendered)
        self.assertEqual(first.algorithm_version, ex.EXCERPT_ALGORITHM_VERSION)
        self.assertEqual(first.excerpt_hash, ex._sha256(first.rendered))

    def test_redaction_and_source_hashes_are_recorded(self):
        secret = "sk_test_" + "a" * 20
        excerpt = ex.build_query_centered_excerpt(
            "Secrets", f"Keep credentials private: {secret}", "credentials"
        )
        self.assertTrue(excerpt.redaction_applied)
        self.assertNotIn(secret, excerpt.rendered)
        self.assertIn("[REDACTED_SECRET]", excerpt.rendered)
        changed = ex.build_query_centered_excerpt(
            "Secrets", f"Keep credentials private: {secret}!", "credentials"
        )
        self.assertNotEqual(excerpt.source_hash, changed.source_hash)

    def test_public_packet_has_no_private_evaluation_fields(self):
        public = ex.build_query_centered_excerpt(
            "Title", "A source body.", "source"
        ).to_public_dict()
        self.assertEqual(
            set(public),
            {
                "schema_version",
                "algorithm_version",
                "title",
                "excerpt",
                "source_hash",
                "excerpt_hash",
                "redaction_applied",
            },
        )
        self.assertFalse({"rank", "config", "model", "expected_target", "target"} & set(public))
        self.assertEqual(ex.JudgingExcerpt.from_public_dict(public).to_public_dict(), public)


if __name__ == "__main__":
    unittest.main()
