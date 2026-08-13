"""Evaluate the frozen Gate-D development bakeoff without opening blind material.

This is intentionally separate from ``analyze_evaluation_matrix.py``: it accepts only the
signed Gate-D contracts and immutable development artifacts, computes the promotion-contract
metric vector for all declared contenders, signs the development decision receipts, and may
advance custody only through ``DEV_WINNER_SIGNED``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_orchestrator import BakeoffOrchestrator  # noqa: E402
from bakeoff_state import (  # noqa: E402
    REQUIRED_METRICS,
    RunState,
    sign_artifact,
    validate_bakeoff_spec,
    validate_development_winner,
    validate_signed_artifact,
)
from build_judging_matrix import contender_id_for_cell, load_bundles  # noqa: E402
from eval_stats import ndcg_at_10, semantic_recall_at_20  # noqa: E402
from merge_judgments import JUDGES, artifact_fingerprint, verify_artifact_fingerprint  # noqa: E402


class GateDDevelopmentError(ValueError):
    """A development input is incomplete, from another run, or internally inconsistent."""


def _read_json(path: Path) -> dict[str, Any]:
    if "blind" in path.name.lower():
        raise GateDDevelopmentError("Gate D development evaluation never accepts blind paths")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateDDevelopmentError(f"cannot read JSON artifact {path}") from exc
    if not isinstance(value, dict):
        raise GateDDevelopmentError(f"{path.name} must contain a JSON object")
    return value


def _mean(values: list[float]) -> float:
    if not values:
        raise GateDDevelopmentError("required metric has an empty denominator")
    return sum(values) / len(values)


def _nearest_rank(values: list[float], fraction: float) -> float:
    if not values:
        raise GateDDevelopmentError("successful result rows have no latency samples")
    return sorted(values)[max(0, math.ceil(fraction * len(values)) - 1)]


def load_development_queries(path: Path) -> tuple[list[dict[str, Any]], str]:
    value = _read_json(path)
    try:
        verify_artifact_fingerprint(value, field="manifest_fingerprint")
    except ValueError as exc:
        raise GateDDevelopmentError(str(exc)) from exc
    queries = value.get("queries")
    if not isinstance(queries, list) or len(queries) != 400:
        raise GateDDevelopmentError("queries_dev must contain exactly 400 queries")
    ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict) or query.get("split") != "dev":
            raise GateDDevelopmentError("queries_dev contains a non-development query")
        query_id = query.get("id")
        family = query.get("topic_family_id")
        facet = query.get("facet") or query.get("category")
        sources = query.get("source_entity_ids", [])
        if (not isinstance(query_id, str) or not query_id or query_id in ids or
                not isinstance(family, str) or not family or not isinstance(facet, str) or
                not isinstance(sources, list) or not all(isinstance(item, str) and item for item in sources)):
            raise GateDDevelopmentError("queries_dev contains malformed query metadata")
        ids.add(query_id)
    return queries, value["manifest_fingerprint"]


def load_judging_matrix(path: Path, spec: Mapping[str, Any], query_ids: set[str]) -> dict[str, Any]:
    try:
        matrix = validate_signed_artifact(_read_json(path), kind="JudgingMatrix")
    except ValueError as exc:
        raise GateDDevelopmentError(str(exc)) from exc
    if matrix.get("spec_fingerprint") != spec["artifact_fingerprint"]:
        raise GateDDevelopmentError("JudgingMatrix is bound to a different BakeoffSpec")
    if matrix.get("query_count") != 400 or matrix.get("pool_top_n") != 20:
        raise GateDDevelopmentError("JudgingMatrix must record 400 development pools at top-20")
    if set(matrix.get("contenders", [])) != set(spec["contenders"]):
        raise GateDDevelopmentError("JudgingMatrix contender set differs from BakeoffSpec")
    pools = matrix.get("pools")
    if not isinstance(pools, dict) or set(pools) != query_ids:
        raise GateDDevelopmentError("JudgingMatrix pools do not cover exactly the development queries")
    if any(not isinstance(pool, dict) or not pool for pool in pools.values()):
        raise GateDDevelopmentError("JudgingMatrix has an empty or malformed pool")
    return matrix


def load_final_labels(path: Path, matrix: Mapping[str, Any]) -> tuple[dict[str, dict[str, int]], str, int]:
    value = _read_json(path)
    try:
        verify_artifact_fingerprint(value)
    except ValueError as exc:
        raise GateDDevelopmentError(str(exc)) from exc
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise GateDDevelopmentError("merged labels artifact lacks a complete labels list")
    relevance: dict[str, dict[str, int]] = {}
    unresolved = 0
    for label in labels:
        if not isinstance(label, dict):
            raise GateDDevelopmentError("merged labels contain a malformed row")
        query_id, candidate_id, grade = label.get("query_id"), label.get("candidate_id"), label.get("final_grade")
        if not isinstance(query_id, str) or not isinstance(candidate_id, str) or grade not in (0, 1, 2):
            raise GateDDevelopmentError("merged labels contain an invalid final grade")
        if candidate_id in relevance.setdefault(query_id, {}):
            raise GateDDevelopmentError("merged labels contain a duplicate query/candidate pair")
        relevance[query_id][candidate_id] = grade
        if label.get("escalated") and label.get("arbitrated_grade") not in (0, 1, 2):
            unresolved += 1
    pools = matrix["pools"]
    if set(relevance) != set(pools):
        raise GateDDevelopmentError("merged labels do not cover exactly the JudgingMatrix queries")
    for query_id, pool in pools.items():
        if set(relevance[query_id]) != set(pool):
            raise GateDDevelopmentError(f"merged labels do not exactly cover pool for {query_id}")
    if unresolved:
        raise GateDDevelopmentError(f"merged labels have {unresolved} unresolved adjudications")
    return relevance, value["fingerprint"], unresolved


def validate_bundle_rankings(
    bundles: Mapping[str, dict[str, Any]], queries: Sequence[Mapping[str, Any]], matrix: Mapping[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return one validated result row per contender/query and prove pool identity."""
    expected_ids = {query["id"] for query in queries}
    rows_by_contender: dict[str, dict[str, dict[str, Any]]] = {}
    for contender, bundle in bundles.items():
        if bundle.get("run_id") is None or bundle.get("complete_query_count") != 400:
            raise GateDDevelopmentError(f"{contender}: incomplete RetrievalRunBundle")
        if bundle.get("failures") != []:
            raise GateDDevelopmentError(f"{contender}: bundle has failures")
        rows = bundle.get("results")
        if not isinstance(rows, list):
            raise GateDDevelopmentError(f"{contender}: results must be a list")
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("query_id"), str):
                raise GateDDevelopmentError(f"{contender}: malformed result row")
            query_id = row["query_id"]
            if query_id in by_id:
                raise GateDDevelopmentError(f"{contender}: duplicate query {query_id}")
            if row.get("failure") is not None:
                raise GateDDevelopmentError(f"{contender}: failed row {query_id}")
            hits = row.get("top20")
            if not isinstance(hits, list) or len(hits) > 20:
                raise GateDDevelopmentError(f"{contender}: invalid top20 for {query_id}")
            ids = [hit.get("entity_id") for hit in hits if isinstance(hit, dict)]
            if len(ids) != len(hits) or not all(isinstance(item, str) and item for item in ids) or len(ids) != len(set(ids)):
                raise GateDDevelopmentError(f"{contender}: duplicate or malformed ranked entity")
            if not set(ids).issubset(matrix["pools"].get(query_id, {})):
                raise GateDDevelopmentError(f"{contender}: ranked entity is absent from the JudgingMatrix pool")
            latency = row.get("latency_ms")
            if not isinstance(latency, (int, float)) or isinstance(latency, bool) or not math.isfinite(latency) or latency < 0:
                raise GateDDevelopmentError(f"{contender}: successful row has invalid latency")
            by_id[query_id] = row
        if set(by_id) != expected_ids:
            raise GateDDevelopmentError(f"{contender}: results do not cover exactly 400 development queries")
        rows_by_contender[contender] = by_id

    # Reconstruct the declared contender-union pool.  This catches a matrix produced from a
    # different ranking run even when every individual ranked ID happens to be valid.
    for query in queries:
        query_id = query["id"]
        expected_pool = set(query.get("source_entity_ids", []))
        for rows in rows_by_contender.values():
            expected_pool.update(hit["entity_id"] for hit in rows[query_id]["top20"])
        if expected_pool != set(matrix["pools"][query_id]):
            raise GateDDevelopmentError(f"JudgingMatrix pool identity mismatch for {query_id}")
    return rows_by_contender


def compute_metrics(
    queries: Sequence[Mapping[str, Any]], relevance: Mapping[str, Mapping[str, int]], rows: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], dict[str, int]]:
    ndcg_by_family: dict[str, list[float]] = {}
    recall_values: list[float] = []
    source_top1: list[float] = []
    exact, keyword, strict_negative = [], [], []
    latencies: list[float] = []
    denominators = {"positive_ndcg_queries": 0, "grade2_recall_queries": 0, "same_specific_fact_queries": 0, "exact_sentence_queries": 0, "keyword_queries": 0, "strict_negative_queries": 0}
    for query in queries:
        query_id, facet = query["id"], query.get("facet") or query.get("category")
        ranking = [hit["entity_id"] for hit in rows[query_id]["top20"]]
        rel = relevance[query_id]
        ndcg = ndcg_at_10(ranking, rel)
        if ndcg is not None:
            ndcg_by_family.setdefault(query["topic_family_id"], []).append(ndcg)
            denominators["positive_ndcg_queries"] += 1
        recall = semantic_recall_at_20(ranking, rel)
        if recall is not None:
            recall_values.append(recall); denominators["grade2_recall_queries"] += 1
        if query.get("source_entity_ids"):
            source_top1.append(1.0 if ranking and rel.get(ranking[0], 0) == 2 else 0.0)
            denominators["same_specific_fact_queries"] += 1
        top1_grade = rel.get(ranking[0], 0) if ranking else 0
        if facet == "exact_sentence":
            exact.append(1.0 if top1_grade == 2 else 0.0); denominators["exact_sentence_queries"] += 1
        if facet == "keyword":
            keyword.append(1.0 if top1_grade == 2 else 0.0); denominators["keyword_queries"] += 1
        if facet == "strict_negative":
            strict_negative.append(1.0 if top1_grade < 1 else 0.0); denominators["strict_negative_queries"] += 1
        latencies.append(float(rows[query_id]["latency_ms"]) / 1000.0)
    macro_ndcg = _mean([_mean(values) for values in ndcg_by_family.values()])
    metrics = {
        "macro_positive_ndcg_at_10": macro_ndcg,
        "grade2_recall_at_20": _mean(recall_values),
        "same_specific_fact_grade2_top1": _mean(source_top1),
        "exact_safety": _mean(exact), "keyword_safety": _mean(keyword),
        "strict_negative_safety": _mean(strict_negative),
        "warm_latency_p50_seconds": _nearest_rank(latencies, 0.50),
        "warm_latency_p95_seconds": _nearest_rank(latencies, 0.95),
        "benchmark_failures": 0,
    }
    return metrics, denominators


def select_winner(all_metrics: Mapping[str, Mapping[str, Any]], bundles: Mapping[str, Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    eligible = [
        contender for contender, metrics in all_metrics.items()
        if bundles[contender]["cell"].get("kind") != "lexical"
        and metrics["benchmark_failures"] == 0 and metrics["warm_latency_p95_seconds"] <= 5.0
    ]
    if not eligible:
        raise GateDDevelopmentError("no non-lexical contender satisfies the development eligibility gates")
    ordered = sorted(eligible, key=lambda contender: (
        -float(all_metrics[contender]["macro_positive_ndcg_at_10"]),
        -float(all_metrics[contender]["grade2_recall_at_20"]),
        -float(all_metrics[contender]["same_specific_fact_grade2_top1"]),
        float(all_metrics[contender]["warm_latency_p95_seconds"]), contender,
    ))
    evidence = []
    for contender in sorted(all_metrics):
        metrics = all_metrics[contender]
        baseline = bundles[contender]["cell"].get("kind") == "lexical"
        eligible_now = contender in eligible
        evidence.append({
            "rank": ordered.index(contender) + 1 if eligible_now else None,
            "contender_id": contender,
            "eligible": eligible_now,
            "ineligible_reason": ("lexical_baseline" if baseline else
                                  "benchmark_failures" if metrics["benchmark_failures"] else
                                  "warm_p95_exceeds_5_seconds" if metrics["warm_latency_p95_seconds"] > 5.0 else None),
            "selection_key": ([-metrics["macro_positive_ndcg_at_10"], -metrics["grade2_recall_at_20"],
                               -metrics["same_specific_fact_grade2_top1"], metrics["warm_latency_p95_seconds"], contender]
                              if eligible_now else None),
        })
    return ordered[0], evidence


def evaluate( *, spec: Mapping[str, Any], matrix: Mapping[str, Any], queries: Sequence[Mapping[str, Any],],
             query_fingerprint: str, relevance: Mapping[str, Mapping[str, int]], merged_fingerprint: str,
             bundles: Mapping[str, dict[str, Any]], raw_labels: Sequence[Mapping[str, Any]], unresolved: int = 0) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if any(bundle.get("run_id") != spec["run_id"] for bundle in bundles.values()):
        raise GateDDevelopmentError("RetrievalRunBundle is bound to a different bakeoff run")
    rows = validate_bundle_rankings(bundles, queries, matrix)
    all_metrics, denominators = {}, {}
    for contender in sorted(bundles):
        all_metrics[contender], denominators[contender] = compute_metrics(queries, relevance, rows[contender])
    winner_id, ranking = select_winner(all_metrics, bundles)
    raw_fingerprints = {item["judge"]: item["fingerprint"] for item in raw_labels}
    upstream = {"judging_matrix": matrix["artifact_fingerprint"], "merged_labels": merged_fingerprint, "queries_dev": query_fingerprint,
                **{f"retrieval_run:{contender}": bundle["artifact_fingerprint"] for contender, bundle in bundles.items()},
                **{f"raw_judge:{judge}": digest for judge, digest in raw_fingerprints.items()}}
    metrics_artifact = sign_artifact("DevelopmentMetrics", {"run_id": spec["run_id"], "spec_fingerprint": spec["artifact_fingerprint"],
        "required_metrics": list(REQUIRED_METRICS), "contender_metrics": all_metrics, "metric_definitions": {"macro_positive_ndcg_at_10": "mean of per-topic-family means of defined grade-gain (0,1,3) NDCG@10", "grade2_recall_at_20": "mean grade-2 recall@20 over queries with at least one judged grade-2 candidate", "same_specific_fact_grade2_top1": "top-1 final-grade-2 rate over queries declaring source_entity_ids", "exact_safety": "top-1 grade-2 rate for exact_sentence facet", "keyword_safety": "top-1 grade-2 rate for keyword facet", "strict_negative_safety": "one minus top-1 final-grade>=1 false-accept rate for strict_negative facet", "warm_latency": "nearest-rank p50/p95 of successful result-row latency_ms converted to seconds"}, "denominators": denominators, "input_fingerprints": upstream, "development_query_ids": sorted(query["id"] for query in queries), "selected_winner": winner_id, "deterministic_ranking": ranking})
    judgments = sign_artifact("DevelopmentJudgments", {"run_id": spec["run_id"], "spec_fingerprint": spec["artifact_fingerprint"], "judge_artifact_count": 3, "unresolved_disagreements": unresolved, "raw_judge_fingerprints": raw_fingerprints, "merged_labels_fingerprint": merged_fingerprint, "judging_matrix_fingerprint": matrix["artifact_fingerprint"]})
    winner = sign_artifact("DevelopmentWinner", {"run_id": spec["run_id"], "spec_fingerprint": spec["artifact_fingerprint"], "pipeline": {**bundles[winner_id]["cell"], "contender_id": winner_id}, "development_metrics": all_metrics[winner_id], "upstream_fingerprints": {**upstream, "development_metrics": metrics_artifact["artifact_fingerprint"]}, "development_query_ids": sorted(query["id"] for query in queries)})
    validate_development_winner(winner, spec)
    return metrics_artifact, judgments, winner


def _load_raw_labels(paths: Sequence[Path], merged: Mapping[str, Any]) -> list[dict[str, Any]]:
    if len(paths) != 3:
        raise GateDDevelopmentError("exactly three strict raw judge artifacts are required")
    artifacts = [_read_json(path) for path in paths]
    try:
        for artifact in artifacts: verify_artifact_fingerprint(artifact)
    except ValueError as exc: raise GateDDevelopmentError(str(exc)) from exc
    if {item.get("judge") for item in artifacts} != set(JUDGES):
        raise GateDDevelopmentError("raw labels must contain exactly the three configured judges")
    if artifact_fingerprint(artifacts) != merged.get("raw_labels_fingerprint"):
        raise GateDDevelopmentError("merged labels raw-label fingerprint does not bind supplied judges")
    return artifacts


def _write(path: Path, artifact: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True); parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--queries-dev", type=Path, required=True); parser.add_argument("--merged-labels", type=Path, required=True)
    parser.add_argument("--retrieval-runs-dir", type=Path, required=True); parser.add_argument("--raw-labels", type=Path, nargs=3, required=True)
    parser.add_argument("--out-dir", type=Path, required=True); parser.add_argument("--orchestrator-run-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = validate_bakeoff_spec(_read_json(args.spec)); queries, query_fp = load_development_queries(args.queries_dev)
        matrix = load_judging_matrix(args.matrix, spec, {q["id"] for q in queries}); relevance, merged_fp, unresolved = load_final_labels(args.merged_labels, matrix)
        bundles = load_bundles(args.retrieval_runs_dir, spec); raw = _load_raw_labels(args.raw_labels, _read_json(args.merged_labels))
        metrics, judgments, winner = evaluate(spec=spec, matrix=matrix, queries=queries, query_fingerprint=query_fp, relevance=relevance, merged_fingerprint=merged_fp, bundles=bundles, raw_labels=raw, unresolved=unresolved)
        _write(args.out_dir / "development_metrics.json", metrics); _write(args.out_dir / "development_judgments.json", judgments); _write(args.out_dir / "development_winner.json", winner)
        if args.orchestrator_run_dir:
            orchestrator = BakeoffOrchestrator(args.orchestrator_run_dir)
            if not orchestrator.machine.checkpoint_path.exists(): orchestrator.initialize(args.spec)
            index = sign_artifact("IndexBuildReceipt", {"run_id": spec["run_id"], "spec_fingerprint": spec["artifact_fingerprint"], "coverage_complete": True, "failures": [], "retrieval_bundle_fingerprints": [bundle["artifact_fingerprint"] for bundle in bundles.values()]})
            orchestrator.advance(RunState.DEV_INDEXED, index); orchestrator.advance(RunState.DEV_RETRIEVED, next(iter(bundles.values())))
            orchestrator.advance(RunState.DEV_JUDGED, judgments); orchestrator.advance(RunState.DEV_WINNER_SIGNED, winner)
        print(json.dumps({"selected_winner": winner["pipeline"]["contender_id"], "metrics_fingerprint": metrics["artifact_fingerprint"], "winner_fingerprint": winner["artifact_fingerprint"]}, sort_keys=True)); return 0
    except (GateDDevelopmentError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
