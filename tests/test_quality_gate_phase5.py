import unittest

from saltmdb.utils import nlp


class TestPhase5QualityGate(unittest.TestCase):
    def test_hard_errors_are_aggregated(self):
        content = "[placeholder] " + ("x" * 520) + "\n```python\nmissing closing fence"
        result = nlp.evaluate_memory_quality(content)

        self.assertEqual(result["status"], "REJECT")
        self.assertIn("EXPLICIT_PLACEHOLDER", result["hard_errors"])
        self.assertIn("BROKEN_MARKDOWN_SYNTAX", result["hard_errors"])
        self.assertIn("MISSING_PARAGRAPH_BREAK", result["hard_errors"])
        self.assertIn("unresolved placeholder", result["reason"])
        self.assertIn("Unclosed Markdown code block", result["reason"])

    def test_statistical_signals_are_warnings(self):
        block = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
        repeated = block * 3
        result = nlp.evaluate_memory_quality(repeated)

        self.assertEqual(result["status"], "WARN")
        self.assertTrue(
            {"HIGH_3GRAM_REPETITION", "HIGH_5GRAM_REPETITION", "LOW_TTR"} <= set(result["warnings"])
        )
        self.assertEqual(result["hard_errors"], [])

    def test_length_tiers_require_only_the_specified_structure(self):
        short_note = " ".join(f"word{i}" for i in range(70))
        self.assertEqual(nlp.evaluate_memory_quality(short_note)["hard_errors"], [])

        paragraphless = "word " * 130
        self.assertIn(
            "MISSING_PARAGRAPH_BREAK",
            nlp.evaluate_memory_quality(paragraphless)["hard_errors"],
        )

        headed = "# Topic\n\n" + ("word " * 330)
        self.assertNotIn(
            "MISSING_HEADING_OR_LIST",
            nlp.evaluate_memory_quality(headed)["hard_errors"],
        )

        one_heading = "# Topic\n\n" + ("word " * 850)
        self.assertIn(
            "INSUFFICIENT_HEADINGS",
            nlp.evaluate_memory_quality(one_heading)["hard_errors"],
        )

    def test_only_extreme_generation_loop_is_statistical_hard_failure(self):
        result = nlp.evaluate_memory_quality("test " * 40)
        self.assertEqual(result["status"], "REJECT")
        self.assertEqual(result["hard_errors"], ["EXTREME_GENERATION_LOOP"])


if __name__ == "__main__":
    unittest.main()
