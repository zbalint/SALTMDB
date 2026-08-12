"""Tests for scripts/benchmarking/build_bakeoff_spec.py (Gate D BakeoffSpec assembly).

Fixtures are small, hand-built synthetic artifacts -- never the real frozen
``scratch/eval_results/accuracy-bakeoff-20260812`` inventory, which is exercised by the real
Gate D run recorded in the task history instead of by this unit suite.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import bakeoff_state as bs  # noqa: E402
import build_bakeoff_spec as bbs  # noqa: E402
import build_query_slots as bqs  # noqa: E402


# -------------------------------------------------------------------------------------------
# Fixture helpers
# -------------------------------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_corpus_manifest(entity_ids: tuple[str, ...] = ("ent-0001",)) -> dict[str, Any]:
    rows = [
        {
            "entity_id": entity_id,
            "title_hash": _sha(f"title-{entity_id}"),
            "body_hash": _sha(f"body-{entity_id}"),
            "source_hash": _sha(f"source-{entity_id}"),
            "chunk_hashes": [_sha(f"chunk-{entity_id}")],
        }
        for entity_id in entity_ids
    ]
    representation_version = "gate-d-test-v1"
    corpus_root_hash = bs.fingerprint(
        {
            "eligible_ids": list(entity_ids),
            "entities": rows,
            "representation_version": representation_version,
        }
    )
    return bs.sign_artifact(
        "CorpusRepresentationManifest",
        {
            "eligible_ids": list(entity_ids),
            "entities": rows,
            "representation_version": representation_version,
            "corpus_root_hash": corpus_root_hash,
        },
    )


def _build_flat_slots(facet_targets: dict[str, dict[str, int]] = bqs.FACET_TARGETS) -> list[dict[str, Any]]:
    """Build exactly 1200 minimal assigned-slot rows, one unique family/no sources per row.

    Only the fields ``build_query_slot_assignments`` reads are populated -- this is a fixture for
    exercising ``build_bakeoff_spec``'s binding logic, not a re-test of Gate B's real facet
    generator (already covered by ``test_build_query_slots.py``).
    """
    counter = 0
    slots: list[dict[str, Any]] = []
    for split in ("dev", "blind"):
        for facet, count in facet_targets[split].items():
            for _ in range(count):
                counter += 1
                slots.append(
                    {
                        "query_id": f"eval-{counter:04d}",
                        "slot_id": f"slot-{counter:04d}",
                        "split": split,
                        "topic_family_id": f"family-{counter:04d}",
                        "source_entity_ids": [],
                        "category": facet,
                    }
                )
    return slots


def _write_query_slot_fixtures(tmp_path: Path, corpus_root_hash: str) -> tuple[Path, Path]:
    """Write a valid slots-sidecar + signed QuerySlotAssignments bound to ``corpus_root_hash``."""
    from build_evaluation_queries import artifact_fingerprint as slot_fp

    slots = _build_flat_slots()
    slots_doc = {
        "schema_version": 1,
        "slots": slots,
        "corpus_root_hash": corpus_root_hash,
        "fingerprint": slot_fp(slots),
    }
    assignments = bqs.build_query_slot_assignments(slots)
    slots_path = tmp_path / "source_slots.json"
    assignments_path = tmp_path / "query_slot_assignments.json"
    slots_path.write_text(json.dumps(slots_doc, ensure_ascii=False))
    assignments_path.write_text(json.dumps(assignments, ensure_ascii=False))
    return slots_path, assignments_path


def _build_model_lock(
    *, source_repository: str = "fake-org/fake-repo", dimension: int = 8, normalization: str = "l2"
) -> dict[str, Any]:
    payload = {
        "source_repository": source_repository,
        "resolved_revision": "abc123def4567890",
        "files": [{"path": "model.onnx", "sha256": "0" * 64, "size_bytes": 10}],
        "dimension": dimension,
        "normalization": normalization,
        "maximum_input_tokens": 512,
        "query_prefix": "",
        "document_prefix": "",
    }
    return bs.sign_artifact("ModelLock", payload)


class _FakePinned:
    def __init__(self, logical_model_id: str) -> None:
        self.logical_model_id = logical_model_id


FAKE_PINNED_MODELS = (
    _FakePinned("fake/dense-a"),
    _FakePinned("fake/dense-b"),
    _FakePinned("fake/late-a"),
)


class _FakeCandidate:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _fake_candidate_by_model_id(model_id: str) -> _FakeCandidate:
    return _FakeCandidate("late_interaction" if "late" in model_id else "dense")


def _write_fake_model_locks(tmp_path: Path) -> Path:
    model_locks_dir = tmp_path / "model_locks"
    model_locks_dir.mkdir()
    for pinned in FAKE_PINNED_MODELS:
        slug = bbs.model_slug(pinned)
        lock = _build_model_lock(source_repository=f"repo-for-{slug}")
        (model_locks_dir / f"{slug}.json").write_text(json.dumps(lock, ensure_ascii=False))
    return model_locks_dir


# -------------------------------------------------------------------------------------------
# commit_hash_field
# -------------------------------------------------------------------------------------------


def test_commit_hash_field_wraps_git_sha1_into_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sha1 = "e7db121eb581bb98d774326398ccc8196afb9a63"
    monkeypatch.setattr(bbs, "git_commit_fingerprint", lambda repo_root=None: fake_sha1)
    result = bbs.commit_hash_field()
    assert result == hashlib.sha256(fake_sha1.encode("utf-8")).hexdigest()
    assert bs.SHA256_RE.fullmatch(result)
    # Not equal to the raw commit hash itself -- this is a hash *of* the commit hash.
    assert result != fake_sha1


def test_commit_hash_field_rejects_unknown_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bbs, "git_commit_fingerprint", lambda repo_root=None: "unknown")
    with pytest.raises(bbs.BakeoffSpecBuildError):
        bbs.commit_hash_field()


# -------------------------------------------------------------------------------------------
# derive_query_slots_hash
# -------------------------------------------------------------------------------------------


def test_derive_query_slots_hash_happy_path(tmp_path: Path) -> None:
    manifest = _build_corpus_manifest()
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    result = bbs.derive_query_slots_hash(slots_path, assignments_path, manifest)
    assignments = bs.validate_query_slot_assignments(json.loads(assignments_path.read_text()))
    assert result == assignments["artifact_fingerprint"]
    assert bs.SHA256_RE.fullmatch(result)


def test_derive_query_slots_hash_rejects_corpus_hash_mismatch(tmp_path: Path) -> None:
    manifest = _build_corpus_manifest()
    other_manifest = _build_corpus_manifest(entity_ids=("ent-0002",))
    assert manifest["corpus_root_hash"] != other_manifest["corpus_root_hash"]
    # Slots sidecar is bound to `manifest`'s hash, but we validate against `other_manifest`.
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    with pytest.raises(bbs.BakeoffSpecBuildError, match="corpus_root_hash"):
        bbs.derive_query_slots_hash(slots_path, assignments_path, other_manifest)


def test_derive_query_slots_hash_rejects_tampered_slots_sidecar(tmp_path: Path) -> None:
    manifest = _build_corpus_manifest()
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    slots_doc = json.loads(slots_path.read_text())
    slots_doc["slots"][0]["topic_family_id"] = "tampered-family"
    slots_path.write_text(json.dumps(slots_doc, ensure_ascii=False))
    with pytest.raises(bbs.BakeoffSpecBuildError, match="fingerprint"):
        bbs.derive_query_slots_hash(slots_path, assignments_path, manifest)


def test_derive_query_slots_hash_rejects_assignments_not_matching_slots(tmp_path: Path) -> None:
    manifest = _build_corpus_manifest()
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    # Re-sign a QuerySlotAssignments over a different (but still valid) slot set.
    other_slots = _build_flat_slots()
    other_slots[0]["topic_family_id"] = "different-family-000"
    other_assignments = bqs.build_query_slot_assignments(other_slots)
    assignments_path.write_text(json.dumps(other_assignments, ensure_ascii=False))
    with pytest.raises(bbs.BakeoffSpecBuildError, match="does not match the slots sidecar"):
        bbs.derive_query_slots_hash(slots_path, assignments_path, manifest)


def test_derive_query_slots_hash_rejects_missing_slots_schema(tmp_path: Path) -> None:
    manifest = _build_corpus_manifest()
    _, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    slots_path = tmp_path / "bad_slots.json"
    slots_path.write_text(json.dumps({"slots": []}, ensure_ascii=False))
    with pytest.raises(bbs.BakeoffSpecBuildError, match="schema"):
        bbs.derive_query_slots_hash(slots_path, assignments_path, manifest)


# -------------------------------------------------------------------------------------------
# load_contenders / derive_configuration_hash
# -------------------------------------------------------------------------------------------


def test_load_contenders_builds_expected_ids_from_synthetic_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", _fake_candidate_by_model_id)
    model_locks_dir = _write_fake_model_locks(tmp_path)
    contenders, specs = bbs.load_contenders(model_locks_dir, FAKE_PINNED_MODELS)
    assert contenders == [
        "dense:fake/dense-a:entity",
        "dense:fake/dense-b:entity",
        "late_interaction:fake/late-a:entity",
        "lexical:bm25",
    ]
    assert len(contenders) == len(set(contenders))
    by_id = {item["contender_id"]: item for item in specs}
    assert by_id["dense:fake/dense-a:entity"]["kind"] == "dense"
    assert by_id["dense:fake/dense-a:entity"]["channel"] == "entity"
    assert by_id["dense:fake/dense-a:entity"]["model_lock_source_repository"] == "repo-for-fake__dense-a"
    assert by_id["late_interaction:fake/late-a:entity"]["kind"] == "late_interaction"
    assert by_id["lexical:bm25"]["model_lock_source_repository"] is None
    assert by_id["lexical:bm25"]["kind"] == "lexical"


def test_load_contenders_rejects_missing_model_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", _fake_candidate_by_model_id)
    model_locks_dir = tmp_path / "model_locks"
    model_locks_dir.mkdir()
    # Only write one of the three expected lock files.
    lock = _build_model_lock()
    (model_locks_dir / f"{bbs.model_slug(FAKE_PINNED_MODELS[0])}.json").write_text(
        json.dumps(lock, ensure_ascii=False)
    )
    with pytest.raises(bbs.BakeoffSpecBuildError, match="missing signed ModelLock"):
        bbs.load_contenders(model_locks_dir, FAKE_PINNED_MODELS)


def test_load_contenders_rejects_tampered_model_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", _fake_candidate_by_model_id)
    model_locks_dir = _write_fake_model_locks(tmp_path)
    slug = bbs.model_slug(FAKE_PINNED_MODELS[0])
    path = model_locks_dir / f"{slug}.json"
    lock = json.loads(path.read_text())
    lock["dimension"] = lock["dimension"] + 1  # invalidates the signed fingerprint
    path.write_text(json.dumps(lock, ensure_ascii=False))
    with pytest.raises(bs.BakeoffContractError, match="fingerprint"):
        bbs.load_contenders(model_locks_dir, FAKE_PINNED_MODELS)


def test_load_contenders_rejects_unsupported_candidate_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", lambda model_id: _FakeCandidate("bogus"))
    model_locks_dir = _write_fake_model_locks(tmp_path)
    with pytest.raises(bbs.BakeoffSpecBuildError, match="unsupported candidate kind"):
        bbs.load_contenders(model_locks_dir, FAKE_PINNED_MODELS)


def test_derive_configuration_hash_is_deterministic_and_order_independent() -> None:
    specs = [
        {"contender_id": "dense:a:entity", "kind": "dense", "channel": "entity", "model_lock_source_repository": "r-a"},
        {"contender_id": "dense:b:entity", "kind": "dense", "channel": "entity", "model_lock_source_repository": "r-b"},
        {"contender_id": "lexical:bm25", "kind": "lexical", "channel": "bm25_plus_current_head", "model_lock_source_repository": None},
    ]
    grids = {"retrieval_cell_variant": ["single_pinned_configuration"]}
    first = bbs.derive_configuration_hash(specs, grids)
    second = bbs.derive_configuration_hash(list(reversed(specs)), grids)
    assert first == second
    assert bs.SHA256_RE.fullmatch(first)


def test_derive_configuration_hash_changes_with_contender_set() -> None:
    base_specs = [
        {"contender_id": "dense:a:entity", "kind": "dense", "channel": "entity", "model_lock_source_repository": "r-a"},
    ]
    grids = {"retrieval_cell_variant": ["single_pinned_configuration"]}
    extended_specs = base_specs + [
        {"contender_id": "lexical:bm25", "kind": "lexical", "channel": "bm25_plus_current_head", "model_lock_source_repository": None},
    ]
    assert bbs.derive_configuration_hash(base_specs, grids) != bbs.derive_configuration_hash(
        extended_specs, grids
    )


# -------------------------------------------------------------------------------------------
# derive_software_versions
# -------------------------------------------------------------------------------------------


def test_derive_software_versions_returns_real_installed_versions() -> None:
    versions = bbs.derive_software_versions()
    assert set(versions) == {"python", "fastembed", "onnxruntime", "numpy"}
    assert all(isinstance(value, str) and value for value in versions.values())


def test_derive_software_versions_rejects_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    real_version = importlib.metadata.version

    def _fake_version(name: str) -> str:
        if name == "onnxruntime":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(bbs.importlib.metadata, "version", _fake_version)
    with pytest.raises(bbs.BakeoffSpecBuildError, match="onnxruntime"):
        bbs.derive_software_versions()


# -------------------------------------------------------------------------------------------
# derive_holm_comparison_family
# -------------------------------------------------------------------------------------------


def test_derive_holm_comparison_family_includes_required_comparison() -> None:
    family = bbs.derive_holm_comparison_family()
    assert "winner_vs_baseline_same_specific_fact" in family
    assert len(family) == len(set(family))
    assert len(family) > 0


# -------------------------------------------------------------------------------------------
# End-to-end: build_bakeoff_spec against synthetic fixtures
# -------------------------------------------------------------------------------------------


def test_build_bakeoff_spec_end_to_end_is_accepted_by_validate_bakeoff_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", _fake_candidate_by_model_id)
    manifest = _build_corpus_manifest()
    manifest_path = tmp_path / "corpus_representation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    model_locks_dir = _write_fake_model_locks(tmp_path)

    spec = bbs.build_bakeoff_spec(
        run_id="unit-test-gate-d-run",
        corpus_manifest_path=manifest_path,
        slots_path=slots_path,
        assignments_path=assignments_path,
        model_locks_dir=model_locks_dir,
        pinned_models=FAKE_PINNED_MODELS,
    )

    validated = bs.validate_bakeoff_spec(spec)
    assert validated["run_id"] == "unit-test-gate-d-run"
    assert validated["corpus_snapshot_hash"] == manifest["corpus_root_hash"]
    assert validated["split_targets"] == {"dev": 400, "blind": 800}
    assert set(validated["contenders"]) == {
        "dense:fake/dense-a:entity",
        "dense:fake/dense-b:entity",
        "late_interaction:fake/late-a:entity",
        "lexical:bm25",
    }
    assert validated["required_metrics"] == list(bs.REQUIRED_METRICS)
    assert "winner_vs_baseline_same_specific_fact" in validated["holm_comparison_family"]

    # Re-signing the exact same inputs must be fully deterministic (content-addressed).
    spec_again = bbs.build_bakeoff_spec(
        run_id="unit-test-gate-d-run",
        corpus_manifest_path=manifest_path,
        slots_path=slots_path,
        assignments_path=assignments_path,
        model_locks_dir=model_locks_dir,
        pinned_models=FAKE_PINNED_MODELS,
    )
    assert spec_again["artifact_fingerprint"] == spec["artifact_fingerprint"]


def test_build_bakeoff_spec_rejects_invalid_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bbs, "candidate_by_model_id", _fake_candidate_by_model_id)
    manifest = _build_corpus_manifest()
    manifest_path = tmp_path / "corpus_representation_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
    slots_path, assignments_path = _write_query_slot_fixtures(tmp_path, manifest["corpus_root_hash"])
    model_locks_dir = _write_fake_model_locks(tmp_path)

    with pytest.raises(bbs.BakeoffSpecBuildError, match="run_id"):
        bbs.build_bakeoff_spec(
            run_id="not a safe run id!",
            corpus_manifest_path=manifest_path,
            slots_path=slots_path,
            assignments_path=assignments_path,
            model_locks_dir=model_locks_dir,
            pinned_models=FAKE_PINNED_MODELS,
        )
