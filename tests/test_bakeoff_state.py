import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from bakeoff_state import (  # noqa: E402
    BlindAccessError,
    BlindVault,
    BakeoffContractError,
    BakeoffStateMachine,
    MANDATORY_FACETS,
    REQUIRED_METRICS,
    RunState,
    authorize_blind_file,
    build_blind_unlock,
    fingerprint,
    sign_artifact,
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_development_winner,
    validate_model_lock,
    validate_query_slot_assignments,
)


def _hash(label: str) -> str:
    return fingerprint(label)


def spec(slots=None):
    slots = slots or [{"slot_id": "b1", "topic_family_id": "blind-family"}]
    return sign_artifact(
        "BakeoffSpec",
        {
            "run_id": "accuracy-20260812",
            "commit": _hash("commit"),
            "corpus_snapshot_hash": _hash("corpus"),
            "query_slots_hash": fingerprint(slots),
            "query_prompt_hash": _hash("prompt"),
            "rubric_hash": _hash("rubric"),
            "configuration_hash": _hash("config"),
            "seeds": {"split": 7, "bootstrap": 11},
            "machine_fingerprint": _hash("machine"),
            "contenders": ["baseline", "candidate"],
            "hyperparameter_grids": {"fusion": [0.25, 0.5, 0.75]},
            "required_metrics": list(REQUIRED_METRICS),
            "software_versions": {"python": "3.12"},
            "holm_comparison_family": ["winner_vs_baseline_same_specific_fact"],
            "split_targets": {"dev": 400, "blind": 800},
            "facet_targets": {
                split: {
                    facet: total // len(MANDATORY_FACETS)
                    + (index < total % len(MANDATORY_FACETS))
                    for index, facet in enumerate(sorted(MANDATORY_FACETS))
                }
                for split, total in (("dev", 400), ("blind", 800))
            },
        },
    )


def metrics():
    return {
        "macro_positive_ndcg_at_10": 0.7,
        "grade2_recall_at_20": 0.8,
        "same_specific_fact_grade2_top1": 0.6,
        "exact_safety": 0.99,
        "keyword_safety": 0.98,
        "strict_negative_safety": 0.97,
        "warm_latency_p50_seconds": 0.2,
        "warm_latency_p95_seconds": 0.5,
        "benchmark_failures": 0,
    }


def winner(frozen_spec):
    return sign_artifact(
        "DevelopmentWinner",
        {
            "run_id": frozen_spec["run_id"],
            "spec_fingerprint": frozen_spec["artifact_fingerprint"],
            "pipeline": {
                "contender_id": "candidate",
                "retrieval": "candidate",
                "ranker": "linear",
                "ce": None,
            },
            "development_metrics": metrics(),
            "upstream_fingerprints": {"retrieval": _hash("retrieval")},
            "development_query_ids": [f"dev-{index:03d}" for index in range(400)],
        },
    )


def test_spec_requires_registered_metric_vector_and_nonzero_facets():
    frozen = spec()
    validate_bakeoff_spec(frozen)
    malformed = dict(frozen)
    malformed["required_metrics"] = ["accuracy"]
    malformed = sign_artifact("BakeoffSpec", malformed)
    with pytest.raises(BakeoffContractError, match="metric vector"):
        validate_bakeoff_spec(malformed)

    missing_facet = dict(frozen)
    missing_facet["facet_targets"] = {
        "dev": {"exact_sentence": 1},
        "blind": {facet: 1 for facet in MANDATORY_FACETS},
    }
    missing_facet = sign_artifact("BakeoffSpec", missing_facet)
    with pytest.raises(BakeoffContractError, match="mandatory facets"):
        validate_bakeoff_spec(missing_facet)

    nonfinite = metrics()
    nonfinite["grade2_recall_at_20"] = float("nan")
    malformed_winner = sign_artifact(
        "DevelopmentWinner",
        {
            **winner(frozen),
            "development_metrics": nonfinite,
        },
    )
    with pytest.raises(BakeoffContractError, match="finite"):
        validate_development_winner(malformed_winner, frozen)


def test_model_lock_rejects_mutable_revision_and_incomplete_inventory():
    base = {
        "source_repository": "BAAI/bge-small-en-v1.5",
        "resolved_revision": "a" * 40,
        "files": [{"path": "model.onnx", "sha256": _hash("model"), "size_bytes": 10}],
        "dimension": 384,
        "normalization": "l2",
        "maximum_input_tokens": 512,
        "query_prefix": "Represent this sentence: ",
        "document_prefix": "",
    }
    validate_model_lock(sign_artifact("ModelLock", base))
    with pytest.raises(BakeoffContractError, match="immutable"):
        validate_model_lock(sign_artifact("ModelLock", {**base, "resolved_revision": "main"}))
    with pytest.raises(BakeoffContractError, match="non-empty"):
        validate_model_lock(sign_artifact("ModelLock", {**base, "files": []}))


def test_corpus_manifest_binds_ordered_entities_and_chunk_hashes():
    entities = [
        {
            "entity_id": "e1",
            "title_hash": _hash("title"),
            "body_hash": _hash("body"),
            "source_hash": _hash("source"),
            "chunk_hashes": [_hash("chunk")],
        }
    ]
    payload = {
        "eligible_ids": ["e1"],
        "entities": entities,
        "representation_version": "authoritative_chunks_v1",
        "corpus_root_hash": fingerprint(
            {
                "eligible_ids": ["e1"],
                "entities": entities,
                "representation_version": "authoritative_chunks_v1",
            }
        ),
    }
    validate_corpus_manifest(sign_artifact("CorpusRepresentationManifest", payload))
    with pytest.raises(BakeoffContractError, match="root hash"):
        validate_corpus_manifest(
            sign_artifact("CorpusRepresentationManifest", {**payload, "corpus_root_hash": _hash("bad")})
        )


def test_state_machine_is_atomic_sequential_and_terminal(tmp_path):
    frozen = spec()
    machine = BakeoffStateMachine(tmp_path / "run")
    checkpoint = machine.initialize(frozen)
    assert checkpoint["state"] == "SPEC_FROZEN"
    evidence = sign_artifact("IndexBuildReceipt", {"coverage": 1.0})
    next_checkpoint = machine.transition(
        RunState.DEV_INDEXED,
        {"indexes": evidence},
        expected_spec_fingerprint=frozen["artifact_fingerprint"],
    )
    assert next_checkpoint["previous_checkpoint_fingerprint"] == checkpoint[
        "artifact_fingerprint"
    ]
    with pytest.raises(BakeoffContractError, match="invalid transition"):
        machine.transition(
            RunState.BLIND_UNLOCKED,
            {"unlock": evidence},
            expected_spec_fingerprint=frozen["artifact_fingerprint"],
        )

    evidence_kinds = {
        RunState.DEV_RETRIEVED: "RetrievalRunBundle",
        RunState.DEV_JUDGED: "DevelopmentJudgments",
        RunState.DEV_WINNER_SIGNED: "DevelopmentWinner",
        RunState.BLIND_UNLOCKED: "BlindUnlock",
        RunState.BLIND_COMPLETE: "BlindEvaluation",
        RunState.RETAINED: "PromotionDecision",
    }
    for state, kind in evidence_kinds.items():
        payload = {"state": state.value}
        if state is RunState.RETAINED:
            payload["promotion"] = False
        machine.transition(
            state,
            {"evidence": sign_artifact(kind, payload)},
            expected_spec_fingerprint=frozen["artifact_fingerprint"],
        )
    with pytest.raises(BakeoffContractError, match="terminal"):
        machine.transition(
            RunState.PROMOTED,
            {"evidence": evidence},
            expected_spec_fingerprint=frozen["artifact_fingerprint"],
        )


def test_blind_vault_does_not_read_before_matching_unlock(tmp_path):
    slots = [{"slot_id": "b1", "topic_family_id": "blind-family"}]
    frozen = spec(slots)
    selected = winner(frozen)
    validate_development_winner(selected, frozen)
    vault = BlindVault(tmp_path / "private-vault")
    vault.seal_slots(slots, frozen, custodian="codex")

    with mock.patch.object(Path, "read_text", side_effect=AssertionError("blind file opened")) as read:
        with pytest.raises(BlindAccessError):
            vault.open_slots({}, frozen, selected, custodian="codex")
        read.assert_not_called()

    unlock = build_blind_unlock(frozen, selected)
    assert vault.open_slots(unlock, frozen, selected, custodian="codex") == slots
    assert (vault.vault_dir.stat().st_mode & 0o777) == 0o700
    assert (vault.slots_path.stat().st_mode & 0o777) == 0o600


def test_blind_unlock_is_experiment_and_winner_specific(tmp_path):
    slots = [{"slot_id": "b1", "topic_family_id": "blind-family"}]
    frozen = spec(slots)
    selected = winner(frozen)
    vault = BlindVault(tmp_path / "private-vault")
    vault.seal_slots(slots, frozen, custodian="codex")
    unlock = build_blind_unlock(frozen, selected)
    other_winner = sign_artifact(
        "DevelopmentWinner", {**selected, "pipeline": {"retrieval": "different"}}
    )
    with pytest.raises((BlindAccessError, BakeoffContractError)):
        vault.open_slots(unlock, frozen, other_winner, custodian="codex")


def test_retrospective_artifact_cannot_stand_in_for_winner():
    frozen = spec()
    retrospective = sign_artifact(
        "HistoricalReplay",
        {"retrospective_only": True, "promotion_eligible": False, "accuracy": 0.99},
    )
    with pytest.raises(BakeoffContractError, match="DevelopmentWinner"):
        validate_development_winner(retrospective, frozen)


def test_query_slot_assignments_enforce_400_800_family_isolation_and_facets():
    facets = sorted(MANDATORY_FACETS)
    rows = []
    for split, total in (("dev", 400), ("blind", 800)):
        for index in range(total):
            rows.append(
                {
                    "query_id": f"{split}-q-{index:03d}",
                    "slot_id": f"{split}-s-{index:03d}",
                    "split": split,
                    "topic_family_id": f"{split}-family-{index:03d}",
                    "source_entity_ids": [] if index % 5 == 0 else [f"{split}-source-{index:03d}"],
                    "facet": facets[index % len(facets)],
                }
            )
    artifact = sign_artifact(
        "QuerySlotAssignments",
        {"assignments": rows, "generation_prompt_hash": _hash("prompt")},
    )
    validate_query_slot_assignments(artifact)
    rows[400]["topic_family_id"] = rows[0]["topic_family_id"]
    with pytest.raises(BakeoffContractError, match="topic family"):
        validate_query_slot_assignments(
            sign_artifact(
                "QuerySlotAssignments",
                {"assignments": rows, "generation_prompt_hash": _hash("prompt")},
            )
        )


def test_blind_file_authorization_checks_unlock_before_opening_target(tmp_path):
    frozen = spec()
    selected = winner(frozen)
    unlock = build_blind_unlock(frozen, selected)
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "queries_blind.json"
    target.write_text("blind material")
    spec_path = tmp_path / "spec.json"
    winner_path = tmp_path / "winner.json"
    unlock_path = tmp_path / "unlock.json"
    spec_path.write_text(json.dumps(frozen))
    winner_path.write_text(json.dumps(selected))
    unlock_path.write_text(json.dumps(unlock))
    authorize_blind_file(target, vault, spec_path, winner_path, unlock_path)
    with pytest.raises(BlindAccessError):
        authorize_blind_file(tmp_path / "public.json", vault, spec_path, winner_path, unlock_path)
