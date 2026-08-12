"""Contract tests for query-blind retrieval-text generation and verification."""

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
MODULE_PATH = ROOT / "scripts" / "benchmarking" / "retrieval_text_generation.py"
SPEC = importlib.util.spec_from_file_location("retrieval_text_generation", MODULE_PATH)
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)


SOURCE_TITLE = "Cache protocol"
SOURCE_BODY = (
    "Decision: use cache_key for MEM-42. The limit is 42 MB. "
    "Docs: https://example.test/cache and /srv/cache.py. "
    "Call `cache_get()`; secret sk_test_" + "a" * 20
)
GOOD = "Use cache_key for MEM-42; the limit is 42 MB. See https://example.test/cache and /srv/cache.py."


class _SequenceGenerator:
    def __init__(self, values):
        self.values = list(values)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.values.pop(0)


class _Verifier:
    def __init__(self, passed=True):
        self.passed = passed
        self.requests = []

    def verify(self, request):
        self.requests.append(request)
        return rt.VerificationResult(self.passed)


class RetrievalTextGenerationTests(unittest.TestCase):
    def test_prompt_is_frozen_and_source_only(self):
        first = rt.build_generation_prompt(SOURCE_TITLE, SOURCE_BODY)
        second = rt.build_generation_prompt(SOURCE_TITLE, SOURCE_BODY)
        self.assertEqual(first, second)
        self.assertIn(SOURCE_TITLE, first)
        self.assertIn("MEM-42", first)
        self.assertNotIn("model:", first.lower().split("source body:", 1)[0])
        self.assertEqual(rt.prompt_hash(SOURCE_TITLE, SOURCE_BODY), rt._sha256(first))
        self.assertNotIn("sk_test_" + "a" * 20, first)

    def test_grounding_checks_concrete_atoms_and_redaction(self):
        source = rt.SourceDocument(SOURCE_TITLE, SOURCE_BODY)
        self.assertEqual(rt.check_grounding(source, GOOD), ())
        issues = rt.check_grounding(
            source,
            "Use cache_key for MEM-99; the limit is 99 MB. See https://evil.test and /tmp/other.py via `other_call()`. "
            "Secret sk_test_" + "b" * 20,
        )
        categories = {issue.category for issue in issues}
        self.assertTrue({"identifier", "number", "url", "path", "code", "redaction"} <= categories)

    def test_success_records_hashes_identity_and_verification(self):
        generator = _SequenceGenerator([GOOD])
        verifier = _Verifier()
        record = rt.generate_retrieval_text(
            SOURCE_TITLE,
            SOURCE_BODY,
            generator,
            verifier,
            generator_model="fixture-generator",
            generator_revision="fixture-r1",
        )
        self.assertEqual(record.verification_state, "verified")
        self.assertEqual(record.verification_attempt_count, 1)
        self.assertFalse(record.coverage_failure)
        self.assertEqual(record.retrieval_text, GOOD)
        self.assertEqual(record.output_hash, rt.output_hash(GOOD))
        self.assertEqual(record.body_hash, rt.source_body_hash(SOURCE_BODY))
        self.assertEqual(record.prompt_hash, rt.prompt_hash(SOURCE_TITLE, SOURCE_BODY))
        self.assertEqual(record.generator_model, "fixture-generator")
        self.assertEqual(record.generator_revision, "fixture-r1")
        self.assertEqual(len(generator.prompts), 1)
        self.assertEqual(len(verifier.requests), 1)
        self.assertEqual(verifier.requests[0].source.body, SOURCE_BODY)

    def test_grounding_failure_gets_one_fixed_retry_then_exclusion(self):
        generator = _SequenceGenerator(
            [GOOD.replace("42 MB", "99 MB"), GOOD.replace("42 MB", "99 MB")]
        )
        verifier = _Verifier()
        record = rt.generate_retrieval_text(
            SOURCE_TITLE,
            SOURCE_BODY,
            generator,
            verifier,
            generator_model="fixture-generator",
            generator_revision="fixture-r1",
        )
        self.assertEqual(len(generator.prompts), rt.MAX_GENERATION_ATTEMPTS)
        self.assertEqual(generator.prompts[0], generator.prompts[1])
        self.assertEqual(len(verifier.requests), 0)
        self.assertEqual(record.verification_state, "excluded")
        self.assertTrue(record.coverage_failure)
        self.assertIsNone(record.retrieval_text)
        self.assertIn(rt.EXCLUSION_COVERAGE_CODE, record.exclusion_reason)

    def test_factual_verifier_is_separate_and_can_consume_retry(self):
        generator = _SequenceGenerator([GOOD, GOOD])
        verifier = _Verifier(passed=False)
        record = rt.generate_retrieval_text(
            SOURCE_TITLE,
            SOURCE_BODY,
            generator,
            verifier,
            generator_model="fixture-generator",
            generator_revision="fixture-r1",
        )
        self.assertEqual(record.verification_attempt_count, 2)
        self.assertEqual(len(verifier.requests), 2)
        self.assertTrue(record.coverage_failure)
        self.assertIsNone(record.retrieval_text)

    def test_source_change_invalidates_without_fallback(self):
        record = rt.generate_retrieval_text(
            SOURCE_TITLE,
            SOURCE_BODY,
            _SequenceGenerator([GOOD]),
            _Verifier(),
            generator_model="fixture-generator",
            generator_revision="fixture-r1",
        )
        changed = SOURCE_BODY + " New constraint."
        self.assertFalse(rt.record_is_current(record, SOURCE_TITLE, changed))
        invalidated = rt.invalidate_for_source_change(record, SOURCE_TITLE, changed)
        self.assertEqual(invalidated.verification_state, "invalidated")
        self.assertTrue(invalidated.coverage_failure)
        self.assertIsNone(invalidated.retrieval_text)
        with self.assertRaises(rt.StaleRetrievalTextRecord):
            rt.ensure_record_current(record, SOURCE_TITLE, changed)

    def test_record_round_trip_and_mutable_revision_rejection(self):
        record = rt.generate_retrieval_text(
            SOURCE_TITLE,
            SOURCE_BODY,
            _SequenceGenerator([GOOD]),
            _Verifier(),
            generator_model="fixture-generator",
            generator_revision="fixture-r1",
        )
        self.assertEqual(rt.RetrievalTextRecord.from_dict(record.to_dict()), record)
        with self.assertRaises(ValueError):
            rt.GeneratorIdentity("fixture-generator", "latest")


if __name__ == "__main__":
    unittest.main()
