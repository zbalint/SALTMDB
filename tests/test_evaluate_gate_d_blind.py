"""Synthetic contract tests for the post-unlock Gate-D blind evaluator."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import bakeoff_state as bs  # noqa: E402
import evaluate_gate_d_blind as blind  # noqa: E402
from merge_judgments import JUDGES, artifact_fingerprint  # noqa: E402


FACETS = (
    "exact_sentence", "keyword", "typo", "short_memory", "long_body",
    "current_vs_superseded", "close_sibling", "multilingual", "strict_negative",
)
COMPARISONS = [
    "winner_vs_baseline_same_specific_fact",
    "winner_vs_baseline_ndcg_at_10",
    "winner_vs_baseline_grade2_recall_at_20",
]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _spec() -> dict[str, Any]:
    facet_targets = {facet: 1 for facet in FACETS}
    facet_targets["strict_negative"] = 792
    dev_facet_targets = {facet: 1 for facet in FACETS}
    dev_facet_targets["strict_negative"] = 392
    return bs.sign_artifact(
        "BakeoffSpec",
        {
            "run_id": "synthetic-gate-d-blind",
            "commit": "a" * 64,
            "corpus_snapshot_hash": "b" * 64,
            "query_slots_hash": "c" * 64,
            "query_prompt_hash": "d" * 64,
            "rubric_hash": "e" * 64,
            "configuration_hash": "f" * 64,
            "seeds": {"bootstrap": 7},
            "machine_fingerprint": "1" * 64,
            "contenders": [blind.WINNER_ID, blind.BASELINE_ID],
            "hyperparameter_grids": {"retrieval": ["frozen"]},
            "required_metrics": list(bs.REQUIRED_METRICS),
            "software_versions": {"python": "synthetic"},
            "holm_comparison_family": list(COMPARISONS),
            "split_targets": {"dev": 400, "blind": 800},
            "facet_targets": {"dev": dev_facet_targets, "blind": facet_targets},
        },
    )


def _winner(spec: dict[str, Any]) -> dict[str, Any]:
    metrics = {
        name: (0 if name == "benchmark_failures" else 1.0)
        for name in bs.REQUIRED_METRICS
    }
    metrics["warm_latency_p50_seconds"] = 0.01
    metrics["warm_latency_p95_seconds"] = 0.02
    return bs.sign_artifact(
        "DevelopmentWinner",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "pipeline": {"contender_id": blind.WINNER_ID, "configuration": "frozen"},
            "development_metrics": metrics,
            "upstream_fingerprints": {"synthetic": "2" * 64},
            "development_query_ids": [f"dev-{i:04d}" for i in range(400)],
        },
    )


def _queries() -> list[dict[str, Any]]:
    queries = []
    for index in range(800):
        facet = FACETS[index % len(FACETS)]
        queries.append(
            {
                "id": f"blind-{index:04d}",
                "split": "blind",
                "topic_family_id": f"family-{index % 8}",
                "facet": facet,
                "source_entity_ids": [] if facet == "strict_negative" else ["good"],
            }
        )
    return queries


def _matrix(spec: dict[str, Any], queries: list[dict[str, Any]]) -> dict[str, Any]:
    return bs.sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": spec["artifact_fingerprint"],
            "corpus_root_hash": "3" * 64,
            "contenders": [blind.BASELINE_ID, blind.WINNER_ID],
            "query_count": 800,
            "pool_top_n": 20,
            "pools": {
                query["id"]: {
                    **({"good": {"title": "Good", "full_content": "good", "ground_truth_forced_include": True}}
                       if query["facet"] != "strict_negative" else {}),
                    "bad": {"title": "Bad", "full_content": "bad", "ground_truth_forced_include": False},
                }
                for query in queries
            },
        },
    )


def _bundle(spec: dict[str, Any], queries: list[dict[str, Any]], contender: str, *, winner_good: bool = True) -> dict[str, Any]:
    rows = []
    for query in queries:
        good = query["facet"] != "strict_negative" and winner_good
        entity_id = "good" if (contender == blind.WINNER_ID and good) else "bad"
        rows.append({"query_id": query["id"], "top20": [{"entity_id": entity_id}], "latency_ms": 10.0, "failure": None})
    cell = {"kind": "lexical", "channel": "bm25_plus_current_head"} if contender == blind.BASELINE_ID else {
        "kind": "late_interaction", "model_id": "answerdotai/answerai-colbert-small-v1", "channel": "entity"
    }
    return bs.sign_artifact(
        "RetrievalRunBundle",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "cell": cell,
            "complete_query_count": 800,
            "failures": [],
            "results": rows,
        },
    )


def _raw_and_merged(matrix: dict[str, Any], *, unresolved: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = []
    for judge in JUDGES:
        value = {"judge": judge, "labels": []}
        value["fingerprint"] = artifact_fingerprint(value)
        raw.append(value)
    labels = []
    for query_id, pool in matrix["pools"].items():
        for candidate_id in pool:
            escalated = unresolved and candidate_id == "good"
            labels.append(
                {
                    "query_id": query_id,
                    "candidate_id": candidate_id,
                    "final_grade": 2 if candidate_id == "good" else 0,
                    "escalated": escalated,
                    "arbitrated_grade": None if escalated else 2,
                }
            )
    merged = {"labels": labels, "raw_labels_fingerprint": artifact_fingerprint(raw)}
    merged["fingerprint"] = artifact_fingerprint(merged)
    return raw, merged


def _authorization_artifacts(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], bytes]:
    spec = _spec()
    winner = _winner(spec)
    unlock = bs.build_blind_unlock(spec, winner, user_confirmation="synthetic approval")
    payload = json.dumps({"queries": _queries()}, separators=(",", ":")).encode()
    receipt = bs.build_blind_manifest_receipt(spec, winner, unlock, _hash(payload.decode()))
    return spec, winner, unlock, receipt, payload


def test_authorized_loader_requires_800_unique_blind_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec, winner, unlock, receipt, payload = _authorization_artifacts(tmp_path)
    calls = []

    def authorize(*args: Any, **kwargs: Any) -> bytes:
        calls.append(args[0])
        return payload

    monkeypatch.setattr(blind, "authorize_blind_file", authorize)
    queries, query_fp = blind.load_authorized_blind_queries(
        queries_path=tmp_path / "sealed.json", vault_dir=tmp_path,
        spec_path=tmp_path / "spec.json", winner_path=tmp_path / "winner.json",
        unlock_path=tmp_path / "unlock.json", receipt_path=tmp_path / "receipt.json",
    )
    assert len(queries) == 800 and len({q["id"] for q in queries}) == 800
    assert query_fp == bs.fingerprint(sorted(q["id"] for q in queries))
    assert calls == [tmp_path / "sealed.json"]

    malformed = json.loads(payload)
    malformed["queries"][-1]["id"] = malformed["queries"][0]["id"]
    monkeypatch.setattr(blind, "authorize_blind_file", lambda *a, **k: json.dumps(malformed).encode())
    with pytest.raises(blind.GateDBlindError, match="malformed query metadata"):
        blind.load_authorized_blind_queries(
            queries_path=tmp_path / "sealed.json", vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json", winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json", receipt_path=tmp_path / "receipt.json",
        )


def test_loader_rejects_wrong_count_and_non_blind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec, winner, unlock, receipt, payload = _authorization_artifacts(tmp_path)
    del spec, winner, unlock, receipt
    value = json.loads(payload)
    replacement = value["queries"][:-1]
    monkeypatch.setattr(blind, "authorize_blind_file", lambda *a, **k: json.dumps({"queries": replacement}).encode())
    with pytest.raises(blind.GateDBlindError, match="exactly 800"):
        blind.load_authorized_blind_queries(
            queries_path=tmp_path / "sealed.json", vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json", winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json", receipt_path=tmp_path / "receipt.json",
        )
    value["queries"][0]["split"] = "dev"
    monkeypatch.setattr(blind, "authorize_blind_file", lambda *a, **k: json.dumps(value).encode())
    with pytest.raises(blind.GateDBlindError, match="non-blind"):
        blind.load_authorized_blind_queries(
            queries_path=tmp_path / "sealed.json", vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json", winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json", receipt_path=tmp_path / "receipt.json",
        )


def test_two_bundle_loader_rejects_stale_duplicate_and_extra(tmp_path: Path) -> None:
    spec, winner, *_ = _authorization_artifacts(tmp_path)
    queries = _queries()
    winner_bundle = _bundle(spec, queries, blind.WINNER_ID)
    baseline_bundle = _bundle(spec, queries, blind.BASELINE_ID)
    # The loader consumes paths, so write only synthetic signed bundle bytes.
    (tmp_path / "winner.json").write_text(json.dumps(winner_bundle))
    (tmp_path / "baseline.json").write_text(json.dumps(baseline_bundle))
    assert set(blind._load_two_bundles([tmp_path / "winner.json", tmp_path / "baseline.json"], spec, winner)) == {blind.WINNER_ID, blind.BASELINE_ID}
    stale = dict(baseline_bundle)
    stale["spec_fingerprint"] = "4" * 64
    stale["artifact_fingerprint"] = bs.fingerprint({k: v for k, v in stale.items() if k != "artifact_fingerprint"})
    (tmp_path / "stale.json").write_text(json.dumps(stale))
    with pytest.raises(blind.GateDBlindError, match="stale"):
        blind._load_two_bundles([tmp_path / "winner.json", tmp_path / "stale.json"], spec, winner)
    with pytest.raises(blind.GateDBlindError, match="exactly"):
        blind._load_two_bundles([tmp_path / "winner.json"], spec, winner)


def test_raw_and_merged_coverage_and_arbitration_fail_closed(tmp_path: Path) -> None:
    spec, *_ = _authorization_artifacts(tmp_path)
    queries = _queries()
    matrix = _matrix(spec, queries)
    raw, merged = _raw_and_merged(matrix)
    paths = []
    for index, artifact in enumerate(raw):
        path = tmp_path / f"raw-{index}.json"
        path.write_text(json.dumps(artifact))
        paths.append(path)
    assert set(blind._raw_labels(paths, merged)) == set(JUDGES)
    duplicate = dict(raw[0])
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate))
    with pytest.raises(blind.GateDBlindError, match="three-judge"):
        blind._raw_labels([paths[0], paths[1], duplicate_path], merged)
    incomplete = {**merged, "labels": merged["labels"][:-1]}
    incomplete["fingerprint"] = artifact_fingerprint({k: v for k, v in incomplete.items() if k != "fingerprint"})
    with pytest.raises(blind.GateDBlindError, match="exactly match"):
        blind.load_final_labels_from_value(incomplete, matrix)
    _, unresolved = _raw_and_merged(matrix, unresolved=True)
    with pytest.raises(blind.GateDBlindError, match="unresolved"):
        blind.load_final_labels_from_value(unresolved, matrix)


def test_holm_has_three_frozen_comparisons_and_unknown_fails_closed() -> None:
    queries = [{"id": "q", "source_entity_ids": ["good"], "topic_family_id": "f"}]
    relevance = {"q": {"good": 2, "bad": 0}}
    rows = {"q": {"top20": [{"entity_id": "good"}]}}
    for name in COMPARISONS:
        result = blind._mcnemar(name, queries, relevance, rows, {"q": {"top20": [{"entity_id": "bad"}]}})
        assert result["comparison"] == name and 0 <= result["raw_p"] <= 1
    with pytest.raises(blind.GateDBlindError, match="unsupported frozen Holm"):
        blind._mcnemar("unknown", queries, relevance, rows, rows)


def test_evaluate_emits_signed_evidence_and_both_promotion_outcomes(tmp_path: Path) -> None:
    spec, winner, unlock, receipt, _ = _authorization_artifacts(tmp_path)
    queries = _queries()
    matrix = _matrix(spec, queries)
    raw, merged = _raw_and_merged(matrix)
    bundles = {
        blind.WINNER_ID: _bundle(spec, queries, blind.WINNER_ID),
        blind.BASELINE_ID: _bundle(spec, queries, blind.BASELINE_ID),
    }
    raw_fps = {item["judge"]: item["fingerprint"] for item in raw}
    metrics, evaluation = blind.evaluate(
        spec=spec, winner=winner, unlock=unlock, receipt=receipt,
        queries=queries, query_ids_fingerprint=bs.fingerprint(sorted(q["id"] for q in queries)),
        matrix=matrix, merged=merged, raw_fingerprints=raw_fps, bundles=bundles,
    )
    assert bs.validate_signed_artifact(metrics, kind="BlindMetrics")["artifact_fingerprint"]
    assert bs.validate_blind_evaluation(evaluation, spec)["unresolved_disagreements"] == 0
    promoted = bs.build_promotion_decision(spec, evaluation, winner, unlock, receipt)
    assert promoted["promotion"] is True

    poor_bundles = dict(bundles)
    poor_bundles[blind.WINNER_ID] = _bundle(spec, queries, blind.WINNER_ID, winner_good=False)
    _, retained_evaluation = blind.evaluate(
        spec=spec, winner=winner, unlock=unlock, receipt=receipt,
        queries=queries, query_ids_fingerprint=bs.fingerprint(sorted(q["id"] for q in queries)),
        matrix=matrix, merged=merged, raw_fingerprints=raw_fps, bundles=poor_bundles,
    )
    retained = bs.build_promotion_decision(spec, retained_evaluation, winner, unlock, receipt)
    assert retained["promotion"] is False
