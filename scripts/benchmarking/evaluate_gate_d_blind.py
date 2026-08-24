"""Post-unlock Gate-D blind evaluator.

This module intentionally has no ``--split`` switch.  It accepts only an authorized private
blind manifest and exactly two signed run bundles: the development winner
``late_interaction:answerdotai/answerai-colbert-small-v1:entity`` and ``lexical:bm25``.
Its signed outputs contain opaque IDs and fingerprints, never blind query text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bakeoff_state import (
    REQUIRED_METRICS,
    authorize_blind_file,
    build_promotion_decision,
    fingerprint,
    sign_artifact,
    validate_bakeoff_spec,
    validate_blind_manifest_receipt,
    validate_blind_unlock,
    validate_development_winner,
    validate_signed_artifact,
)
from build_judging_matrix import contender_id_for_cell
from eval_stats import (
    cluster_bootstrap_delta_ci,
    holm_adjust,
    mcnemar_continuity_corrected,
    ndcg_at_10,
)
from evaluate_gate_d_development import (
    GateDDevelopmentError,
    compute_metrics,
    validate_bundle_rankings,
)
from merge_judgments import JUDGES, artifact_fingerprint, verify_artifact_fingerprint

WINNER_ID = "late_interaction:answerdotai/answerai-colbert-small-v1:entity"
BASELINE_ID = "lexical:bm25"


class GateDBlindError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateDBlindError(f"cannot read control artifact {path}") from exc
    if not isinstance(result, dict):
        raise GateDBlindError("control artifact must be an object")
    return result


def load_authorized_blind_queries(
    *,
    queries_path: Path,
    vault_dir: Path,
    spec_path: Path,
    winner_path: Path,
    unlock_path: Path,
    receipt_path: Path,
) -> tuple[list[dict[str, Any]], str]:
    """The sole blind read; authorization and SHA-256 verification happen before bytes open."""
    raw = authorize_blind_file(
        queries_path, vault_dir, spec_path, winner_path, unlock_path, receipt_path
    )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateDBlindError("authorized blind manifest is invalid JSON") from exc
    queries = manifest.get("queries") if isinstance(manifest, dict) else None
    if not isinstance(queries, list) or len(queries) != 800:
        raise GateDBlindError("blind manifest must contain exactly 800 queries")
    ids: set[str] = set()
    for query in queries:
        if not isinstance(query, dict) or query.get("split") != "blind":
            raise GateDBlindError("blind manifest contains a non-blind query")
        query_id, family = query.get("id"), query.get("topic_family_id")
        if (
            not isinstance(query_id, str)
            or not query_id
            or query_id in ids
            or not isinstance(family, str)
            or not family
        ):
            raise GateDBlindError("blind manifest has malformed query metadata")
        ids.add(query_id)
    return queries, fingerprint(sorted(ids))


def _load_two_bundles(
    paths: Sequence[Path], spec: Mapping[str, Any], winner: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    if len(paths) != 2:
        raise GateDBlindError("blind evaluation requires exactly winner and baseline bundles")
    bundles: dict[str, dict[str, Any]] = {}
    for path in paths:
        bundle = validate_signed_artifact(_read(path), kind="RetrievalRunBundle")
        if (
            bundle.get("run_id") != spec["run_id"]
            or bundle.get("spec_fingerprint") != spec["artifact_fingerprint"]
        ):
            raise GateDBlindError("RetrievalRunBundle is stale")
        contender = contender_id_for_cell(bundle.get("cell") or {})
        if contender in bundles:
            raise GateDBlindError("duplicate blind retrieval bundle")
        bundles[contender] = bundle
    if set(bundles) != {WINNER_ID, BASELINE_ID}:
        raise GateDBlindError(
            "blind evaluation permits only the selected ColBERT winner and lexical:bm25"
        )
    if winner.get("pipeline", {}).get("contender_id") != WINNER_ID:
        raise GateDBlindError("DevelopmentWinner is not the permitted ColBERT winner")
    return bundles


def _raw_labels(paths: Sequence[Path], merged: Mapping[str, Any]) -> dict[str, str]:
    if len(paths) != 3:
        raise GateDBlindError("exactly three raw judge artifacts are required")
    artifacts = [_read(path) for path in paths]
    try:
        for artifact in artifacts:
            verify_artifact_fingerprint(artifact)
    except ValueError as exc:
        raise GateDBlindError(str(exc)) from exc
    if {item.get("judge") for item in artifacts} != set(JUDGES):
        raise GateDBlindError("raw judgments lack strict three-judge coverage")
    if artifact_fingerprint(artifacts) != merged.get("raw_labels_fingerprint"):
        raise GateDBlindError("merged labels are not bound to raw judge artifacts")
    return {item["judge"]: item["fingerprint"] for item in artifacts}


def _family_ndcg(
    queries: Sequence[Mapping[str, Any]],
    relevance: Mapping[str, Mapping[str, int]],
    rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for query in queries:
        value = ndcg_at_10(
            [h["entity_id"] for h in rows[query["id"]]["top20"]], dict(relevance[query["id"]])
        )
        if value is not None:
            result.setdefault(query["topic_family_id"], []).append(value)
    if not result:
        raise GateDBlindError("NDCG has an undefined denominator")
    return result


def _mcnemar(
    name: str,
    queries: Sequence[Mapping[str, Any]],
    relevance: Mapping[str, Mapping[str, int]],
    candidate: Mapping[str, Mapping[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    # Each predeclared accuracy metric supplies a paired binary per-query outcome.  Unknown
    # names fail closed rather than silently reusing a different statistical comparison.
    accepted = {
        "winner_vs_baseline_same_specific_fact",
        "winner_vs_baseline_ndcg_at_10",
        "winner_vs_baseline_grade2_recall_at_20",
    }
    if name not in accepted:
        raise GateDBlindError(f"unsupported frozen Holm comparison {name!r}")
    if name == "winner_vs_baseline_same_specific_fact":
        usable = [q for q in queries if q.get("source_entity_ids")]
    elif name == "winner_vs_baseline_ndcg_at_10":
        usable = [q for q in queries if any(grade > 0 for grade in relevance[q["id"]].values())]
    else:
        usable = [q for q in queries if any(grade == 2 for grade in relevance[q["id"]].values())]
    if not usable:
        raise GateDBlindError(f"{name} McNemar denominator is empty")
    outcomes = []
    for q in usable:
        rel = relevance[q["id"]]
        candidate_ids = [hit["entity_id"] for hit in candidate[q["id"]]["top20"]]
        baseline_ids = [hit["entity_id"] for hit in baseline[q["id"]]["top20"]]
        if name in {"winner_vs_baseline_same_specific_fact", "winner_vs_baseline_ndcg_at_10"}:
            c = bool(candidate_ids) and rel.get(candidate_ids[0], 0) == 2
            b = bool(baseline_ids) and rel.get(baseline_ids[0], 0) == 2
        else:
            c = any(rel.get(entity_id, 0) == 2 for entity_id in candidate_ids)
            b = any(rel.get(entity_id, 0) == 2 for entity_id in baseline_ids)
        outcomes.append((c, b))
    b_only, base_only = sum(c and not b for c, b in outcomes), sum(b and not c for c, b in outcomes)
    statistic, raw_p = mcnemar_continuity_corrected(b_only, base_only)
    return {
        "comparison": name,
        "candidate_only": b_only,
        "baseline_only": base_only,
        "statistic": statistic,
        "raw_p": raw_p,
    }


def evaluate(
    *,
    spec: Mapping[str, Any],
    winner: Mapping[str, Any],
    unlock: Mapping[str, Any],
    receipt: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    query_ids_fingerprint: str,
    matrix: Mapping[str, Any],
    merged: Mapping[str, Any],
    raw_fingerprints: Mapping[str, str],
    bundles: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_bakeoff_spec(spec)
    validate_development_winner(winner, spec)
    validate_blind_unlock(unlock, spec, winner)
    validate_blind_manifest_receipt(receipt, spec, winner, unlock)
    if (
        matrix.get("spec_fingerprint") != spec["artifact_fingerprint"]
        or matrix.get("query_count") != 800
        or matrix.get("pool_top_n") != 20
        or set(matrix.get("contenders", [])) != {WINNER_ID, BASELINE_ID}
    ):
        raise GateDBlindError("blind judging matrix is not the frozen two-bundle 800-query matrix")
    matrix_binding = {
        "authorized_query_manifest_fingerprint": matrix.get(
            "authorized_query_manifest_fingerprint"
        ),
        "blind_manifest_receipt_fingerprint": matrix.get("blind_manifest_receipt_fingerprint"),
        "blind_manifest_file_sha256": matrix.get("blind_manifest_file_sha256"),
    }
    if any(not isinstance(value, str) or not value for value in matrix_binding.values()):
        raise GateDBlindError("blind judging matrix lacks complete query custody binding")
    if matrix_binding["blind_manifest_receipt_fingerprint"] != receipt.get(
        "artifact_fingerprint"
    ) or matrix_binding["blind_manifest_file_sha256"] != receipt.get("file_sha256"):
        raise GateDBlindError("blind judging matrix is bound to a different manifest receipt")
    if matrix.get("corpus_root_hash") != spec.get("corpus_snapshot_hash"):
        raise GateDBlindError("blind judging matrix is bound to a different corpus")
    for bundle in bundles.values():
        if any(bundle.get(field) != value for field, value in matrix_binding.items()):
            raise GateDBlindError("blind retrieval bundle is not bound to the judging matrix")
    query_ids = {q["id"] for q in queries}
    if set(matrix.get("pools", {})) != query_ids:
        raise GateDBlindError("blind judging matrix does not reconstruct query coverage")
    relevance, merged_fp, unresolved = load_final_labels_from_value(merged, matrix)
    rows = validate_bundle_rankings(bundles, queries, matrix)
    candidate, baseline = rows[WINNER_ID], rows[BASELINE_ID]
    candidate_metrics, candidate_denoms = compute_metrics(queries, relevance, candidate)
    baseline_metrics, baseline_denoms = compute_metrics(queries, relevance, baseline)
    family_candidate, family_baseline = (
        _family_ndcg(queries, relevance, candidate),
        _family_ndcg(queries, relevance, baseline),
    )
    point, low, high = cluster_bootstrap_delta_ci(
        family_candidate, family_baseline, seed=spec["seeds"]["bootstrap"]
    )
    raw_tests = [
        _mcnemar(name, queries, relevance, candidate, baseline)
        for name in spec["holm_comparison_family"]
    ]
    adjusted = holm_adjust([test["raw_p"] for test in raw_tests])
    holm = [{**test, "adjusted_p": adjusted[index]} for index, test in enumerate(raw_tests)]
    accuracy = {
        "ndcg_at_10": candidate_metrics["macro_positive_ndcg_at_10"]
        - baseline_metrics["macro_positive_ndcg_at_10"],
        "grade2_recall_at_20": candidate_metrics["grade2_recall_at_20"]
        - baseline_metrics["grade2_recall_at_20"],
        "same_specific_fact_grade2_top1": candidate_metrics["same_specific_fact_grade2_top1"]
        - baseline_metrics["same_specific_fact_grade2_top1"],
    }
    safety = {
        "exact": baseline_metrics["exact_safety"] - candidate_metrics["exact_safety"],
        "keyword": baseline_metrics["keyword_safety"] - candidate_metrics["keyword_safety"],
        "strict_negative": baseline_metrics["strict_negative_safety"]
        - candidate_metrics["strict_negative_safety"],
    }
    metrics = sign_artifact(
        "BlindMetrics",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "required_metrics": list(REQUIRED_METRICS),
            "candidate_metrics": candidate_metrics,
            "baseline_metrics": baseline_metrics,
            "denominators": {"candidate": candidate_denoms, "baseline": baseline_denoms},
            "blind_query_ids": sorted(query_ids),
            "paired_statistics": {"ndcg_delta_ci95": [low, high], "holm_results": holm},
        },
    )
    evidence = {
        "development_winner": winner["artifact_fingerprint"],
        "blind_unlock": unlock["artifact_fingerprint"],
        "blind_manifest_receipt": receipt["artifact_fingerprint"],
        "authorized_query_manifest_fingerprint": matrix_binding[
            "authorized_query_manifest_fingerprint"
        ],
        "blind_manifest_receipt_fingerprint": matrix_binding["blind_manifest_receipt_fingerprint"],
        "blind_manifest_file_sha256": matrix_binding["blind_manifest_file_sha256"],
        "judging_matrix": matrix["artifact_fingerprint"],
        "winner_bundle": bundles[WINNER_ID]["artifact_fingerprint"],
        "baseline_bundle": bundles[BASELINE_ID]["artifact_fingerprint"],
        "raw_judgments": fingerprint(dict(sorted(raw_fingerprints.items()))),
        "merged_labels": merged_fp,
        "metrics_artifact": metrics["artifact_fingerprint"],
        "blind_query_ids": query_ids_fingerprint,
    }
    blind = sign_artifact(
        "BlindEvaluation",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "candidate_contender_id": WINNER_ID,
            "baseline_contender_id": BASELINE_ID,
            "complete_query_count": 800,
            "blind_query_ids": sorted(query_ids),
            "candidate_metrics": candidate_metrics,
            "baseline_metrics": baseline_metrics,
            "accuracy_deltas": accuracy,
            "safety_deltas": safety,
            "confidence_intervals": {"ndcg_delta_ci95": [low, high]},
            "holm_results": holm,
            "configuration_mutated_after_first_result": False,
            "fixed_configuration_attestation": {
                "frozen": True,
                "configuration_hash": spec["configuration_hash"],
                "winner_pipeline_fingerprint": fingerprint(winner["pipeline"]),
                "post_first_result_mutations": [],
            },
            "unresolved_disagreements": unresolved,
            "evidence_fingerprints": evidence,
        },
    )
    return metrics, blind


def load_final_labels_from_value(value: Mapping[str, Any], matrix: Mapping[str, Any]):
    # Reuse the development parser's coverage/pool/elevation safeguards without reading a path.
    try:
        verify_artifact_fingerprint(dict(value))
    except ValueError as exc:
        raise GateDBlindError(str(exc)) from exc
    relevance, unresolved = {}, 0
    for label in value.get("labels", []):
        if not isinstance(label, dict) or label.get("final_grade") not in (0, 1, 2):
            raise GateDBlindError("merged blind label is malformed")
        q, c = label.get("query_id"), label.get("candidate_id")
        if not isinstance(q, str) or not isinstance(c, str) or c in relevance.setdefault(q, {}):
            raise GateDBlindError("merged blind labels have duplicate or malformed coverage")
        relevance[q][c] = label["final_grade"]
        if label.get("escalated") and label.get("arbitrated_grade") not in (0, 1, 2):
            unresolved += 1
    if set(relevance) != set(matrix["pools"]) or any(
        set(relevance[q]) != set(pool) for q, pool in matrix["pools"].items()
    ):
        raise GateDBlindError("merged blind labels do not exactly match matrix pools")
    if unresolved:
        raise GateDBlindError("merged blind labels have unresolved adjudications")
    return relevance, value["fingerprint"], unresolved


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in (
        "spec",
        "winner",
        "unlock",
        "manifest-receipt",
        "queries-blind",
        "blind-vault-dir",
        "matrix",
        "merged-labels",
    ):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--bundles", type=Path, nargs=2, required=True)
    p.add_argument("--raw-labels", type=Path, nargs=3, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    a = p.parse_args(argv)
    spec, winner, unlock, receipt = (
        _read(a.spec),
        _read(a.winner),
        _read(a.unlock),
        _read(a.manifest_receipt),
    )
    queries, ids_fp = load_authorized_blind_queries(
        queries_path=a.queries_blind,
        vault_dir=a.blind_vault_dir,
        spec_path=a.spec,
        winner_path=a.winner,
        unlock_path=a.unlock,
        receipt_path=a.manifest_receipt,
    )
    merged, matrix = (
        _read(a.merged_labels),
        validate_signed_artifact(_read(a.matrix), kind="JudgingMatrix"),
    )
    raw = _raw_labels(a.raw_labels, merged)
    bundles = _load_two_bundles(a.bundles, spec, winner)
    metrics, blind = evaluate(
        spec=spec,
        winner=winner,
        unlock=unlock,
        receipt=receipt,
        queries=queries,
        query_ids_fingerprint=ids_fp,
        matrix=matrix,
        merged=merged,
        raw_fingerprints=raw,
        bundles=bundles,
    )
    decision = build_promotion_decision(spec, blind, winner, unlock, receipt)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("blind_metrics.json", metrics),
        ("blind_evaluation.json", blind),
        ("promotion_decision.json", decision),
    ):
        (a.out_dir / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateDBlindError, GateDDevelopmentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
