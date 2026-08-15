"""Synthetic contract tests for historical ColBERT compatibility evaluation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))
import historical_compatibility_evaluation as compat  # noqa: E402


def _fp(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _queries(n: int = 800) -> list[dict[str, object]]:
    return [{"query_id": f"q-{i:04d}", "split": "blind", "text": f"query {i}"} for i in range(n)]


def _bundle(name: str, *, query_fp: str = "qfp", corpus_fp: str = "cfp") -> dict[str, object]:
    return {
        "system_id": name,
        "configuration_id": "frozen-config",
        "model_id": "frozen-model",
        "index_id": "frozen-index",
        "model_revision": "frozen-revision",
        "model_lock_fingerprint": "lock-fingerprint",
        "tokenizer_fingerprint": "tokenizer-fingerprint",
        "index_fingerprint": "index-fingerprint",
        "configuration_fingerprint": "configuration-fingerprint",
        "query_manifest_fingerprint": query_fp,
        "corpus_fingerprint": corpus_fp,
        "results": {
            f"q-{i:04d}": [{"entity_id": "e-good"}, {"entity_id": "e-other"}] for i in range(800)
        },
    }


def test_manifest_requires_exactly_800_unique_blind_queries():
    assert compat.validate_blind_manifest(_queries()) == 800
    with pytest.raises(compat.HistoricalCompatibilityError, match="exactly 800"):
        compat.validate_blind_manifest(_queries(799))
    duplicate = _queries()
    duplicate[-1]["query_id"] = duplicate[0]["query_id"]
    with pytest.raises(compat.HistoricalCompatibilityError, match="unique"):
        compat.validate_blind_manifest(duplicate)


def test_manifest_rejects_non_blind_rows():
    rows = _queries()
    rows[-1]["split"] = "dev"
    with pytest.raises(compat.HistoricalCompatibilityError, match="blind"):
        compat.validate_blind_manifest(rows)


def test_bundle_identity_requires_baseline_and_colbert_and_matching_fingerprints():
    bundles = [
        _bundle("broad_rt0_pdt0_ds0_ce0"),
        _bundle("late_interaction:answerdotai/answerai-colbert-small-v1:entity", corpus_fp="cfp"),
    ]
    assert compat.validate_bundle_identity(bundles, query_fp="qfp", corpus_fp="cfp") is None
    with pytest.raises(compat.HistoricalCompatibilityError, match="fingerprint"):
        compat.validate_bundle_identity(
            [_bundle("baseline", query_fp="wrong"), bundles[1]], query_fp="qfp", corpus_fp="cfp"
        )


def test_pool_is_fresh_union_and_reuses_only_exact_content_hash_labels():
    baseline = {
        "q-0000": [
            {"entity_id": "e-a", "content_hash": "ha"},
            {"entity_id": "e-b", "content_hash": "hb"},
        ]
    }
    candidate = {
        "q-0000": [
            {"entity_id": "e-b", "content_hash": "hb"},
            {"entity_id": "e-c", "content_hash": "hc"},
        ]
    }
    pool = compat.build_two_system_pool(baseline, candidate, depth=20)
    assert {row["entity_id"] for row in pool["q-0000"]} == {"e-a", "e-b", "e-c"}
    labels = {("q-0000", "e-b", "hb"): {"grade": 2}}
    assert compat.reuse_exact_labels(pool, labels) == labels
    assert compat.reuse_exact_labels(pool, {("q-0000", "e-b", "stale"): {"grade": 2}}) == {}


@pytest.mark.parametrize("bad", ["missing", "extraneous", "unresolved"])
def test_labels_fail_closed(bad: str):
    labels = {"q-0000|e-a|ha": {"grade": 2}}
    if bad == "missing":
        labels = {}
    elif bad == "extraneous":
        labels["q-9999|e-z|hz"] = {"grade": 0}
    else:
        labels["q-0000|e-b|hb"] = {"grade": None, "disagreement": True}
    with pytest.raises(compat.HistoricalCompatibilityError, match="label"):
        compat.validate_final_labels({"q-0000|e-a|ha", "q-0000|e-b|hb"}, labels)


def test_decision_requires_ndcg_ci_holm_safety_and_latency():
    passing = {
        "paired_ndcg_improvement": 0.05,
        "ndcg_ci_low": 0.01,
        "holm_p": 0.04,
        "safety_regression": 0.005,
        "warm_p95_seconds": 4.9,
    }
    result = compat.decide(passing)
    assert result.decision == "PROMOTED"
    for key, value in (
        ("ndcg_ci_low", -0.01),
        ("holm_p", 0.2),
        ("safety_regression", 0.02),
        ("warm_p95_seconds", 5.1),
    ):
        metrics = dict(passing, **{key: value})
        assert compat.decide(metrics).decision == "RETAINED"


def test_output_is_historical_compatibility_evaluation_not_gate_d():
    result = compat.make_result(decision="RETAINED", metrics={})
    assert result["artifact_type"] == "HistoricalCompatibilityEvaluation"
    assert "GateD" not in json.dumps(result)
