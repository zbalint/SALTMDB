"""Focused Stage-1 fixture/config/provenance/promotion contract tests."""

import copy
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parents[1] / "src"))

from eval_configs import (  # noqa: E402
    FUTURE_CONTROL_DEFAULTS,
    RUNTIME_BASELINE_CONFIG_NAME,
    _build_evaluation_configs,
    config_fingerprint,
    runtime_baseline_config,
)
from evaluation_artifacts import StaleArtifactError  # noqa: E402
from latency_protocol import (  # noqa: E402
    INTERLEAVED_REPETITIONS,
    WARMUP_COUNT,
    build_latency_protocol,
    interleaved_schedule,
    validate_latency_protocol,
)
from promotion_analysis import evaluate_promotion  # noqa: E402
from search_accuracy_fixtures import (  # noqa: E402
    SAFETY_HOLDOUT_GROUPS,
    SEMANTIC_NEGATIVE_TOTAL,
    SEMANTIC_POSITIVE_GROUPS,
    build_safety_holdout_manifest,
    build_semantic_blind_manifest,
    validate_fixture_manifest,
)


def _provenance_kwargs():
    return {
        "commit_fingerprint": "a" * 40,
        "corpus_fingerprint": "b" * 64,
        "random_seed": 19,
        "config_fingerprint": "c" * 64,
        "judge_version_fingerprint": "d" * 64,
    }


def _semantic_rows():
    rows = []
    for group, count in {**SEMANTIC_POSITIVE_GROUPS, "negative": SEMANTIC_NEGATIVE_TOTAL}.items():
        for index in range(count):
            row = {
                "id": f"{group}-{index}",
                "query": f"{group} sourced query {index}",
                "group": group,
                "source_reference": f"fixture:{group}:{index}",
            }
            if group != "negative":
                row["source_entity_ids"] = [f"entity:{group}:{index}"]
                row["topic_family_id"] = f"family:{group}:{index}"
            rows.append(row)
    return rows


def _safety_rows():
    rows = []
    for group, count in SAFETY_HOLDOUT_GROUPS.items():
        for index in range(count):
            rows.append(
                {
                    "id": f"{group}-{index}",
                    "query": f"{group} safety query {index}",
                    "group": group,
                    "expected_entity_id": f"entity:{group}:{index}",
                    "top_k": 10,
                    "source_reference": f"fixture:{group}:{index}",
                }
            )
    return rows


class TestRuntimeBaselineConfig(unittest.TestCase):
    def test_all_future_controls_are_explicitly_disabled(self):
        baseline = runtime_baseline_config()
        self.assertEqual(baseline["name"], RUNTIME_BASELINE_CONFIG_NAME)
        self.assertEqual(baseline["mode"], "broad")
        for key in (
            "rerank_by_topic",
            "prefer_durable_types",
            "demote_superseded",
            "use_cross_encoder",
            "use_chunk_candidates",
            "collapse_supersedes_families",
            "force_cross_encoder",
            "use_retrieval_text_candidates",
        ):
            self.assertFalse(baseline[key], key)
        self.assertEqual(baseline["oversampling_multiplier"], 1)
        self.assertEqual(baseline["candidate_window"], 0)
        self.assertEqual(baseline["chunk_weight"], 0.0)
        self.assertIsNone(baseline["cross_encoder_candidate_cap"])
        self.assertIsNone(baseline["cross_encoder_text_cap_chars"])
        self.assertEqual(baseline["retrieval_fts_weight"], 0.0)
        self.assertEqual(baseline["retrieval_vector_weight"], 0.0)
        for key in FUTURE_CONTROL_DEFAULTS:
            self.assertIn(key, baseline)

    def test_config_fingerprint_changes_when_future_control_changes(self):
        configs = _build_evaluation_configs()
        changed = copy.deepcopy(configs)
        changed[0]["chunk_weight"] = 0.25
        self.assertNotEqual(config_fingerprint(configs), config_fingerprint(changed))


class TestFixtureQuotasAndStaleness(unittest.TestCase):
    def test_signed_semantic_and_safety_manifests_have_frozen_quotas(self):
        semantic = build_semantic_blind_manifest(_semantic_rows(), **_provenance_kwargs())
        safety = build_safety_holdout_manifest(_safety_rows(), **_provenance_kwargs())
        validate_fixture_manifest(semantic)
        validate_fixture_manifest(safety)
        self.assertEqual(len(semantic["queries"]), 350)
        self.assertEqual(len(safety["queries"]), 200)

    def test_tampering_and_missing_source_are_rejected(self):
        manifest = build_semantic_blind_manifest(_semantic_rows(), **_provenance_kwargs())
        tampered = copy.deepcopy(manifest)
        tampered["queries"][0]["query"] = "invented content"
        with self.assertRaises(StaleArtifactError):
            validate_fixture_manifest(tampered)
        missing_source = _semantic_rows()
        missing_source[0].pop("source_reference", None)
        missing_source[0].pop("source_entity_ids", None)
        with self.assertRaises(ValueError):
            build_semantic_blind_manifest(missing_source, **_provenance_kwargs())


class TestLatencyAndPromotion(unittest.TestCase):
    def test_protocol_requires_warm_persistent_daemon_shape(self):
        protocol = build_latency_protocol(
            corpus_fingerprint="b" * 64, machine_fingerprint="m" * 64, daemon_id="daemon-1"
        )
        self.assertEqual(protocol["warmups"], WARMUP_COUNT)
        self.assertEqual(protocol["interleaved_repetitions"], INTERLEAVED_REPETITIONS)
        self.assertTrue(protocol["direct_service_diagnostic_only"])
        self.assertEqual(protocol["p95_limit_seconds"], 5.0)
        self.assertNotIn("max_slowdown_fraction", protocol)
        validate_latency_protocol(protocol)
        schedule = interleaved_schedule(["a", "b"])
        self.assertEqual(schedule, ["a", "b"] * INTERLEAVED_REPETITIONS)

    def test_promotion_gate_is_fail_closed_on_each_requirement(self):
        passed = evaluate_promotion(
            semantic_recall_delta=0.03,
            ndcg_delta_ci95=[0.001, 0.02],
            holm_adjusted_p=0.049,
            exact_regression=0.01,
            keyword_regression=0.01,
            negative_regression=0.01,
            benchmark_failures=0,
            candidate_p95_seconds=0.99,
            baseline_p95_seconds=0.9,
        )
        self.assertTrue(passed["promotion"])
        accuracy_first = evaluate_promotion(
            semantic_recall_delta=0.03,
            ndcg_delta_ci95=[0.001, 0.02],
            holm_adjusted_p=0.049,
            exact_regression=0.01,
            keyword_regression=0.01,
            negative_regression=0.01,
            benchmark_failures=0,
            candidate_p95_seconds=5.0,
            baseline_p95_seconds=0.5,
        )
        self.assertTrue(accuracy_first["promotion"])
        self.assertNotIn("warm_p95_slowdown_within_limit", accuracy_first["checks"])
        failed = evaluate_promotion(
            semantic_recall_delta=0.029,
            ndcg_delta_ci95=[0.001, 0.02],
            holm_adjusted_p=0.049,
            exact_regression=0.01,
            keyword_regression=0.01,
            negative_regression=0.01,
            benchmark_failures=0,
            candidate_p95_seconds=0.99,
            baseline_p95_seconds=0.9,
        )
        self.assertFalse(failed["promotion"])


if __name__ == "__main__":
    unittest.main()
