"""Contract tests for split isolation and the frozen-shortlist blind gate."""

import importlib.util
import unittest
from pathlib import Path


_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmarking"
    / "analyze_evaluation_matrix.py"
)
_SPEC = importlib.util.spec_from_file_location("analyze_evaluation_matrix", _PATH)
am = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(am)


def _fixture(split="dev"):
    configs = [item["name"] for item in am._build_evaluation_configs()]
    query = {
        "id": "q1",
        "query": "cache protocol",
        "split": split,
        "category": "exact_title",
        "topic_family_id": "family:1",
        "source_entity_ids": ["c1"],
    }
    matrix = {
        "errors": [],
        "config_rankings": {"q1": {name: ["c1"] for name in configs}},
        "pools": {"q1": {"c1": {"title": "Cache", "snippet": "protocol"}}},
    }
    labels = [{"query_id": "q1", "candidate_id": "c1", "median_grade": 2, "final_grade": 2}]
    return [query], matrix, labels


class TestAnalyzeEvaluationMatrix(unittest.TestCase):
    def test_freeze_orders_by_ndcg_then_mrr_then_name(self):
        names = [item["name"] for item in am._build_evaluation_configs()]
        metrics = {
            name: {
                "ndcg_at_10": {"value": 0.5},
                "mrr": {"value": 0.5},
                "top1_direct_relevance": {"value": 0.0},
            }
            for name in names
        }
        non_default = [name for name in names if name != am.CURRENT_DEFAULT_CONFIG_NAME]
        metrics[non_default[0]]["ndcg_at_10"]["value"] = 0.9
        metrics[non_default[1]]["ndcg_at_10"]["value"] = 0.9
        metrics[non_default[1]]["mrr"]["value"] = 0.8
        metrics[non_default[2]]["ndcg_at_10"]["value"] = 0.8
        analysis = {"split": "dev", "metrics": metrics, "input_hashes": {}}
        shortlist = am.freeze_dev_contenders(analysis)
        self.assertEqual(shortlist["contenders"][:2], [non_default[1], non_default[0]])
        am.validate_frozen_shortlist(shortlist)

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
        shortlist = am.freeze_dev_contenders(
            am.analyze(dev_queries, dev_matrix, dev_labels, n_resamples=10)
        )
        comparisons = am.paired_comparisons(analysis, shortlist["comparisons"], n_resamples=10)
        self.assertEqual(len(comparisons), 4)
        self.assertEqual(
            {(item["contender"], item["baseline"]) for item in comparisons},
            {tuple(pair) for pair in shortlist["comparisons"]},
        )

    def test_rejects_sharded_or_tampered_label_artifact(self):
        queries, matrix, labels = _fixture()
        with self.assertRaises(ValueError):
            am.analyze(queries, matrix, {"shards": [{"labels": labels}]}, n_resamples=10)
        artifact = {"labels": labels, "schema_version": 1, "fingerprint": "bad"}
        with self.assertRaises(ValueError):
            am.analyze(queries, matrix, artifact, n_resamples=10)

    def test_rejects_tampered_matrix_fingerprint(self):
        queries, matrix, labels = _fixture()
        matrix["fingerprint"] = "bad"
        with self.assertRaises(ValueError):
            am.analyze(queries, matrix, labels, n_resamples=10)

    def test_blind_decision_uses_ndcg_4pp_and_value_slices(self):
        names = [item["name"] for item in am._build_evaluation_configs()]
        contenders = [name for name in names if name != am.CURRENT_DEFAULT_CONFIG_NAME][:3]
        shortlist = {
            "schema_version": 1,
            "current_default": am.CURRENT_DEFAULT_CONFIG_NAME,
            "contenders": contenders,
            "dev_ranking": contenders,
            "comparisons": [
                [contenders[0], am.CURRENT_DEFAULT_CONFIG_NAME],
                [contenders[1], am.CURRENT_DEFAULT_CONFIG_NAME],
                [contenders[2], am.CURRENT_DEFAULT_CONFIG_NAME],
                [contenders[0], contenders[1]],
            ],
            "development_input_hashes": {},
        }
        shortlist["fingerprint"] = am._hash(shortlist)
        metrics = {
            name: {
                "mrr": {"value": 0.4},
                "known_answer_recall_at_10": {"value": 0.4},
                "misleading_top1": {"value": 0.2},
            }
            for name in [am.CURRENT_DEFAULT_CONFIG_NAME, *contenders]
        }
        comparisons = []
        for index, contender in enumerate(contenders):
            comparisons.append(
                {
                    "contender": contender,
                    "baseline": am.CURRENT_DEFAULT_CONFIG_NAME,
                    "holm_adjusted_p": 0.01 if index == 0 else 0.5,
                    "ndcg_delta": 0.05 if index == 0 else 0.0,
                    "ndcg_delta_ci95": [0.01, 0.08] if index == 0 else [-0.02, 0.02],
                }
            )
        comparisons.append(
            {
                "contender": contenders[0],
                "baseline": contenders[1],
                "holm_adjusted_p": 0.5,
                "ndcg_delta": 0.0,
                "ndcg_delta_ci95": [-0.01, 0.01],
            }
        )
        vectors = {
            category: {
                name: [True, True, True, True]
                for name in [am.CURRENT_DEFAULT_CONFIG_NAME, *contenders]
            }
            for category in am.HIGH_VALUE_CATEGORIES
        }
        analysis = {"split": "blind", "metrics": metrics, "slice_vectors": vectors}
        decision = am.blind_decision(analysis, comparisons, shortlist)
        self.assertEqual(decision["outcome"], "select_contender")
        self.assertEqual(decision["winner"], contenders[0])
        self.assertEqual(decision["candidate_evidence"][contenders[0]]["status"], "WIN")


if __name__ == "__main__":
    unittest.main()
