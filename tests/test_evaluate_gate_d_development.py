"""Focused metric and deterministic-selection tests for the Gate-D development evaluator."""

from __future__ import annotations

import sys
import math
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from evaluate_gate_d_development import (  # noqa: E402
    GateDDevelopmentError,
    compute_metrics,
    select_winner,
)


def _query(query_id: str, family: str, facet: str, sources: list[str] | None = None) -> dict:
    return {
        "id": query_id,
        "topic_family_id": family,
        "category": facet,
        "source_entity_ids": sources or [],
    }


def _row(*ids: str, latency: float = 100.0) -> dict:
    return {"top20": [{"entity_id": item} for item in ids], "latency_ms": latency}


def test_metrics_use_grade_gains_family_macro_and_declared_facet_denominators():
    queries = [
        _query("q1", "family-a", "exact_sentence", ["a"]),
        _query("q2", "family-a", "keyword", ["b"]),
        _query("q3", "family-b", "strict_negative"),
    ]
    relevance = {"q1": {"a": 2, "x": 1}, "q2": {"b": 2}, "q3": {"z": 0}}
    rows = {
        "q1": _row("a", "x", latency=100),
        "q2": _row("x", "b", latency=200),
        "q3": _row("z", latency=300),
    }
    metrics, denominators = compute_metrics(queries, relevance, rows)
    assert metrics["macro_positive_ndcg_at_10"] == pytest.approx((1 + 1 / math.log2(3)) / 2)
    assert metrics["grade2_recall_at_20"] == 1.0
    assert metrics["same_specific_fact_grade2_top1"] == 0.5
    assert metrics["exact_safety"] == 1.0 and metrics["keyword_safety"] == 0.0
    assert metrics["strict_negative_safety"] == 1.0
    assert metrics["warm_latency_p50_seconds"] == 0.2
    assert metrics["warm_latency_p95_seconds"] == 0.3
    assert denominators["positive_ndcg_queries"] == 2


def test_metrics_reject_undefined_required_positive_denominator():
    queries = [_query("q", "f", "exact_sentence")]
    with pytest.raises(GateDDevelopmentError, match="empty denominator"):
        compute_metrics(queries, {"q": {"x": 0}}, {"q": _row("x")})


def test_winner_ordering_excludes_lexical_and_honors_every_tiebreak():
    base = {
        "macro_positive_ndcg_at_10": 0.8,
        "grade2_recall_at_20": 0.8,
        "same_specific_fact_grade2_top1": 0.8,
        "warm_latency_p95_seconds": 1.0,
        "benchmark_failures": 0,
    }
    metrics = {"lexical:bm25": dict(base), "dense:one": dict(base), "dense:two": dict(base)}
    bundles = {
        "lexical:bm25": {"cell": {"kind": "lexical"}},
        "dense:one": {"cell": {"kind": "dense"}},
        "dense:two": {"cell": {"kind": "dense"}},
    }
    winner, evidence = select_winner(metrics, bundles)
    assert winner == "dense:one"  # lexical contender ID breaks the exact final tie.
    assert (
        next(item for item in evidence if item["contender_id"] == "lexical:bm25")[
            "ineligible_reason"
        ]
        == "lexical_baseline"
    )
    metrics["dense:two"]["warm_latency_p95_seconds"] = 0.9
    assert select_winner(metrics, bundles)[0] == "dense:two"
    metrics["dense:two"]["same_specific_fact_grade2_top1"] = 0.7
    assert select_winner(metrics, bundles)[0] == "dense:one"
    metrics["dense:two"]["same_specific_fact_grade2_top1"] = 0.9
    assert select_winner(metrics, bundles)[0] == "dense:two"
    metrics["dense:two"]["grade2_recall_at_20"] = 0.7
    assert select_winner(metrics, bundles)[0] == "dense:one"
    metrics["dense:two"]["grade2_recall_at_20"] = 0.9
    assert select_winner(metrics, bundles)[0] == "dense:two"
