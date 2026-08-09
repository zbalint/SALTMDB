"""Automated tests for scripts/benchmarking/eval_stats.py -- the disambiguated, executable form
of plan §5's metric/statistics formulas (`scratch/plans/precision_first_search_evaluation.md`).
Every test case's expected value is either a hand-derived closed-form computation (checked in
these docstrings) or a structural/monotonicity property, never "whatever the code currently
returns" -- this suite exists specifically because the prose version of these formulas needed
5 rounds of Codex review to pin down; the code must be independently verifiable, not just
self-consistent.
"""

import importlib.util
import math
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "eval_stats.py"
_spec = importlib.util.spec_from_file_location("eval_stats", _MODULE_PATH)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)


class TestGainAndDCG(unittest.TestCase):
    def test_ndcg_perfect_ranking_is_one(self):
        # Only one relevant item, ranked first -> NDCG must be exactly 1.0 regardless of gain
        # function specifics (DCG == IDCG when the actual order matches the ideal order).
        relevance = {"a": 2, "b": 0, "c": 1}
        ranked = ["a", "c", "b"]  # matches ideal order (grade desc: a=2,c=1,b=0)
        self.assertAlmostEqual(es.ndcg_at_10(ranked, relevance), 1.0, places=9)

    def test_ndcg_hand_computed_example(self):
        # relevance: a=2 (gain 3), b=1 (gain 1), c=0 (gain 0)
        # config returns [a, c, b] -> DCG = 3/log2(2) + 0/log2(3) + 1/log2(4) = 3 + 0 + 0.5 = 3.5
        # ideal order [a, b, c] -> IDCG = 3/log2(2) + 1/log2(3) + 0/log2(4) = 3 + 1/log2(3)
        relevance = {"a": 2, "b": 1, "c": 0}
        ranked = ["a", "c", "b"]
        dcg = es.dcg_at_10(ranked, relevance)
        self.assertAlmostEqual(dcg, 3.0 + 0.0 + 1.0 / math.log2(4), places=9)
        idcg = es.idcg_at_10(relevance)
        self.assertAlmostEqual(idcg, 3.0 + 1.0 / math.log2(3) + 0.0, places=9)
        expected_ndcg = dcg / idcg
        self.assertAlmostEqual(es.ndcg_at_10(ranked, relevance), expected_ndcg, places=9)

    def test_ndcg_undefined_when_no_relevant_items(self):
        relevance = {"a": 0, "b": 0}
        self.assertIsNone(es.ndcg_at_10(["a", "b"], relevance))

    def test_ndcg_only_scores_top_10(self):
        # An 11th-ranked perfect hit must not count -- DCG@10 truncates.
        relevance = {f"filler{i}": 0 for i in range(10)}
        relevance["real"] = 2
        ranked = [f"filler{i}" for i in range(10)] + ["real"]
        self.assertEqual(es.dcg_at_10(ranked, relevance), 0.0)

    def test_ties_broken_by_candidate_id_deterministically(self):
        # Two grade-2 items tied -- IDCG must be identical across repeated calls (determinism),
        # and independent of dict insertion order.
        rel1 = {"z": 2, "a": 2}
        rel2 = {"a": 2, "z": 2}
        self.assertEqual(es.idcg_at_10(rel1), es.idcg_at_10(rel2))


class TestMRR(unittest.TestCase):
    def test_mrr_primary_grade_2_threshold(self):
        relevance = {"a": 1, "b": 2, "c": 0}
        ranked = ["a", "b", "c"]
        self.assertAlmostEqual(es.mrr(ranked, relevance, grade_threshold=2), 0.5, places=9)

    def test_mrr_secondary_grade_1_threshold_finds_earlier_hit(self):
        relevance = {"a": 1, "b": 2, "c": 0}
        ranked = ["a", "b", "c"]
        self.assertAlmostEqual(es.mrr(ranked, relevance, grade_threshold=1), 1.0, places=9)

    def test_mrr_zero_when_no_hit(self):
        relevance = {"a": 0, "b": 1}
        self.assertEqual(es.mrr(["a", "b"], relevance, grade_threshold=2), 0.0)


class TestRecall(unittest.TestCase):
    def test_pooled_recall_hand_computed(self):
        # 3 grade==2 items total in relevance set, 2 of them in top-10 -> 2/3.
        relevance = {"a": 2, "b": 2, "c": 2, "d": 0}
        ranked = ["a", "d", "b"]  # a and b are grade 2 and in top-10; c is not returned at all
        self.assertAlmostEqual(es.pooled_recall_at_10(ranked, relevance), 2 / 3, places=9)

    def test_pooled_recall_none_when_no_relevant_items(self):
        self.assertIsNone(es.pooled_recall_at_10(["a"], {"a": 0}))

    def test_known_answer_recall_hit(self):
        self.assertEqual(es.known_answer_recall_at_10(["x", "y", "z"], ["y"]), 1.0)

    def test_known_answer_recall_miss(self):
        self.assertEqual(es.known_answer_recall_at_10(["x", "y", "z"], ["q"]), 0.0)

    def test_known_answer_recall_none_when_no_ground_truth(self):
        self.assertIsNone(es.known_answer_recall_at_10(["x", "y"], []))


class TestFlags(unittest.TestCase):
    def test_false_accept_true_when_top1_grade_ge_1(self):
        self.assertTrue(es.false_accept_rate_flag(["a"], {"a": 1}))
        self.assertTrue(es.false_accept_rate_flag(["a"], {"a": 2}))

    def test_false_accept_false_when_top1_grade_0(self):
        self.assertFalse(es.false_accept_rate_flag(["a"], {"a": 0}))

    def test_misleading_top1_only_grade_0(self):
        self.assertTrue(es.misleading_top1_flag(["a"], {"a": 0}))
        self.assertFalse(es.misleading_top1_flag(["a"], {"a": 1}))

    def test_top1_direct_relevance_only_grade_2(self):
        self.assertTrue(es.top1_direct_relevance_flag(["a"], {"a": 2}))
        self.assertFalse(es.top1_direct_relevance_flag(["a"], {"a": 1}))

    def test_empty_ranked_ids_never_crashes(self):
        self.assertFalse(es.false_accept_rate_flag([], {}))
        self.assertFalse(es.misleading_top1_flag([], {}))
        self.assertFalse(es.top1_direct_relevance_flag([], {}))


class TestMcNemar(unittest.TestCase):
    def test_formula_matches_hand_computation(self):
        # Classic textbook shape: b=21, c=9 -> stat = (|21-9|-1)^2 / (21+9) = 11^2/30 = 4.0333...
        stat, p = es.mcnemar_continuity_corrected(b=21, c=9)
        self.assertAlmostEqual(stat, 121 / 30, places=9)
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1.0)

    def test_no_discordant_pairs_gives_p_one(self):
        stat, p = es.mcnemar_continuity_corrected(b=0, c=0)
        self.assertEqual(stat, 0.0)
        self.assertEqual(p, 1.0)

    def test_more_discordance_gives_smaller_p(self):
        _, p_small = es.mcnemar_continuity_corrected(b=6, c=4)
        _, p_large_discordance = es.mcnemar_continuity_corrected(b=40, c=0)
        self.assertLess(p_large_discordance, p_small)

    def test_symmetric_in_b_and_c(self):
        stat1, p1 = es.mcnemar_continuity_corrected(b=15, c=5)
        stat2, p2 = es.mcnemar_continuity_corrected(b=5, c=15)
        self.assertAlmostEqual(stat1, stat2, places=9)
        self.assertAlmostEqual(p1, p2, places=9)


class TestHolmAdjust(unittest.TestCase):
    def test_already_sorted_hand_computed(self):
        # p = [.01,.02,.03,.04], m=4
        # scaled: 4*.01=.04, 3*.02=.06, 2*.03=.06, 1*.04=.04
        # cummax:  .04,      .06,      .06,       .06
        raw = [0.01, 0.02, 0.03, 0.04]
        adjusted = es.holm_adjust(raw)
        expected = [0.04, 0.06, 0.06, 0.06]
        for a, e in zip(adjusted, expected):
            self.assertAlmostEqual(a, e, places=9)

    def test_identity_travels_with_original_position(self):
        # Same 4 raw values, shuffled order: [.04, .01, .03, .02]
        # sorted ascending: idx1(.01) rank1, idx3(.02) rank2, idx2(.03) rank3, idx0(.04) rank4
        # scaled: rank1=4*.01=.04; rank2=3*.02=.06; rank3=2*.03=.06; rank4=1*.04=.04
        # cummax: .04, .06, .06, .06
        # mapped back to [idx0,idx1,idx2,idx3] = [.06, .04, .06, .06]
        raw = [0.04, 0.01, 0.03, 0.02]
        adjusted = es.holm_adjust(raw)
        expected = [0.06, 0.04, 0.06, 0.06]
        for a, e in zip(adjusted, expected):
            self.assertAlmostEqual(a, e, places=9)

    def test_monotone_non_decreasing_in_sorted_order(self):
        raw = [0.001, 0.2, 0.03, 0.5]
        adjusted = es.holm_adjust(raw)
        order = sorted(range(len(raw)), key=lambda i: raw[i])
        sorted_adjusted = [adjusted[i] for i in order]
        for a, b in zip(sorted_adjusted, sorted_adjusted[1:]):
            self.assertLessEqual(a, b)

    def test_capped_at_one(self):
        adjusted = es.holm_adjust([0.9, 0.95, 0.99])
        for a in adjusted:
            self.assertLessEqual(a, 1.0)

    def test_empty_input(self):
        self.assertEqual(es.holm_adjust([]), [])


class TestWinLossTie(unittest.TestCase):
    def test_hand_computed(self):
        contender = [True, False, True, False, True]
        target = [False, False, True, True, True]
        # q1: contender hit, target miss -> WIN
        # q2: both miss -> TIE
        # q3: both hit -> TIE
        # q4: contender miss, target hit -> LOSS
        # q5: both hit -> TIE
        win, loss, tie = es.win_loss_tie_counts(contender, target)
        self.assertEqual((win, loss, tie), (1, 1, 3))

    def test_counts_sum_to_total(self):
        contender = [True, True, False, False]
        target = [True, False, True, False]
        win, loss, tie = es.win_loss_tie_counts(contender, target)
        self.assertEqual(win + loss + tie, 4)


class TestBootstrap(unittest.TestCase):
    def test_zero_variance_ci_collapses_to_point(self):
        families = [
            es.FamilyMetricSample("f1", [0.5, 0.5]),
            es.FamilyMetricSample("f2", [0.5]),
            es.FamilyMetricSample("f3", [0.5, 0.5, 0.5]),
        ]
        point, lo, hi = es.cluster_bootstrap_mean_ci(families, n_resamples=500, seed=1)
        self.assertAlmostEqual(point, 0.5, places=9)
        self.assertAlmostEqual(lo, 0.5, places=6)
        self.assertAlmostEqual(hi, 0.5, places=6)

    def test_ci_ordering(self):
        families = [
            es.FamilyMetricSample("f1", [0.1, 0.9]),
            es.FamilyMetricSample("f2", [0.3]),
            es.FamilyMetricSample("f3", [0.7, 0.2]),
            es.FamilyMetricSample("f4", [0.5]),
        ]
        point, lo, hi = es.cluster_bootstrap_mean_ci(families, n_resamples=2000, seed=2)
        self.assertLessEqual(lo, point)
        self.assertLessEqual(point, hi)

    def test_empty_families_returns_nan(self):
        point, lo, hi = es.cluster_bootstrap_mean_ci([], n_resamples=100, seed=0)
        self.assertTrue(math.isnan(point))

    def test_paired_delta_clear_separation_excludes_zero(self):
        # Config A always scores 0.9, config B always scores 0.1, on the same families ->
        # the delta CI must exclude 0 with an obvious, large positive point estimate.
        family_ids = [f"f{i}" for i in range(20)]
        a = {fid: [0.9, 0.9] for fid in family_ids}
        b = {fid: [0.1, 0.1] for fid in family_ids}
        point, lo, hi = es.cluster_bootstrap_delta_ci(a, b, n_resamples=2000, seed=3)
        self.assertAlmostEqual(point, 0.8, places=9)
        self.assertFalse(es.ci_includes_zero(lo, hi))
        self.assertGreater(lo, 0.0)

    def test_paired_delta_identical_configs_includes_zero(self):
        family_ids = [f"f{i}" for i in range(20)]
        a = {fid: [0.5, 0.6, 0.4] for fid in family_ids}
        b = {fid: [0.5, 0.6, 0.4] for fid in family_ids}
        point, lo, hi = es.cluster_bootstrap_delta_ci(a, b, n_resamples=2000, seed=4)
        self.assertEqual(point, 0.0)
        self.assertTrue(es.ci_includes_zero(lo, hi))

    def test_paired_draw_uses_same_families_for_both_configs(self):
        # If pairing were broken (independent resampling per config), a families-only-in-A
        # scenario would still "work" silently; this just checks the delta is deterministic
        # given a fixed seed (same families drawn for A and B every iteration).
        a = {"f1": [1.0], "f2": [0.0]}
        b = {"f1": [1.0], "f2": [0.0]}
        r1 = es.cluster_bootstrap_delta_ci(a, b, n_resamples=500, seed=42)
        r2 = es.cluster_bootstrap_delta_ci(a, b, n_resamples=500, seed=42)
        self.assertEqual(r1, r2)


class TestTieBreakSelection(unittest.TestCase):
    def test_no_tie_break_needed_single_clear_winner(self):
        deltas = {"cfg_a": 0.10, "cfg_b": 0.02, "cfg_c": 0.01}
        candidates = es.select_tie_break_candidates(deltas)
        self.assertEqual(candidates, ["cfg_a"])

    def test_tie_break_uses_signed_delta_not_magnitude(self):
        # cfg_a has the highest (least negative / most positive) delta; cfg_b has a large
        # NEGATIVE delta with bigger magnitude -- must NOT be selected (this is exactly the bug
        # Codex round 4 caught in the prose version: "magnitude could favor a more-negative
        # delta").
        deltas = {"cfg_a": 0.01, "cfg_b": -0.20}
        candidates = es.select_tie_break_candidates(deltas)
        self.assertEqual(candidates, ["cfg_a"])

    def test_within_tolerance_of_d_max_all_included(self):
        deltas = {"cfg_a": 0.050, "cfg_b": 0.047, "cfg_c": 0.030}
        # d_max = 0.050; cfg_b is within 0.005 (0.050-0.047=0.003 <= 0.005); cfg_c is not
        # (0.050-0.030=0.020 > 0.005).
        candidates = es.select_tie_break_candidates(deltas)
        self.assertEqual(set(candidates), {"cfg_a", "cfg_b"})

    def test_non_transitive_case_handled_by_d_max_not_chaining(self):
        # A within .005 of B, B within .005 of C, but A NOT within .005 of C -- per Codex round-4
        # fix, candidate set is defined relative to d_max only, not a pairwise chain.
        deltas = {"a": 0.010, "b": 0.006, "c": 0.001}
        # d_max = 0.010 (a). b: 0.010-0.006=0.004 <= 0.005 -> included. c: 0.010-0.001=0.009 >
        # 0.005 -> excluded, even though b and c are within .005 of each other.
        candidates = es.select_tie_break_candidates(deltas)
        self.assertEqual(set(candidates), {"a", "b"})

    def test_empty_input(self):
        self.assertEqual(es.select_tie_break_candidates({}), [])


if __name__ == "__main__":
    unittest.main()
