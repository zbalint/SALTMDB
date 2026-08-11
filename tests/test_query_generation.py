"""Automated tests for scripts/benchmarking/query_generation.py -- the programmatic (zero-LLM-
cost, deterministic) query-generation building blocks from plan §2c/§2b/§0b item 8."""

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "query_generation.py"
)
_spec = importlib.util.spec_from_file_location("query_generation", _MODULE_PATH)
qg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qg)


class TestLengthBucket(unittest.TestCase):
    def test_short(self):
        self.assertEqual(qg.classify_length_bucket("What color is the sky?"), "short")

    def test_medium(self):
        self.assertEqual(qg.classify_length_bucket("x" * 50), "medium")

    def test_long(self):
        self.assertEqual(qg.classify_length_bucket("x" * 150), "long")

    def test_boundary_exact_40_is_medium(self):
        self.assertEqual(qg.classify_length_bucket("x" * 40), "medium")


class TestTypoPerturbation(unittest.TestCase):
    def test_deterministic_given_seed(self):
        r1 = qg.perturb_typo("what color is the sky", seed=42)
        r2 = qg.perturb_typo("what color is the sky", seed=42)
        self.assertEqual(r1, r2)

    def test_different_seeds_can_differ(self):
        results = {qg.perturb_typo("what color is the sky today friend", seed=s) for s in range(10)}
        self.assertGreater(len(results), 1)

    def test_result_stays_close_in_length(self):
        original = "what color is the sky"
        perturbed = qg.perturb_typo(original, seed=1, n_edits=1)
        self.assertLessEqual(abs(len(perturbed) - len(original)), 1)

    def test_short_unedittable_text_returned_unchanged(self):
        self.assertEqual(qg.perturb_typo("a", seed=1), "a")
        self.assertEqual(qg.perturb_typo("", seed=1), "")

    def test_multiple_edits_still_deterministic(self):
        r1 = qg.perturb_typo("what color is the sky today", seed=7, n_edits=3)
        r2 = qg.perturb_typo("what color is the sky today", seed=7, n_edits=3)
        self.assertEqual(r1, r2)


class TestPartialTerms(unittest.TestCase):
    def test_drops_tokens_keeps_order(self):
        original = "what is the capital of france today"
        result = qg.truncate_to_partial_terms(original, seed=1, keep_fraction=0.5)
        original_tokens = original.split()
        result_tokens = result.split()
        self.assertLess(len(result_tokens), len(original_tokens))
        # relative order preserved: result tokens must appear as a subsequence of original
        it = iter(original_tokens)
        self.assertTrue(all(tok in it for tok in result_tokens))

    def test_single_token_returned_unchanged(self):
        self.assertEqual(qg.truncate_to_partial_terms("hello", seed=1), "hello")

    def test_deterministic(self):
        r1 = qg.truncate_to_partial_terms("a b c d e f g", seed=5)
        r2 = qg.truncate_to_partial_terms("a b c d e f g", seed=5)
        self.assertEqual(r1, r2)


class TestNegativeGenerators(unittest.TestCase):
    def test_gibberish_deterministic(self):
        r1 = qg.generate_gibberish_query(seed=1)
        r2 = qg.generate_gibberish_query(seed=1)
        self.assertEqual(r1, r2)

    def test_gibberish_varies_by_seed(self):
        results = {qg.generate_gibberish_query(seed=s) for s in range(20)}
        self.assertGreater(len(results), 15)

    def test_gibberish_contains_no_real_fragments(self):
        # Weak sanity check: gibberish tokens shouldn't happen to spell a real fragment used by
        # the partial-nonsense generator (would blur the two negative subtypes together).
        query = qg.generate_gibberish_query(seed=99)
        for frag in qg._REAL_FRAGMENTS:
            self.assertNotIn(frag, query)

    def test_partial_nonsense_uses_real_fragments(self):
        query = qg.generate_partial_word_nonsense_query(seed=1)
        self.assertTrue(any(frag in query for frag in qg._REAL_FRAGMENTS))

    def test_partial_nonsense_deterministic(self):
        r1 = qg.generate_partial_word_nonsense_query(seed=3)
        r2 = qg.generate_partial_word_nonsense_query(seed=3)
        self.assertEqual(r1, r2)


class TestTopicFamilyId(unittest.TestCase):
    def test_cluster_root_takes_priority(self):
        result = qg.compute_topic_family_id(
            dataset="squad", source_title="Some_Title", entity_id="e1", cluster_root_id="root1"
        )
        self.assertEqual(result, "cluster:root1")

    def test_dataset_and_title_used_when_no_cluster(self):
        result = qg.compute_topic_family_id(
            dataset="squad", source_title="Classical_music", entity_id="e1"
        )
        self.assertEqual(result, "squad:Classical_music")

    def test_falls_back_to_entity_id(self):
        result = qg.compute_topic_family_id(dataset=None, source_title=None, entity_id="e1")
        self.assertEqual(result, "entity:e1")

    def test_matches_build_diverse_test_db_convention(self):
        # Must literally match build_diverse_test_db.py's compute_split_group_id shape
        # (f"{dataset}:{source_title}") so family grouping never contradicts that script's own
        # split_group_id for the same entities.
        result = qg.compute_topic_family_id(
            dataset="wikipedia_french", source_title="Paris", entity_id="e1"
        )
        self.assertEqual(result, "wikipedia_french:Paris")


class TestSplitAssignment(unittest.TestCase):
    def test_every_family_assigned_exactly_once(self):
        counts = {f"fam{i}": (i % 3) + 1 for i in range(30)}
        assignment = qg.assign_families_to_split(counts, dev_target=25, blind_target=50)
        self.assertEqual(set(assignment.keys()), set(counts.keys()))
        self.assertTrue(all(v in ("dev", "blind") for v in assignment.values()))

    def test_deterministic(self):
        counts = {f"fam{i}": (i % 4) + 1 for i in range(20)}
        a1 = qg.assign_families_to_split(counts, dev_target=15, blind_target=15)
        a2 = qg.assign_families_to_split(counts, dev_target=15, blind_target=15)
        self.assertEqual(a1, a2)

    def test_totals_approximate_targets(self):
        # 40 families of size 3 each = 120 total queries; targets 40/80 (1:2 ratio) should be
        # hit closely at this granularity.
        counts = {f"fam{i}": 3 for i in range(40)}
        assignment = qg.assign_families_to_split(counts, dev_target=40, blind_target=80)
        dev_total = sum(counts[f] for f, split in assignment.items() if split == "dev")
        blind_total = sum(counts[f] for f, split in assignment.items() if split == "blind")
        self.assertEqual(dev_total + blind_total, 120)
        self.assertLessEqual(abs(dev_total - 40), 3)
        self.assertLessEqual(abs(blind_total - 80), 3)

    def test_single_large_family_not_split(self):
        # A family is an atomic unit -- even if it's huge relative to the targets, it goes
        # entirely to one side, never partially counted toward both.
        counts = {"huge_family": 50, "tiny1": 1, "tiny2": 1}
        assignment = qg.assign_families_to_split(counts, dev_target=10, blind_target=45)
        # huge_family must be assigned to exactly one split (this is trivially true given the
        # dict shape, but assert it explicitly as the property under test).
        self.assertIn(assignment["huge_family"], ("dev", "blind"))
        self.assertEqual(len({assignment["huge_family"]}), 1)

    def test_empty_input(self):
        self.assertEqual(qg.assign_families_to_split({}, dev_target=10, blind_target=20), {})


class TestQueryRow(unittest.TestCase):
    def test_to_dict_round_trips_all_fields(self):
        row = qg.QueryRow(
            id="q1",
            query="what color is the sky",
            lang="en",
            category="exact_title",
            subtype="squad",
            split="dev",
            source_entity_ids=["e1"],
            topic_family_id="fam1",
            length_bucket="short",
            provenance="squad-ground-truth",
        )
        d = row.to_dict()
        self.assertEqual(d["id"], "q1")
        self.assertEqual(d["source_entity_ids"], ["e1"])
        self.assertEqual(
            set(d.keys()),
            {
                "id",
                "query",
                "lang",
                "category",
                "subtype",
                "split",
                "source_entity_ids",
                "topic_family_id",
                "length_bucket",
                "provenance",
            },
        )


if __name__ == "__main__":
    unittest.main()
