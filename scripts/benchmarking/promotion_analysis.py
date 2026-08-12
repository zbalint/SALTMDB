"""Promotion-gate calculations for the Stage-1 search-accuracy plan.

This module consumes already-produced metric vectors/artifacts.  It deliberately does not run
search, mutate the corpus, or select a runtime default.  A caller receives a complete gate
breakdown so a failed/stale/partial benchmark cannot be mistaken for a negative quality result.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Mapping, Sequence

from eval_stats import (
    cluster_bootstrap_delta_ci,
    holm_adjust,
    mcnemar_continuity_corrected,
    semantic_recall_at_20,
    win_loss_tie_counts,
)


SEMANTIC_RECALL_DELTA_MIN = 0.03
NDCG_DELTA_LOWER_BOUND_MIN = 0.0
REGRESSION_MAX = 0.01
P95_MAX_SECONDS = 1.0
SLOWDOWN_MAX = 0.15
ALPHA = 0.05


@dataclass(frozen=True)
class PromotionThresholds:
    semantic_recall_delta_min: float = SEMANTIC_RECALL_DELTA_MIN
    ndcg_delta_lower_bound_min: float = NDCG_DELTA_LOWER_BOUND_MIN
    regression_max: float = REGRESSION_MAX
    p95_max_seconds: float = P95_MAX_SECONDS
    slowdown_max: float = SLOWDOWN_MAX
    alpha: float = ALPHA


def metric_delta(candidate: Sequence[float], baseline: Sequence[float]) -> float:
    """Return candidate-minus-baseline mean delta for paired metric observations."""
    if len(candidate) != len(baseline) or not candidate:
        raise ValueError("paired metric vectors must be non-empty and equally long")
    return sum(candidate) / len(candidate) - sum(baseline) / len(baseline)


def grade2_recall_delta(
    candidate_rankings: Mapping[str, Sequence[str]],
    baseline_rankings: Mapping[str, Sequence[str]],
    relevance_by_query: Mapping[str, Mapping[str, int]],
) -> float:
    """Compute semantic grade-2 Recall@20 delta over the paired query set."""
    query_ids = sorted(set(candidate_rankings) & set(baseline_rankings) & set(relevance_by_query))
    if not query_ids:
        raise ValueError("no paired queries for semantic recall delta")
    candidate_values, baseline_values = [], []
    for query_id in query_ids:
        candidate_value = semantic_recall_at_20(
            list(candidate_rankings[query_id]), dict(relevance_by_query[query_id])
        )
        baseline_value = semantic_recall_at_20(
            list(baseline_rankings[query_id]), dict(relevance_by_query[query_id])
        )
        if candidate_value is None or baseline_value is None:
            continue
        candidate_values.append(candidate_value)
        baseline_values.append(baseline_value)
    return metric_delta(candidate_values, baseline_values)


def paired_ndcg_lower_bound(
    candidate_family_values: Mapping[str, Sequence[float]],
    baseline_family_values: Mapping[str, Sequence[float]],
    *,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Return paired NDCG delta and its cluster-bootstrap 95% interval."""
    return cluster_bootstrap_delta_ci(
        {str(key): list(values) for key, values in candidate_family_values.items()},
        {str(key): list(values) for key, values in baseline_family_values.items()},
        n_resamples=n_resamples,
        seed=seed,
    )


def grade2_mcnemar_comparison(
    candidate_hits: Sequence[bool], baseline_hits: Sequence[bool]
) -> dict:
    """Build one predeclared grade-2 top-1 McNemar comparison."""
    wins, losses, ties = win_loss_tie_counts(list(candidate_hits), list(baseline_hits))
    statistic, p_value = mcnemar_continuity_corrected(wins, losses)
    return {
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "mcnemar_chi2": statistic,
        "raw_p": p_value,
    }


def holm_mcnemar_comparisons(comparisons: Sequence[Mapping[str, object]]) -> list[dict]:
    """Apply Holm correction to a fixed list of grade-2 McNemar comparisons."""
    if not comparisons:
        raise ValueError("at least one predeclared comparison is required")
    raw_p = []
    result = []
    for comparison in comparisons:
        if "candidate_hits" not in comparison or "baseline_hits" not in comparison:
            raise ValueError("comparison lacks candidate_hits/baseline_hits")
        item = grade2_mcnemar_comparison(comparison["candidate_hits"], comparison["baseline_hits"])
        item.update(
            {
                key: value
                for key, value in comparison.items()
                if key not in {"candidate_hits", "baseline_hits"}
            }
        )
        raw_p.append(item["raw_p"])
        result.append(item)
    for item, adjusted in zip(result, holm_adjust(raw_p)):
        item["holm_adjusted_p"] = adjusted
        item["holm_significant"] = adjusted < ALPHA
    return result


def _regression_ok(value: float | None, maximum: float) -> bool:
    return value is not None and float(value) <= maximum


def evaluate_promotion(
    *,
    semantic_recall_delta: float,
    ndcg_delta_ci95: Sequence[float],
    holm_adjusted_p: float,
    exact_regression: float,
    keyword_regression: float,
    negative_regression: float,
    benchmark_failures: int,
    candidate_p95_seconds: float,
    baseline_p95_seconds: float,
    thresholds: PromotionThresholds | None = None,
) -> dict:
    """Evaluate all Stage-1 promotion criteria and return an auditable decision breakdown.

    Regression values are fractions (``0.01`` = one percentage point), and deltas are candidate
    minus baseline.  This function does not infer a pass from missing data: absent/invalid inputs
    fail closed with a ``ValueError``.
    """
    limits = thresholds or PromotionThresholds()
    if len(ndcg_delta_ci95) != 2:
        raise ValueError("ndcg_delta_ci95 must be [lower, upper]")
    ndcg_low, ndcg_high = float(ndcg_delta_ci95[0]), float(ndcg_delta_ci95[1])
    if ndcg_low > ndcg_high:
        raise ValueError("ndcg_delta_ci95 lower bound exceeds upper bound")
    if benchmark_failures < 0:
        raise ValueError("benchmark_failures cannot be negative")
    if baseline_p95_seconds <= 0 or candidate_p95_seconds < 0:
        raise ValueError("latency values are invalid")
    slowdown = candidate_p95_seconds / baseline_p95_seconds - 1.0
    checks = {
        "semantic_recall_at_20_delta": semantic_recall_delta >= limits.semantic_recall_delta_min,
        "ndcg_delta_lower_bound_positive": ndcg_low > limits.ndcg_delta_lower_bound_min,
        "holm_mcnemar_significant": holm_adjusted_p < limits.alpha,
        "exact_regression_within_limit": _regression_ok(exact_regression, limits.regression_max),
        "keyword_regression_within_limit": _regression_ok(
            keyword_regression, limits.regression_max
        ),
        "negative_regression_within_limit": _regression_ok(
            negative_regression, limits.regression_max
        ),
        "zero_benchmark_failures": benchmark_failures == 0,
        "warm_p95_under_one_second": candidate_p95_seconds < limits.p95_max_seconds,
        "warm_p95_slowdown_within_limit": slowdown <= limits.slowdown_max,
    }
    result = {
        "promotion": all(checks.values()),
        "checks": checks,
        "thresholds": asdict(limits),
        "observed": {
            "semantic_recall_at_20_delta": semantic_recall_delta,
            "ndcg_delta_ci95": [ndcg_low, ndcg_high],
            "holm_adjusted_p": holm_adjusted_p,
            "exact_regression": exact_regression,
            "keyword_regression": keyword_regression,
            "negative_regression": negative_regression,
            "benchmark_failures": benchmark_failures,
            "candidate_p95_seconds": candidate_p95_seconds,
            "baseline_p95_seconds": baseline_p95_seconds,
            "slowdown_fraction": slowdown,
        },
    }
    return result


# Compatibility names for report/fixture callers.
promotion_gate = evaluate_promotion
analyze_promotion = evaluate_promotion
