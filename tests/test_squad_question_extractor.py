"""Automated tests for scripts/benchmarking/squad_question_extractor.py -- the safe, line-scan-
only SQuAD `question:` recovery used by the precision-first search evaluation
(`scratch/plans/precision_first_search_evaluation.md`, §0b item 14 / §2c).

Includes both a synthetic single-line fixture and the two REAL corpus files this module's own
docstring cites as its wrapped-line / hex-escape regression cases (per Codex round-3 review:
"I found no actual test referencing squad_question_extractor under tests/... Add that test before
implementation proceeds") -- so this suite doubles as the promised regression test.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "squad_question_extractor.py"
)
_spec = importlib.util.spec_from_file_location("squad_question_extractor", _MODULE_PATH)
sqe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sqe)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_WRAPPED_FILE = _REPO_ROOT / "test_data" / "squad" / "chunk_431" / "doc_431751_part_000.md"
_REAL_HEX_ESCAPE_FILE = _REPO_ROOT / "test_data" / "squad" / "chunk_414" / "doc_414004_part_000.md"


class TestSquadQuestionExtractorSynthetic(unittest.TestCase):
    """Synthetic fixtures -- fast, don't depend on test_data/ (gitignored, not always present)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

    def _write(self, name: str, lines: list) -> Path:
        p = self.tmp_path / name
        p.write_text("\n".join(lines), encoding="utf-8")
        return p

    def test_single_line_plain_scalar_question(self):
        p = self._write(
            "doc1.md",
            [
                "---",
                "source_dataset: squad",
                "question: What color is the sky?",
                "title: Sky",
                "---",
                "",
                "The sky is blue.",
            ],
        )
        self.assertEqual(sqe.extract_question(p), "What color is the sky?")

    def test_wrapped_plain_scalar_question(self):
        p = self._write(
            "doc2.md",
            [
                "---",
                "source_dataset: squad",
                "question: What did the committee decide about the",
                "  proposed budget increase for next year?",
                "title: Budget",
                "---",
                "",
                "Body text.",
            ],
        )
        self.assertEqual(
            sqe.extract_question(p),
            "What did the committee decide about the proposed budget increase for next year?",
        )

    def test_double_quoted_hex_escape_and_continuation_backslash(self):
        # Mirrors the real corpus shape: a double-quoted value wraps with a literal backslash +
        # spaces continuation marker, and a non-ASCII character appears as a \xHH escape.
        p = self._write(
            "doc3.md",
            [
                "---",
                "source_dataset: squad",
                'question: "Who named Beyonc\\xE9 the Artist of the',
                '  \\ \\ Decade?"',
                "title: Beyonce",
                "---",
                "",
                "Body text.",
            ],
        )
        self.assertEqual(sqe.extract_question(p), "Who named Beyoncé the Artist of the Decade?")

    def test_missing_question_key_returns_none(self):
        p = self._write(
            "doc4.md", ["---", "source_dataset: squad", "title: NoQuestion", "---", "", "Body."]
        )
        self.assertIsNone(sqe.extract_question(p))

    def test_malformed_no_frontmatter_returns_none(self):
        p = self._write("doc5.md", ["Just a body, no frontmatter at all."])
        self.assertIsNone(sqe.extract_question(p))

    def test_no_residual_escape_artifacts(self):
        """Regression guard: the cleaned value must never contain a literal backslash or an
        odd number of double-quote characters (both are signs of an incompletely-decoded
        escape/continuation, the exact failure mode round-3 review checked for)."""
        p = self._write(
            "doc6.md",
            [
                "---",
                "source_dataset: squad",
                'question: "Simple quoted question?"',
                "---",
                "",
                "Body.",
            ],
        )
        q = sqe.extract_question(p)
        self.assertNotIn("\\", q)
        self.assertEqual(q.count('"') % 2, 0)


@unittest.skipUnless(
    _REAL_WRAPPED_FILE.exists() and _REAL_HEX_ESCAPE_FILE.exists(),
    "test_data/squad/ is gitignored and not always present -- skip real-corpus cases if absent, "
    "the synthetic fixtures above already cover the same code paths.",
)
class TestSquadQuestionExtractorRealCorpusFiles(unittest.TestCase):
    """The exact two real files this module's docstring and the round-3 Codex review cite by
    name as its wrapped-line and hex-escape regression cases."""

    def test_real_wrapped_plain_scalar_file(self):
        q = sqe.extract_question(_REAL_WRAPPED_FILE)
        self.assertEqual(
            q,
            "Composers and musicians began to construct lives independent of what in the "
            "19th century?",
        )

    def test_real_hex_escaped_continuation_file(self):
        q = sqe.extract_question(_REAL_HEX_ESCAPE_FILE)
        self.assertEqual(
            q,
            "What year was Beyoncé featured both on the Time 100 list as well as the cover of the issue?",
        )
        self.assertNotIn("\\", q)


if __name__ == "__main__":
    unittest.main()
