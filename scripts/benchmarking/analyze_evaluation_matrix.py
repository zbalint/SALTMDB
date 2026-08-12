"""Analyze frozen evaluation artifacts without permitting blind-result leakage.

Development freezes a deterministic shortlist (current default plus three contenders). Blind
analysis accepts that immutable artifact and performs inferential comparisons only for its four
predeclared pairs, while still reporting all 24 configurations descriptively.
"""

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_configs import CURRENT_DEFAULT_CONFIG_NAME, _build_evaluation_configs
from eval_stats import (
    FamilyMetricSample,
    cluster_bootstrap_delta_ci,
    cluster_bootstrap_mean_ci,
    false_accept_rate_flag,
    holm_adjust,
    known_answer_recall_at_10,
    mcnemar_continuity_corrected,
    misleading_top1_flag,
    mrr,
    ndcg_at_10,
    pooled_recall_at_10,
    top1_direct_relevance_flag,
    win_loss_tie_counts,
    select_tie_break_candidates,
    ci_includes_zero,
)

NEGATIVE_CATEGORIES = frozenset(
    {
        "pure_gibberish",
        "partial_real_word_nonsense",
        "nl_off_topic",
        "false_premise",
        "fictional_unanswerable",
        "vocabulary_overlap_mismatch",
    }
)
HIGH_VALUE_CATEGORIES = frozenset({"current_vs_superseded", "closely_related_incident"})


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _load_queries(path: Path) -> list[dict]:
    value = json.loads(path.read_text())
    return value.get("queries", []) if isinstance(value, dict) else value


def _labels(value: object) -> list[dict]:
    if isinstance(value, dict):
        # A merged artifact is a single, complete label set.  Sharded/raw artifacts are
        # deliberately rejected here: analysis must never silently analyze a partial pool.
        if "shards" in value or not isinstance(value.get("labels"), list):
            raise ValueError("analysis requires one complete merged labels artifact")
        if "fingerprint" in value:
            copy = dict(value)
            fingerprint = copy.pop("fingerprint")
            if fingerprint != _hash(copy):
                raise ValueError("merged labels fingerprint mismatch")
        return value["labels"]
    if not isinstance(value, list):
        raise ValueError("labels must be a complete list or merged artifact")
    return value


def _validate_embedded_fingerprint(value: object, label: str) -> None:
    if not isinstance(value, dict) or "fingerprint" not in value:
        return
    copy = dict(value)
    fingerprint = copy.pop("fingerprint")
    if fingerprint != _hash(copy):
        raise ValueError(f"{label} fingerprint mismatch")


def _relevance_by_query(merged_labels: list[dict]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in merged_labels:
        if not isinstance(item, dict):
            raise ValueError("invalid merged label")
        query_id, candidate_id = item.get("query_id"), item.get("candidate_id")
        if not isinstance(query_id, str) or not isinstance(candidate_id, str):
            raise ValueError("invalid merged label")
        grade = item.get("final_grade")
        if grade is None:
            grade = item.get("arbitrated_grade")
        if grade is None:
            grade = item.get("median_grade")
        if grade not in (0, 1, 2) or candidate_id in result.setdefault(query_id, {}):
            raise ValueError("invalid or duplicate merged grade")
        result[query_id][candidate_id] = grade
    return result


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _samples(values: dict[str, list[float]]) -> list[FamilyMetricSample]:
    return [FamilyMetricSample(family, series) for family, series in sorted(values.items())]


def _with_ci(values: dict[str, list[float]], n_resamples: int) -> dict:
    point, low, high = cluster_bootstrap_mean_ci(_samples(values), n_resamples=n_resamples)
    return {
        "value": None if math.isnan(point) else point,
        "ci95": [None if math.isnan(low) else low, None if math.isnan(high) else high],
        "n": sum(len(series) for series in values.values()),
    }


def _mcnemar(b: int, c: int) -> tuple[float, float]:
    """Use scipy when available, with the exact chi-square(1) survival fallback."""
    try:
        return mcnemar_continuity_corrected(b, c)
    except ModuleNotFoundError as exc:
        if exc.name != "scipy":
            raise
        if b + c == 0:
            return 0.0, 1.0
        statistic = ((abs(b - c) - 1) ** 2) / (b + c)
        return statistic, math.erfc(math.sqrt(statistic / 2.0))


def _validate_inputs(  # noqa: C901, PLR0912
    queries: list[dict], matrix: dict, relevance: dict[str, dict[str, int]], split: str
) -> list[str]:
    if matrix.get("errors"):
        raise ValueError("matrix contains errors; retry before analysis")
    _validate_embedded_fingerprint(matrix, "matrix")
    expected_configs = {config["name"] for config in _build_evaluation_configs()}
    rankings, pools = matrix.get("config_rankings", {}), matrix.get("pools", {})
    seen = set()
    if len({query.get("id") for query in queries}) != len(queries):
        raise ValueError("duplicate query ids")
    for query in queries:
        query_id = query.get("id")
        if query.get("split") != split:
            raise ValueError(f"query {query_id} does not match requested split {split}")
        if (
            query_id in seen
            or query_id not in rankings
            or query_id not in pools
            or query_id not in relevance
        ):
            raise ValueError(f"missing complete matrix/labels for {query_id}")
        seen.add(query_id)
        if set(rankings[query_id]) != expected_configs:
            raise ValueError(f"matrix configs incomplete for {query_id}")
        if set(relevance[query_id]) != set(pools[query_id]):
            raise ValueError(f"merged labels do not exactly cover pooled candidates for {query_id}")
        for config_name, ranking in rankings[query_id].items():
            if len(ranking) != len(set(ranking)) or not set(ranking).issubset(pools[query_id]):
                raise ValueError(f"invalid ranking for {query_id}/{config_name}")
    if set(rankings) != seen or set(relevance) != seen:
        raise ValueError("matrix or labels contains a query outside the selected split")
    meta = matrix.get("resume_meta", matrix.get("meta", {}))
    if isinstance(meta, dict):
        query_hash = _hash(queries)
        config_hash = _hash(_build_evaluation_configs())
        if meta.get("queries_fingerprint") not in (None, query_hash):
            raise ValueError("matrix query fingerprint mismatch")
        if meta.get("configs_fingerprint") not in (None, config_hash):
            raise ValueError("matrix config fingerprint mismatch")
    return sorted(expected_configs)


def analyze(  # noqa: C901, PLR0912
    queries: list[dict],
    matrix: dict,
    merged_labels: list[dict] | dict,
    n_resamples: int = 10000,
    split: str = "dev",
) -> dict:
    """Return all descriptives and reusable paired comparison inputs for one frozen split."""
    relevance = _relevance_by_query(_labels(merged_labels))
    configs = _validate_inputs(queries, matrix, relevance, split)
    metric_names = (
        "ndcg_at_10",
        "mrr",
        "mrr_at_ge1",
        "pooled_recall_at_10",
        "known_answer_recall_at_10",
        "top1_direct_relevance",
        "misleading_top1",
        "false_accept_rate",
    )
    rows = {name: {metric: {} for metric in metric_names} for name in configs}
    category_rows = {name: {} for name in configs}
    slices = {name: {} for name in configs}
    top1_vectors = {name: [] for name in configs}
    top1_query_ids: list[str] = []
    slice_vectors: dict[str, dict[str, list[bool]]] = {
        category: {name: [] for name in configs} for category in HIGH_VALUE_CATEGORIES
    }
    slice_query_ids: dict[str, list[str]] = {category: [] for category in HIGH_VALUE_CATEGORIES}
    metric_families_by_metric = {name: {metric: {} for metric in metric_names} for name in configs}
    query_categories: dict[str, str] = {}
    for query in queries:
        qid, family, category = query["id"], query["topic_family_id"], query["category"]
        is_negative = category in NEGATIVE_CATEGORIES
        query_categories[qid] = category
        if category in HIGH_VALUE_CATEGORIES:
            slice_query_ids[category].append(qid)
        for name in configs:
            ranking = matrix["config_rankings"][qid][name]
            rel = relevance[qid]
            metric_values = {
                "misleading_top1": float(misleading_top1_flag(ranking, rel)),
            }
            if is_negative:
                metric_values["false_accept_rate"] = float(false_accept_rate_flag(ranking, rel))
            if not is_negative:
                metric_values.update(
                    {
                        "ndcg_at_10": ndcg_at_10(ranking, rel),
                        "mrr": mrr(ranking, rel, 2),
                        "mrr_at_ge1": mrr(ranking, rel, 1),
                        "pooled_recall_at_10": pooled_recall_at_10(ranking, rel),
                        "known_answer_recall_at_10": known_answer_recall_at_10(
                            ranking, query.get("source_entity_ids", [])
                        ),
                        "top1_direct_relevance": float(top1_direct_relevance_flag(ranking, rel)),
                    }
                )
                top1_vectors[name].append(bool(metric_values["top1_direct_relevance"]))
                if category in HIGH_VALUE_CATEGORIES:
                    slice_vectors[category][name].append(
                        bool(metric_values["top1_direct_relevance"])
                    )
            for metric, value in metric_values.items():
                if value is not None:
                    rows[name][metric].setdefault(family, []).append(value)
                    category_rows[name].setdefault(category, {}).setdefault(metric, {}).setdefault(
                        family, []
                    ).append(value)
                    metric_families_by_metric[name][metric].setdefault(family, []).append(value)
                    if category in HIGH_VALUE_CATEGORIES:
                        slices[name].setdefault(category, {}).setdefault(metric, {}).setdefault(
                            family, []
                        ).append(value)
    metrics = {
        name: {metric: _with_ci(values, n_resamples) for metric, values in row.items()}
        for name, row in rows.items()
    }
    high_value = {
        name: {
            category: {
                metric: _with_ci(values, n_resamples) for metric, values in category_values.items()
            }
            for category, category_values in config_values.items()
        }
        for name, config_values in slices.items()
    }
    per_category = {
        category: {
            name: {
                metric: _with_ci(values, n_resamples) for metric, values in category_values.items()
            }
            for name, config_values in category_rows.items()
            for category_values in [config_values.get(category, {})]
        }
        for category in sorted({q["category"] for q in queries})
    }

    latency = {}
    for name in configs:
        values = [float(value) for value in matrix.get("latencies_ms", {}).get(name, [])]
        ordered = sorted(values)
        latency[name] = {
            "n": len(values),
            "mean_ms": statistics.fmean(values) if values else None,
            "p50_ms": ordered[int(0.50 * (len(ordered) - 1))] if ordered else None,
            "p95_ms": ordered[int(0.95 * (len(ordered) - 1))] if ordered else None,
        }

    quality = {}
    if isinstance(merged_labels, dict):
        for key in ("calibration", "agreement", "escalation"):
            if key in merged_labels:
                quality[key] = merged_labels[key]
    if top1_vectors[configs[0]]:
        top1_query_ids = [q["id"] for q in queries if q["category"] not in NEGATIVE_CATEGORIES]
    return {
        "split": split,
        "metrics": metrics,
        "per_category": per_category,
        "category_metrics": per_category,
        "per_config_categories": {
            name: {
                category: per_category.get(category, {}).get(name, {}) for category in per_category
            }
            for name in configs
        },
        "high_value_slices": high_value,
        "slice_vectors": slice_vectors,
        "slice_query_ids": slice_query_ids,
        "top1_vectors": top1_vectors,
        "top1_query_ids": top1_query_ids,
        "metric_families": {name: rows[name]["ndcg_at_10"] for name in configs},
        "metric_families_by_metric": metric_families_by_metric,
        "query_categories": query_categories,
        "latency": latency,
        "latency_ms": latency,
        "judge_quality": quality,
        "calibration": quality.get("calibration"),
        "agreement": quality.get("agreement"),
        "escalation": quality.get("escalation"),
        "input_hashes": {
            "queries": _hash(queries),
            "matrix": _hash(matrix),
            "merged_labels": _hash(_labels(merged_labels)),
        },
    }


def freeze_dev_contenders(analysis: dict) -> dict:
    metrics = analysis["metrics"]
    if analysis.get("split") != "dev" or CURRENT_DEFAULT_CONFIG_NAME not in metrics:
        raise ValueError("current default absent from development matrix")
    ranked = sorted(
        (name for name in metrics if name != CURRENT_DEFAULT_CONFIG_NAME),
        key=lambda name: (
            -(
                metrics[name]["ndcg_at_10"]["value"]
                if metrics[name]["ndcg_at_10"]["value"] is not None
                else -1
            ),
            -(metrics[name]["mrr"]["value"] if metrics[name]["mrr"]["value"] is not None else -1),
            name,
        ),
    )
    if len(ranked) < 3:
        raise ValueError("need three non-default configurations")
    contenders = ranked[:3]
    shortlist = {
        "schema_version": 1,
        "current_default": CURRENT_DEFAULT_CONFIG_NAME,
        "contenders": contenders,
        "dev_ranking": ranked,
        "comparisons": [
            [contenders[0], CURRENT_DEFAULT_CONFIG_NAME],
            [contenders[1], CURRENT_DEFAULT_CONFIG_NAME],
            [contenders[2], CURRENT_DEFAULT_CONFIG_NAME],
            [contenders[0], contenders[1]],
        ],
        "development_input_hashes": analysis["input_hashes"],
    }
    shortlist["fingerprint"] = _hash(shortlist)
    return shortlist


def validate_frozen_shortlist(shortlist: dict) -> None:
    expected = {
        "schema_version",
        "current_default",
        "contenders",
        "dev_ranking",
        "comparisons",
        "development_input_hashes",
        "fingerprint",
    }
    if set(shortlist) != expected or shortlist["current_default"] != CURRENT_DEFAULT_CONFIG_NAME:
        raise ValueError("malformed frozen development shortlist")
    if (
        len(shortlist["contenders"]) != 3
        or len(set(shortlist["contenders"])) != 3
        or len(shortlist["comparisons"]) != 4
        or shortlist["dev_ranking"][:3] != shortlist["contenders"]
    ):
        raise ValueError("malformed frozen development shortlist")
    expected_pairs = [
        [shortlist["contenders"][0], CURRENT_DEFAULT_CONFIG_NAME],
        [shortlist["contenders"][1], CURRENT_DEFAULT_CONFIG_NAME],
        [shortlist["contenders"][2], CURRENT_DEFAULT_CONFIG_NAME],
        [shortlist["contenders"][0], shortlist["contenders"][1]],
    ]
    if shortlist["comparisons"] != expected_pairs:
        raise ValueError("frozen shortlist comparisons do not match the preregistered family")
    copy = dict(shortlist)
    fingerprint = copy.pop("fingerprint")
    if fingerprint != _hash(copy):
        raise ValueError("frozen development shortlist fingerprint mismatch")


def paired_comparisons(
    analysis: dict, comparisons: list[list[str]], n_resamples: int = 10000
) -> list[dict]:
    """Compute exactly the predeclared paired tests; caller controls comparison list."""
    if len(comparisons) != 4 or any(
        not isinstance(pair, list) or len(pair) != 2 for pair in comparisons
    ):
        raise ValueError("blind analysis requires exactly four predeclared comparisons")
    names = set(analysis["metrics"])
    if any(
        pair[0] not in names or pair[1] not in names or pair[0] == pair[1] for pair in comparisons
    ):
        raise ValueError("comparison references an unknown or duplicate configuration")
    result, pvalues = [], []
    families_by_metric = analysis.get("metric_families_by_metric", {})
    for contender, baseline in comparisons:
        a, b = analysis["top1_vectors"][contender], analysis["top1_vectors"][baseline]
        if len(a) != len(b):
            raise ValueError("paired vectors do not align")
        wins, losses, ties = win_loss_tie_counts(a, b)
        stat, pvalue = _mcnemar(wins, losses)
        delta, low, high = cluster_bootstrap_delta_ci(
            analysis["metric_families"][contender],
            analysis["metric_families"][baseline],
            n_resamples=n_resamples,
        )
        mrr_delta, mrr_low, mrr_high = cluster_bootstrap_delta_ci(
            families_by_metric.get(contender, {}).get("mrr", {}),
            families_by_metric.get(baseline, {}).get("mrr", {}),
            n_resamples=n_resamples,
        )
        known_delta, known_low, known_high = cluster_bootstrap_delta_ci(
            families_by_metric.get(contender, {}).get("known_answer_recall_at_10", {}),
            families_by_metric.get(baseline, {}).get("known_answer_recall_at_10", {}),
            n_resamples=n_resamples,
        )
        result.append(
            {
                "contender": contender,
                "baseline": baseline,
                "top1_win_loss_tie": [wins, losses, ties],
                "mcnemar_chi2": stat,
                "mcnemar_p": pvalue,
                "ndcg_delta": delta,
                "ndcg_delta_ci95": [low, high],
                "holm_significant": False,
                "ndcg_holm_delta_ci95": [low, high],
                "mrr_delta": mrr_delta,
                "mrr_delta_ci95": [mrr_low, mrr_high],
                "known_answer_recall_delta": known_delta,
                "known_answer_recall_delta_ci95": [known_low, known_high],
                "query_count": len(a),
            }
        )
        pvalues.append(pvalue)
    for item, adjusted in zip(result, holm_adjust(pvalues)):
        item["holm_adjusted_p"] = adjusted
        item["holm_significant"] = adjusted < 0.05
        item["comparison"] = [item["contender"], item["baseline"]]
    return result


def high_value_slice_results(
    analysis: dict, shortlist: dict, n_resamples: int = 10000
) -> dict[str, dict[str, dict]]:
    """Run the uncorrected, secondary McNemar test for each contender and value slice."""
    default = shortlist["current_default"]
    result: dict[str, dict[str, dict]] = {}
    vectors = analysis.get("slice_vectors", {})
    for category in sorted(HIGH_VALUE_CATEGORIES):
        result[category] = {}
        for contender in shortlist["contenders"]:
            a = vectors.get(category, {}).get(contender, [])
            b = vectors.get(category, {}).get(default, [])
            if len(a) != len(b):
                raise ValueError(f"high-value slice vectors do not align for {category}")
            wins, losses, ties = win_loss_tie_counts(a, b)
            stat, pvalue = _mcnemar(wins, losses)
            delta = (wins / len(a) - losses / len(a)) if a else None
            result[category][contender] = {
                "contender": contender,
                "baseline": default,
                "n": len(a),
                "win_loss_tie": [wins, losses, ties],
                "top1_win_loss_tie": [wins, losses, ties],
                "mcnemar_chi2": stat,
                "mcnemar_p": pvalue,
                "top1_delta": delta,
                "wins_slice": bool(a) and pvalue < 0.05 and delta > 0,
            }
    return result


def _metric_value(analysis: dict, config: str, metric: str) -> float:
    value = analysis.get("metrics", {}).get(config, {}).get(metric, {}).get("value")
    return float(value) if value is not None and not math.isnan(value) else 0.0


def blind_decision(analysis: dict, comparisons: list[dict], shortlist: dict) -> dict:
    """Apply §5e–§5g exactly; blind NDCG, not top-1, controls the 4pp rule."""
    validate_frozen_shortlist(shortlist)
    if analysis.get("split") != "blind":
        raise ValueError("blind decision requires blind analysis")
    default = shortlist["current_default"]
    expected_pairs = {tuple(pair) for pair in shortlist["comparisons"]}
    actual_pairs = {(item.get("contender"), item.get("baseline")) for item in comparisons}
    if actual_pairs != expected_pairs or len(comparisons) != 4:
        raise ValueError("decision requires exactly the frozen four comparisons")
    slices = high_value_slice_results(analysis, shortlist)
    by_contender = {item["contender"]: item for item in comparisons if item["baseline"] == default}
    evidence = {}
    qualifying: dict[str, float] = {}
    for contender in shortlist["contenders"]:
        item = by_contender[contender]
        delta = float(item["ndcg_delta"])
        primary = item["holm_adjusted_p"] < 0.05 and delta >= 0.04 and delta > 0
        ndcg_ci = item.get("ndcg_delta_ci95", [None, None])
        tied_ndcg = ndcg_ci[0] is not None and ndcg_ci[1] is not None and ci_includes_zero(*ndcg_ci)
        slice_wins = all(
            slices[category][contender]["wins_slice"] for category in HIGH_VALUE_CATEGORIES
        )
        value_only = tied_ndcg and slice_wins
        status = (
            "WIN" if primary else "NO-PRIMARY-WIN-BUT-WINS-ON-VALUE" if value_only else "NO_WIN"
        )
        if primary or value_only:
            qualifying[contender] = delta
        evidence[contender] = {
            "status": status,
            "blind_ndcg_delta": delta,
            "holm_adjusted_p": item["holm_adjusted_p"],
            "holm_significant": item["holm_adjusted_p"] < 0.05,
            "ndcg_delta_ci95": item["ndcg_delta_ci95"],
            "ndcg_tied": tied_ndcg,
            "wins_both_high_value_slices": slice_wins,
        }
    tie_candidates = select_tie_break_candidates(qualifying)
    selected = default
    tie_break = {"triggered": False, "candidates": tie_candidates, "steps": []}
    if qualifying:
        if len(tie_candidates) == 1:
            selected = tie_candidates[0]
        else:
            tie_break["triggered"] = True
            # The tuple is the fully specified §5g sequence.  The final default fallback is
            # represented explicitly below because candidates are non-default by construction.
            high_value_score = {
                contender: sum(
                    slices[category][contender]["win_loss_tie"][0]
                    / max(1, slices[category][contender]["n"])
                    for category in HIGH_VALUE_CATEGORIES
                )
                for contender in tie_candidates
            }
            selected = sorted(
                tie_candidates,
                key=lambda contender: (
                    -qualifying[contender],
                    -_metric_value(analysis, contender, "mrr"),
                    -_metric_value(analysis, contender, "known_answer_recall_at_10"),
                    _metric_value(analysis, contender, "misleading_top1"),
                    -high_value_score[contender],
                    contender == default,
                    contender,
                ),
            )[0]
            tie_break["steps"] = [
                "signed_ndcg_delta",
                "mrr",
                "known_answer_recall_at_10",
                "lower_misleading_top1",
                "high_value_slice_win_rate_sum",
                "current_default_final_resort",
            ]
    return {
        "outcome": "select_contender" if qualifying else "retain_current_default",
        "winner": selected,
        "reason": (
            "highest qualifying blind NDCG delta under the preregistered rule"
            if qualifying
            else "no frozen contender met the preregistered Holm, 4pp, and high-value criteria"
        ),
        "status_quo": default,
        "qualifying": qualifying,
        "candidate_evidence": evidence,
        "high_value_slices": slices,
        "tie_break": tie_break,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("dev", "blind"), required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dev-contenders", type=Path)
    parser.add_argument("--resamples", type=int, default=10000)
    args = parser.parse_args()
    if args.split == "blind" and (not args.dev_contenders or not args.dev_contenders.exists()):
        raise RuntimeError("blind analysis requires frozen --dev-contenders")
    analysis = analyze(
        _load_queries(args.queries),
        json.loads(args.matrix.read_text()),
        json.loads(args.labels.read_text()),
        args.resamples,
        args.split,
    )
    result = {"analysis": analysis}
    if args.split == "dev":
        result["contenders"] = freeze_dev_contenders(analysis)
        result["frozen_dev_shortlist"] = result["contenders"]
    else:
        shortlist = json.loads(args.dev_contenders.read_text())
        validate_frozen_shortlist(shortlist)
        comparisons = paired_comparisons(analysis, shortlist["comparisons"], args.resamples)
        result.update(
            {
                "dev_contenders": shortlist,
                "comparisons": comparisons,
                "comparison_results": comparisons,
                "decision": blind_decision(analysis, comparisons, shortlist),
            }
        )
        result["final_decision"] = result["decision"]
    result["fingerprint"] = _hash(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(args.out.suffix + ".tmp")
    temp.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    temp.replace(args.out)


if __name__ == "__main__":
    main()
