"""Automated tests for scripts/benchmarking/eval_configs.py -- the plan's 24-config matrix,
per §0b item 15 / §3 (`scratch/plans/precision_first_search_evaluation.md`)."""

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking" / "eval_configs.py"
_spec = importlib.util.spec_from_file_location("eval_configs", _MODULE_PATH)
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)


class TestBuildEvaluationConfigs(unittest.TestCase):
    def test_total_count_is_24(self):
        configs = ec._build_evaluation_configs()
        self.assertEqual(len(configs), 24)

    def test_16_broad_5_strict_3_history(self):
        configs = ec._build_evaluation_configs()
        by_mode = {}
        for cfg in configs:
            by_mode.setdefault(cfg["mode"], []).append(cfg)
        self.assertEqual(len(by_mode["broad"]), 16)
        self.assertEqual(len(by_mode["strict"]), 5)
        self.assertEqual(len(by_mode["history"]), 3)

    def test_history_configs_correctly_named_and_flagged(self):
        configs = ec._build_evaluation_configs()
        history = {cfg["name"]: cfg for cfg in configs if cfg["mode"] == "history"}
        self.assertIn("history_all_false", history)
        self.assertIn("history_kitchen_sink", history)
        self.assertIn("history_current_default", history)

        all_false = history["history_all_false"]
        self.assertFalse(all_false["prefer_durable_types"])
        self.assertFalse(all_false["demote_superseded"])

        kitchen_sink = history["history_kitchen_sink"]
        self.assertTrue(kitchen_sink["rerank_by_topic"])
        self.assertTrue(kitchen_sink["prefer_durable_types"])
        self.assertTrue(kitchen_sink["demote_superseded"])
        self.assertTrue(kitchen_sink["use_cross_encoder"])

        current_default = history["history_current_default"]
        self.assertTrue(current_default["prefer_durable_types"])
        self.assertTrue(current_default["demote_superseded"])
        self.assertFalse(current_default["rerank_by_topic"])
        self.assertFalse(current_default["use_cross_encoder"])

    def test_no_stale_history_default_name_present(self):
        configs = ec._build_evaluation_configs()
        names = [cfg["name"] for cfg in configs]
        self.assertNotIn("history_default", names)

    def test_all_names_unique(self):
        configs = ec._build_evaluation_configs()
        names = [cfg["name"] for cfg in configs]
        self.assertEqual(len(names), len(set(names)))

    def test_current_default_config_present_and_matches_shipped_defaults(self):
        configs = ec._build_evaluation_configs()
        by_name = {cfg["name"]: cfg for cfg in configs}
        self.assertIn(ec.CURRENT_DEFAULT_CONFIG_NAME, by_name)
        default_cfg = by_name[ec.CURRENT_DEFAULT_CONFIG_NAME]
        self.assertEqual(default_cfg["mode"], "broad")
        # Matches search_memory's real shipped defaults (v0.1.0-alpha.70, commit 1be6770):
        # prefer_durable_types=True, demote_superseded=True, others False.
        self.assertTrue(default_cfg["prefer_durable_types"])
        self.assertTrue(default_cfg["demote_superseded"])
        self.assertFalse(default_cfg["rerank_by_topic"])
        self.assertFalse(default_cfg["use_cross_encoder"])

    def test_shared_builder_file_itself_unmodified(self):
        # Guards against silent scope creep: the shared benchmark_search_option_matrix.py must
        # still produce its OWN original 23-config shape (16 broad + 5 strict + 2 history),
        # never mutated by this module. (16+5+2=23 -- Codex round-3 review cited "22" for this
        # count, itself a minor arithmetic slip; verified directly against the source here rather
        # than propagating an unrechecked number, same discipline this plan applies throughout.)
        shared_build_configs = ec._load_shared_build_configs()
        shared_configs = shared_build_configs()
        self.assertEqual(len(shared_configs), 23)
        history_names = {cfg["name"] for cfg in shared_configs if cfg["mode"] == "history"}
        self.assertEqual(history_names, {"history_default", "history_kitchen_sink"})


if __name__ == "__main__":
    unittest.main()
