"""Analyze frozen evaluation artifacts without permitting blind-result leakage.

Development freezes a deterministic shortlist (current default plus three contenders). Blind
analysis accepts that immutable artifact and performs inferential comparisons only for its four
predeclared pairs, while still reporting all 24 configurations descriptively.
"""

import argparse
import hashlib
import json
import math
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
    return value.get("labels", []) if isinstance(value, dict) else value


def _relevance_by_query(merged_labels: list[dict]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for item in merged_labels:
        query_id, candidate_id = item["query_id"], item["candidate_id"]
        grade = item.get("final_grade", item.get("arbitrated_grade", item["median_grade"]))
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
    }


def _validate_inputs(
    queries: list[dict], matrix: dict, relevance: dict[str, dict[str, int]], split: str
) -> list[str]:
    if matrix.get("errors"):
        raise ValueError("matrix contains errors; retry before analysis")
    expected_configs = {config["name"] for config in _build_evaluation_configs()}
    rankings, pools = matrix.get("config_rankings", {}), matrix.get("pools", {})
    seen = set()
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
    return sorted(expected_configs)


def analyze(
    queries: list[dict],
    matrix: dict,
    merged_labels: list[dict] | dict,
    n_resamples: int = 10000,
    split: str = "dev",
) -> dict:
    """Return all descriptives and reusable paired comparison inputs for one frozen split."""
    relevance = _relevance_by_query(_labels(merged_labels))
    configs = _validate_inputs(queries, matrix, relevance, split)
    rows = {
        name: {
            metric: {}
            for metric in (
                "ndcg_at_10",
                "mrr",
                "pooled_recall_at_10",
                "known_answer_recall_at_10",
                "top1_direct_relevance",
                "misleading_top1",
                "false_accept_rate",
            )
        }
        for name in configs
    }
    slices = {name: {} for name in configs}
    top1_vectors = {name: [] for name in configs}
    query_ids = []
    for query in queries:
        qid, family, category = query["id"], query["topic_family_id"], query["category"]
        is_negative = category in NEGATIVE_CATEGORIES
        query_ids.append(qid)
        for name in configs:
            ranking = matrix["config_rankings"][qid][name]
            rel = relevance[qid]
            metric_values = {
                "misleading_top1": float(misleading_top1_flag(ranking, rel)),
                "false_accept_rate": float(false_accept_rate_flag(ranking, rel)),
            }
            if not is_negative:
                metric_values.update(
                    {
                        "ndcg_at_10": ndcg_at_10(ranking, rel),
                        "mrr": mrr(ranking, rel, 2),
                        "pooled_recall_at_10": pooled_recall_at_10(ranking, rel),
                        "known_answer_recall_at_10": known_answer_recall_at_10(
                            ranking, query.get("source_entity_ids", [])
                        ),
                        "top1_direct_relevance": float(top1_direct_relevance_flag(ranking, rel)),
                    }
                )
                top1_vectors[name].append(bool(metric_values["top1_direct_relevance"]))
            for metric, value in metric_values.items():
                if value is not None and (metric != "false_accept_rate" or is_negative):
                    rows[name][metric].setdefault(family, []).append(value)
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
    return {
        "split": split,
        "metrics": metrics,
        "high_value_slices": high_value,
        "top1_vectors": top1_vectors,
        "top1_query_ids": query_ids,
        "metric_families": {name: rows[name]["ndcg_at_10"] for name in configs},
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
            -(metrics[name]["top1_direct_relevance"]["value"] or -1),
            -(metrics[name]["ndcg_at_10"]["value"] or -1),
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
    ):
        raise ValueError("malformed frozen development shortlist")
    copy = dict(shortlist)
    fingerprint = copy.pop("fingerprint")
    if fingerprint != _hash(copy):
        raise ValueError("frozen development shortlist fingerprint mismatch")


def paired_comparisons(
    analysis: dict, comparisons: list[list[str]], n_resamples: int = 10000
) -> list[dict]:
    """Compute exactly the predeclared paired tests; caller controls comparison list."""
    result, pvalues = [], []
    for contender, baseline in comparisons:
        a, b = analysis["top1_vectors"][contender], analysis["top1_vectors"][baseline]
        if len(a) != len(b):
            raise ValueError("paired vectors do not align")
        wins, losses, ties = win_loss_tie_counts(a, b)
        stat, pvalue = mcnemar_continuity_corrected(wins, losses)
        delta, low, high = cluster_bootstrap_delta_ci(
            analysis["metric_families"][contender],
            analysis["metric_families"][baseline],
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
            }
        )
        pvalues.append(pvalue)
    for item, adjusted in zip(result, holm_adjust(pvalues)):
        item["holm_adjusted_p"] = adjusted
    return result


def blind_decision(analysis: dict, comparisons: list[dict], shortlist: dict) -> dict:
    """Strict, preregistered decision: >=4pp top-1 gain and Holm significance, else retain."""
    default = shortlist["current_default"]
    winners = []
    for item in comparisons:
        if item["baseline"] != default or item["holm_adjusted_p"] > 0.05:
            continue
        delta = (analysis["metrics"][item["contender"]]["top1_direct_relevance"]["value"] or 0) - (
            analysis["metrics"][default]["top1_direct_relevance"]["value"] or 0
        )
        if (
            delta >= 0.04
            and item["ndcg_delta_ci95"][0] is not None
            and item["ndcg_delta_ci95"][0] > 0
        ):
            winners.append((delta, item["contender"]))
    if not winners:
        return {
            "outcome": "retain_current_default",
            "winner": default,
            "reason": "no frozen contender met the 4pp, Holm, and paired-NDCG criteria",
        }
    return {
        "outcome": "select_contender",
        "winner": max(winners)[1],
        "reason": "highest preregistered contender satisfying the locked decision rule",
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
    else:
        if not args.dev_contenders or not args.dev_contenders.exists():
            raise RuntimeError("blind analysis requires frozen --dev-contenders")
        shortlist = json.loads(args.dev_contenders.read_text())
        validate_frozen_shortlist(shortlist)
        comparisons = paired_comparisons(analysis, shortlist["comparisons"], args.resamples)
        result.update(
            {
                "dev_contenders": shortlist,
                "comparisons": comparisons,
                "decision": blind_decision(analysis, comparisons, shortlist),
            }
        )
    result["fingerprint"] = _hash(result)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(args.out.suffix + ".tmp")
    temp.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    temp.replace(args.out)


if __name__ == "__main__":
    main()
