"""Contract tests for split isolation and the frozen-shortlist blind gate."""
import importlib.util
import unittest
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "analyze_evaluation_matrix.py"
_SPEC = importlib.util.spec_from_file_location("analyze_evaluation_matrix", _PATH)
am = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(am)


def _fixture(split="dev"):
    configs = [item["name"] for item in am._build_evaluation_configs()]
    query = {"id": "q1", "query": "cache protocol", "split": split, "category": "exact_title",
             "topic_family_id": "family:1", "source_entity_ids": ["c1"]}
    matrix = {"errors": [], "config_rankings": {"q1": {name: ["c1"] for name in configs}},
              "pools": {"q1": {"c1": {"title": "Cache", "snippet": "protocol"}}}}
    labels = [{"query_id": "q1", "candidate_id": "c1", "median_grade": 2, "final_grade": 2}]
    return [query], matrix, labels


class TestAnalyzeEvaluationMatrix(unittest.TestCase):
    def test_rejects_query_from_other_split(self):
        queries, matrix, labels = _fixture("blind")
        with self.assertRaises(ValueError):
            am.analyze(queries, matrix, labels, n_resamples=10, split="dev")

    def test_rejects_incomplete_labels_for_pool(self):
        queries, matrix, labels = _fixture()
        matrix["pools"]["q1"]["extra"] = {"title": "Extra", "snippet": "x"}
        with self.assertRaises(ValueError):
            am.analyze(queries, matrix, labels, n_resamples=10)

    def test_freeze_shortlist_is_tamper_evident(self):
        queries, matrix, labels = _fixture()
        shortlist = am.freeze_dev_contenders(am.analyze(queries, matrix, labels, n_resamples=10))
        am.validate_frozen_shortlist(shortlist)
        shortlist["contenders"][0] = "tampered"
        with self.assertRaises(ValueError):
            am.validate_frozen_shortlist(shortlist)

    def test_blind_comparisons_use_only_frozen_pairs(self):
        queries, matrix, labels = _fixture("blind")
        analysis = am.analyze(queries, matrix, labels, n_resamples=10, split="blind")
        dev_queries, dev_matrix, dev_labels = _fixture("dev")
        shortlist = am.freeze_dev_contenders(am.analyze(dev_queries, dev_matrix, dev_labels, n_resamples=10))
        comparisons = am.paired_comparisons(analysis, shortlist["comparisons"], n_resamples=10)
        self.assertEqual(len(comparisons), 4)
        self.assertEqual({(item["contender"], item["baseline"]) for item in comparisons},
                         {tuple(pair) for pair in shortlist["comparisons"]})


if __name__ == "__main__":
    unittest.main()
