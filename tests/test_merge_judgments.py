"""Automated tests for scripts/benchmarking/merge_judgments.py -- median-of-3 aggregation,
disagreement escalation, arbitration override, per-grade confusion/kappa, and SQuAD calibration
accuracy, per plan §4/§0b items 11-12/17.
"""

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "merge_judgments.py"
)
_spec = importlib.util.spec_from_file_location("merge_judgments", _MODULE_PATH)
mj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mj)


def _raw(judge, query_id, candidate_id, grade):
    return mj.RawJudgment(judge=judge, query_id=query_id, candidate_id=candidate_id, grade=grade)


class TestMedianAndEscalation(unittest.TestCase):
    def test_plain_median_no_escalation(self):
        # grades 1,1,2 -> median 1, not full disagreement, not a ground-truth item.
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 1),
            _raw("agent_eval_judge_b", "q1", "c1", 1),
            _raw("agent_eval_judge_c", "q1", "c1", 2),
        ]
        result = mj.merge_query_judgments(raws)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].median_grade, 1)
        self.assertFalse(result[0].escalated)
        self.assertIsNone(result[0].escalation_reason)

    def test_full_disagreement_triggers_escalation(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 0),
            _raw("agent_eval_judge_b", "q1", "c1", 1),
            _raw("agent_eval_judge_c", "q1", "c1", 2),
        ]
        result = mj.merge_query_judgments(raws)
        item = result[0]
        self.assertEqual(item.median_grade, 1)
        self.assertTrue(item.escalated)
        self.assertIn("full_disagreement", item.escalation_reason)

    def test_ground_truth_conflict_triggers_escalation(self):
        # median grade 1 (not 2) on the query's own designated correct-answer entity.
        raws = [
            _raw("agent_eval_judge_a", "q1", "answer_entity", 1),
            _raw("agent_eval_judge_b", "q1", "answer_entity", 1),
            _raw("agent_eval_judge_c", "q1", "answer_entity", 0),
        ]
        result = mj.merge_query_judgments(raws, source_entity_ids=["answer_entity"])
        item = result[0]
        self.assertTrue(item.escalated)
        self.assertIn("ground_truth_conflict", item.escalation_reason)

    def test_ground_truth_item_graded_2_is_not_escalated_for_that_reason(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "answer_entity", 2),
            _raw("agent_eval_judge_b", "q1", "answer_entity", 2),
            _raw("agent_eval_judge_c", "q1", "answer_entity", 2),
        ]
        result = mj.merge_query_judgments(raws, source_entity_ids=["answer_entity"])
        item = result[0]
        self.assertFalse(item.escalated)

    def test_both_triggers_can_fire_together(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "answer_entity", 0),
            _raw("agent_eval_judge_b", "q1", "answer_entity", 1),
            _raw("agent_eval_judge_c", "q1", "answer_entity", 2),
        ]
        result = mj.merge_query_judgments(raws, source_entity_ids=["answer_entity"])
        item = result[0]
        self.assertIn("full_disagreement", item.escalation_reason)
        self.assertIn("ground_truth_conflict", item.escalation_reason)

    def test_multiple_candidates_grouped_independently(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 2),
            _raw("agent_eval_judge_b", "q1", "c1", 2),
            _raw("agent_eval_judge_c", "q1", "c1", 2),
            _raw("agent_eval_judge_a", "q1", "c2", 0),
            _raw("agent_eval_judge_b", "q1", "c2", 0),
            _raw("agent_eval_judge_c", "q1", "c2", 0),
        ]
        result = mj.merge_query_judgments(raws)
        by_cand = {r.candidate_id: r for r in result}
        self.assertEqual(by_cand["c1"].median_grade, 2)
        self.assertEqual(by_cand["c2"].median_grade, 0)

    def test_wrong_grade_count_raises(self):
        with self.assertRaises(ValueError):
            mj._median_int([1, 2])


class TestArbitrationOverride(unittest.TestCase):
    def test_override_replaces_final_grade_not_median(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 0),
            _raw("agent_eval_judge_b", "q1", "c1", 1),
            _raw("agent_eval_judge_c", "q1", "c1", 2),
        ]
        item = mj.merge_query_judgments(raws)[0]
        self.assertTrue(item.escalated)
        self.assertEqual(item.final_grade, item.median_grade)  # before arbitration
        mj.apply_arbitration_override(item, arbitrated_grade=2)
        self.assertEqual(item.arbitrated_grade, 2)
        self.assertEqual(item.final_grade, 2)
        self.assertEqual(item.median_grade, 1)  # median itself is preserved, not overwritten

    def test_cannot_arbitrate_non_escalated_item(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 2),
            _raw("agent_eval_judge_b", "q1", "c1", 2),
            _raw("agent_eval_judge_c", "q1", "c1", 2),
        ]
        item = mj.merge_query_judgments(raws)[0]
        with self.assertRaises(ValueError):
            mj.apply_arbitration_override(item, arbitrated_grade=1)


class TestPairwiseAgreement(unittest.TestCase):
    def test_perfect_agreement(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 2),
            _raw("agent_eval_judge_b", "q1", "c1", 2),
            _raw("agent_eval_judge_a", "q1", "c2", 0),
            _raw("agent_eval_judge_b", "q1", "c2", 0),
        ]
        agreement = mj.compute_pairwise_agreement(raws, "agent_eval_judge_a", "agent_eval_judge_b")
        self.assertEqual(agreement.n, 2)
        self.assertAlmostEqual(agreement.exact_agreement_rate, 1.0)
        self.assertAlmostEqual(agreement.cohens_kappa, 1.0, places=6)

    def test_zero_agreement_with_full_marginal_overlap(self):
        # judge_a always says 2, judge_b always says 0 -- p_o=0. Chance agreement p_e is computed
        # from marginals; kappa should be <= 0 (at or below chance).
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 2),
            _raw("agent_eval_judge_b", "q1", "c1", 0),
            _raw("agent_eval_judge_a", "q1", "c2", 2),
            _raw("agent_eval_judge_b", "q1", "c2", 0),
        ]
        agreement = mj.compute_pairwise_agreement(raws, "agent_eval_judge_a", "agent_eval_judge_b")
        self.assertEqual(agreement.exact_agreement_rate, 0.0)
        self.assertLessEqual(agreement.cohens_kappa, 0.0)

    def test_only_common_items_counted(self):
        raws = [
            _raw("agent_eval_judge_a", "q1", "c1", 2),
            _raw("agent_eval_judge_b", "q1", "c1", 2),
            _raw("agent_eval_judge_a", "q1", "c2", 1),  # judge_b never graded c2
        ]
        agreement = mj.compute_pairwise_agreement(raws, "agent_eval_judge_a", "agent_eval_judge_b")
        self.assertEqual(agreement.n, 1)

    def test_empty_input(self):
        agreement = mj.compute_pairwise_agreement([], "agent_eval_judge_a", "agent_eval_judge_b")
        self.assertEqual(agreement.n, 0)
        self.assertTrue(agreement.exact_agreement_rate != agreement.exact_agreement_rate)  # NaN


class TestCalibrationAccuracy(unittest.TestCase):
    def test_hand_computed(self):
        merged = [
            mj.MergedJudgment(
                "q1",
                "answer1",
                {"agent_eval_judge_a": 2, "agent_eval_judge_b": 2, "agent_eval_judge_c": 2},
                median_grade=2,
                escalated=False,
            ),
            mj.MergedJudgment(
                "q2",
                "answer2",
                {"agent_eval_judge_a": 1, "agent_eval_judge_b": 1, "agent_eval_judge_c": 1},
                median_grade=1,
                escalated=False,
            ),
            mj.MergedJudgment(
                "q1",
                "distractor",
                {"agent_eval_judge_a": 0, "agent_eval_judge_b": 0, "agent_eval_judge_c": 0},
                median_grade=0,
                escalated=False,
            ),
        ]
        source_map = {"q1": ["answer1"], "q2": ["answer2"]}
        result = mj.calibration_accuracy(merged, source_map)
        self.assertEqual(result["n"], 2)  # only answer1 and answer2 are ground-truth items
        self.assertAlmostEqual(result["accuracy"], 0.5, places=9)  # 1 of 2 correctly graded 2

    def test_uses_final_grade_including_arbitration(self):
        item = mj.MergedJudgment(
            "q1",
            "answer1",
            {"agent_eval_judge_a": 0, "agent_eval_judge_b": 1, "agent_eval_judge_c": 2},
            median_grade=1,
            escalated=True,
        )
        mj.apply_arbitration_override(item, arbitrated_grade=2)
        result = mj.calibration_accuracy([item], {"q1": ["answer1"]})
        self.assertEqual(result["accuracy"], 1.0)

    def test_no_ground_truth_items_gives_nan(self):
        merged = [
            mj.MergedJudgment(
                "q1",
                "c1",
                {"agent_eval_judge_a": 1, "agent_eval_judge_b": 1, "agent_eval_judge_c": 1},
                median_grade=1,
                escalated=False,
            )
        ]
        result = mj.calibration_accuracy(merged, {})
        self.assertEqual(result["n"], 0)
        self.assertTrue(result["accuracy"] != result["accuracy"])  # NaN


if __name__ == "__main__":
    unittest.main()
