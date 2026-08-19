"""No-download orchestration tests for the signed bakeoff state machine."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from bakeoff_orchestrator import (  # noqa: E402
    BakeoffOrchestrator,
    OrchestrationError,
    _read_control_artifact,
    main,
)
from bakeoff_state import (  # noqa: E402
    MANDATORY_FACETS,
    REQUIRED_METRICS,
    RunState,
    build_blind_manifest_receipt,
    build_blind_unlock,
    sign_artifact,
    fingerprint,
)


def _hash(label: str) -> str:
    return fingerprint(label)


def _facets(total: int) -> dict[str, int]:
    names = sorted(MANDATORY_FACETS)
    base, remainder = divmod(total, len(names))
    return {name: base + int(index < remainder) for index, name in enumerate(names)}


def _spec() -> dict:
    return sign_artifact(
        "BakeoffSpec",
        {
            "run_id": "orchestrator-test",
            "commit": _hash("commit"),
            "corpus_snapshot_hash": _hash("corpus"),
            "query_slots_hash": _hash("slots"),
            "query_prompt_hash": _hash("prompt"),
            "rubric_hash": _hash("rubric"),
            "configuration_hash": _hash("configuration"),
            "seeds": {"split": 7, "bootstrap": 11},
            "machine_fingerprint": _hash("machine"),
            "contenders": ["baseline", "candidate"],
            "hyperparameter_grids": {"fusion": [0.25, 0.5, 0.75]},
            "required_metrics": list(REQUIRED_METRICS),
            "software_versions": {"python": "3.12"},
            "holm_comparison_family": ["winner_vs_baseline_same_specific_fact"],
            "split_targets": {"dev": 400, "blind": 800},
            "facet_targets": {"dev": _facets(400), "blind": _facets(800)},
        },
    )


def _metrics(*, failures: int = 0, p95: float = 0.5) -> dict:
    return {
        "macro_positive_ndcg_at_10": 0.7,
        "grade2_recall_at_20": 0.8,
        "same_specific_fact_grade2_top1": 0.6,
        "exact_safety": 0.99,
        "keyword_safety": 0.98,
        "strict_negative_safety": 0.97,
        "warm_latency_p50_seconds": 0.2,
        "warm_latency_p95_seconds": p95,
        "benchmark_failures": failures,
    }


def _winner(spec: dict) -> dict:
    return sign_artifact(
        "DevelopmentWinner",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "pipeline": {"contender_id": "candidate", "retrieval": "candidate"},
            "development_metrics": _metrics(),
            "upstream_fingerprints": {"retrieval": _hash("retrieval")},
            "development_query_ids": [f"dev-{index:03d}" for index in range(400)],
        },
    )


def _generic(kind: str, spec: dict, **extra: object) -> dict:
    required: dict[str, object] = {
        "IndexBuildReceipt": {"coverage_complete": True, "failures": []},
        "RetrievalRunBundle": {"complete_query_count": 400, "failures": []},
        "DevelopmentJudgments": {
            "judge_artifact_count": 3,
            "unresolved_disagreements": 0,
        },
        "BlindEvaluation": {
            "complete_query_count": 800,
            "configuration_mutated_after_first_result": False,
        },
    }.get(kind, {})
    return sign_artifact(
        kind,
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            **required,
            **extra,
        },
    )


def _blind_evaluation(
    spec: dict, winner: dict, unlock: dict, receipt: dict, *, passes: bool
) -> dict:
    baseline_metrics = _metrics()
    candidate_metrics = _metrics(failures=0 if passes else 1)
    if passes:
        candidate_metrics.update(
            {
                "macro_positive_ndcg_at_10": 0.8,
                "grade2_recall_at_20": 0.9,
                "same_specific_fact_grade2_top1": 0.7,
                "exact_safety": 0.99,
                "keyword_safety": 0.98,
                "strict_negative_safety": 0.97,
            }
        )
    return sign_artifact(
        "BlindEvaluation",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "candidate_contender_id": "candidate",
            "baseline_contender_id": "lexical:bm25",
            "complete_query_count": 800,
            "blind_query_ids": [f"blind-{index:03d}" for index in range(800)],
            "candidate_metrics": candidate_metrics,
            "baseline_metrics": baseline_metrics,
            "accuracy_deltas": {
                "ndcg_at_10": candidate_metrics["macro_positive_ndcg_at_10"]
                - baseline_metrics["macro_positive_ndcg_at_10"],
                "grade2_recall_at_20": candidate_metrics["grade2_recall_at_20"]
                - baseline_metrics["grade2_recall_at_20"],
                "same_specific_fact_grade2_top1": candidate_metrics[
                    "same_specific_fact_grade2_top1"
                ]
                - baseline_metrics["same_specific_fact_grade2_top1"],
            },
            "safety_deltas": {
                "exact": baseline_metrics["exact_safety"] - candidate_metrics["exact_safety"],
                "keyword": baseline_metrics["keyword_safety"] - candidate_metrics["keyword_safety"],
                "strict_negative": baseline_metrics["strict_negative_safety"]
                - candidate_metrics["strict_negative_safety"],
            },
            "confidence_intervals": {"ndcg_delta_ci95": [0.01 if passes else -0.01, 0.2]},
            "holm_results": [
                {
                    "comparison": "winner_vs_baseline_same_specific_fact",
                    "adjusted_p": 0.01 if passes else 0.5,
                }
            ],
            "configuration_mutated_after_first_result": False,
            "fixed_configuration_attestation": {
                "frozen": True,
                "configuration_hash": spec["configuration_hash"],
                "winner_pipeline_fingerprint": fingerprint(winner["pipeline"]),
                "post_first_result_mutations": [],
            },
            "unresolved_disagreements": 0,
            "evidence_fingerprints": {
                "development_winner": winner["artifact_fingerprint"],
                "blind_unlock": unlock["artifact_fingerprint"],
                "blind_manifest_receipt": receipt["artifact_fingerprint"],
                "judging_matrix": _hash("judging-matrix"),
                "winner_bundle": _hash("winner-bundle"),
                "baseline_bundle": _hash("baseline-bundle"),
                "raw_judgments": _hash("raw-judgments"),
                "merged_labels": _hash("merged-labels"),
                "metrics_artifact": _hash("metrics-artifact"),
                "blind_query_ids": fingerprint(
                    sorted([f"blind-{index:03d}" for index in range(800)])
                ),
            },
        },
    )


def _decision(
    spec: dict,
    evaluation: dict,
    winner: dict,
    unlock: dict,
    receipt: dict,
    *,
    promotion: bool = False,
) -> dict:
    return sign_artifact(
        "PromotionDecision",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "blind_metrics": evaluation["candidate_metrics"],
            "accuracy_deltas": evaluation["accuracy_deltas"],
            "confidence_intervals": evaluation["confidence_intervals"],
            "holm_results": evaluation["holm_results"],
            "safety_deltas": evaluation["safety_deltas"],
            "failures": []
            if evaluation["candidate_metrics"]["benchmark_failures"] == 0
            else ["candidate_benchmark_failures"],
            "latency": {"candidate_warm_p95_seconds": 0.5},
            "promotion": promotion,
            "evidence_fingerprints": {
                "development_winner": winner["artifact_fingerprint"],
                "blind_evaluation": evaluation["artifact_fingerprint"],
                "blind_unlock": unlock["artifact_fingerprint"],
                "blind_manifest_receipt": receipt["artifact_fingerprint"],
            },
        },
    )


def _write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _initialize(tmp_path: Path) -> tuple[BakeoffOrchestrator, dict, Path]:
    spec = _spec()
    spec_path = _write(tmp_path / "spec.json", spec)
    orchestrator = BakeoffOrchestrator(tmp_path / "run")
    orchestrator.initialize(spec_path)
    return orchestrator, spec, spec_path


def _advance_to_winner(orchestrator: BakeoffOrchestrator, spec: dict) -> dict:
    evidence = {
        RunState.DEV_INDEXED: _generic(
            "IndexBuildReceipt", spec, coverage_complete=True, failures=[]
        ),
        RunState.DEV_RETRIEVED: _generic(
            "RetrievalRunBundle", spec, complete_query_count=400, failures=[]
        ),
        RunState.DEV_JUDGED: _generic(
            "DevelopmentJudgments",
            spec,
            judge_artifact_count=3,
            unresolved_disagreements=0,
        ),
    }
    for state, artifact in evidence.items():
        orchestrator.advance(state, artifact)
    winner = _winner(spec)
    orchestrator.advance(RunState.DEV_WINNER_SIGNED, winner)
    return winner


def test_initialize_and_advance_record_chain_auditable_receipts(tmp_path):
    orchestrator, spec, _ = _initialize(tmp_path)
    winner = _advance_to_winner(orchestrator, spec)
    assert winner["kind"] == "DevelopmentWinner"
    receipts = orchestrator.verify_receipt_chain()
    assert [item["sequence"] for item in receipts] == list(range(5))
    assert receipts[0]["state"] == RunState.SPEC_FROZEN.value
    assert receipts[-1]["state"] == RunState.DEV_WINNER_SIGNED.value
    assert receipts[-1]["previous_receipt_fingerprint"] == receipts[-2]["artifact_fingerprint"]
    index = json.loads(orchestrator.index_path.read_text(encoding="utf-8"))
    assert index["last_receipt_fingerprint"] == receipts[-1]["artifact_fingerprint"]


def test_tampered_and_stale_evidence_fail_before_transition(tmp_path):
    orchestrator, spec, _ = _initialize(tmp_path)
    stale = _generic("IndexBuildReceipt", {**spec, "run_id": "other-run"})
    with pytest.raises(OrchestrationError, match="different bakeoff run"):
        orchestrator.advance(RunState.DEV_INDEXED, stale)

    tampered = _generic("IndexBuildReceipt", spec)
    tampered["coverage"] = 1.0
    with pytest.raises(OrchestrationError, match="fingerprint"):
        orchestrator.advance(RunState.DEV_INDEXED, tampered)
    assert orchestrator.verify_receipt_chain()[0]["state"] == RunState.SPEC_FROZEN.value


def test_invalid_transition_and_terminal_restart_are_rejected(tmp_path):
    orchestrator, spec, _ = _initialize(tmp_path)
    with pytest.raises(OrchestrationError, match="invalid transition"):
        orchestrator.advance(RunState.DEV_RETRIEVED, _generic("RetrievalRunBundle", spec))

    winner = _advance_to_winner(orchestrator, spec)
    unlock = _generic("BlindUnlock", spec)
    # The exact unlock validator rejects the placeholder before any state change.
    with pytest.raises(Exception):
        orchestrator.advance(RunState.BLIND_UNLOCKED, unlock, winner=winner)

    # Use a valid winner-bound unlock, then complete and retain the run.
    from bakeoff_state import build_blind_unlock  # local import keeps helper intent explicit

    unlock = build_blind_unlock(
        spec, winner, user_confirmation="synthetic Gate D test confirmation"
    )
    orchestrator.advance(RunState.BLIND_UNLOCKED, unlock, winner=winner)
    receipt = build_blind_manifest_receipt(spec, winner, unlock, _hash("blind-bytes"))
    evaluation = _blind_evaluation(spec, winner, unlock, receipt, passes=False)
    orchestrator.advance(
        RunState.BLIND_COMPLETE,
        evaluation,
    )
    decision = _decision(spec, evaluation, winner, unlock, receipt, promotion=False)
    orchestrator.advance(
        RunState.RETAINED,
        {
            "evidence": decision,
            "winner": winner,
            "blind_evaluation": evaluation,
            "unlock": unlock,
            "manifest_receipt": receipt,
        },
    )
    with pytest.raises(OrchestrationError, match="(invalid transition|terminal)"):
        orchestrator.advance(RunState.PROMOTED, decision)


def test_receipt_chain_detects_tampering(tmp_path):
    orchestrator, spec, _ = _initialize(tmp_path)
    _advance_to_winner(orchestrator, spec)
    receipts = orchestrator.verify_receipt_chain()
    receipt_path = orchestrator.receipts_dir / orchestrator._load_receipt_index()["receipts"][2]
    tampered = dict(receipts[2])
    tampered["state"] = "TAMPERED"
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(OrchestrationError, match="fingerprint"):
        orchestrator.verify_receipt_chain()


def test_blind_query_path_is_rejected_before_first_read(tmp_path):
    blind_path = tmp_path / "queries_blind.json"
    with mock.patch.object(
        Path, "read_text", side_effect=AssertionError("blind file opened")
    ) as read:
        with pytest.raises(OrchestrationError, match="blind query"):
            _read_control_artifact(blind_path)
        read.assert_not_called()


def test_cli_prints_only_metadata_for_init(tmp_path, capsys):
    spec_path = _write(tmp_path / "spec.json", _spec())
    assert main(["init", "--run-dir", str(tmp_path / "run"), "--spec", str(spec_path)]) == 0
    output = capsys.readouterr().out
    assert "SPEC_FROZEN" in output
    assert "query_slots_hash" not in output


@pytest.mark.parametrize("promotion", [True, False])
def test_orchestrator_blind_complete_reaches_promoted_or_retained(tmp_path, promotion):
    orchestrator, spec, _ = _initialize(tmp_path)
    winner = _advance_to_winner(orchestrator, spec)
    unlock = build_blind_unlock(
        spec, winner, user_confirmation="synthetic Gate D test confirmation"
    )
    orchestrator.advance(RunState.BLIND_UNLOCKED, unlock, winner=winner)
    receipt = build_blind_manifest_receipt(spec, winner, unlock, _hash("blind-bytes"))
    evaluation = _blind_evaluation(spec, winner, unlock, receipt, passes=promotion)
    orchestrator.advance(
        RunState.BLIND_COMPLETE,
        evaluation,
    )
    decision = _decision(spec, evaluation, winner, unlock, receipt, promotion=promotion)
    target = RunState.PROMOTED if promotion else RunState.RETAINED
    orchestrator.advance(
        target,
        {
            "evidence": decision,
            "winner": winner,
            "blind_evaluation": evaluation,
            "unlock": unlock,
            "manifest_receipt": receipt,
        },
    )
    assert orchestrator.verify_receipt_chain()[-1]["state"] == target.value


def test_orchestrator_terminal_rejects_missing_or_mismatched_custody(tmp_path):
    orchestrator, spec, _ = _initialize(tmp_path)
    winner = _advance_to_winner(orchestrator, spec)
    unlock = build_blind_unlock(
        spec, winner, user_confirmation="synthetic Gate D test confirmation"
    )
    orchestrator.advance(RunState.BLIND_UNLOCKED, unlock, winner=winner)
    receipt = build_blind_manifest_receipt(spec, winner, unlock, _hash("blind-bytes"))
    evaluation = _blind_evaluation(spec, winner, unlock, receipt, passes=False)
    orchestrator.advance(
        RunState.BLIND_COMPLETE,
        evaluation,
    )
    decision = _decision(spec, evaluation, winner, unlock, receipt, promotion=False)
    with pytest.raises(OrchestrationError, match="complete custody evidence"):
        orchestrator.advance(RunState.RETAINED, {"evidence": decision, "winner": winner})
    mismatched = sign_artifact(
        "PromotionDecision",
        {
            **decision,
            "evidence_fingerprints": {
                **decision["evidence_fingerprints"],
                "blind_unlock": _hash("wrong-unlock"),
            },
        },
    )
    with pytest.raises(OrchestrationError, match="does not bind"):
        orchestrator.advance(
            RunState.RETAINED,
            {
                "evidence": mismatched,
                "winner": winner,
                "blind_evaluation": evaluation,
                "unlock": unlock,
                "manifest_receipt": receipt,
            },
        )
