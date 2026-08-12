import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from bakeoff_state import validate_model_lock  # noqa: E402
from materialize_model_locks import (  # noqa: E402
    EXPECTED_TOTAL_SELECTED_BYTES,
    PINNED_MODELS,
    ModelMaterializationError,
    PinnedModel,
    _inventory,
    build_model_lock,
    download_pinned_model,
    materialize_and_sign,
    model_slug,
    pinned_model_by_id,
)
from retrieval_architecture import candidate_by_model_id  # noqa: E402

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_pinned_inventory_has_nine_models_matching_declared_candidates():
    assert len(PINNED_MODELS) == 9
    assert sum(model.expected_selected_total_bytes for model in PINNED_MODELS) == (
        EXPECTED_TOTAL_SELECTED_BYTES
    )
    repos_and_revisions = set()
    for pinned in PINNED_MODELS:
        assert _REVISION_RE.fullmatch(pinned.resolved_revision), pinned.resolved_revision
        for path, sha256 in pinned.expected_model_file_sha256.items():
            assert not path.startswith("/")
            assert ".." not in Path(path).parts
            assert _SHA256_RE.fullmatch(sha256), (path, sha256)
        key = (pinned.source_repository, pinned.resolved_revision)
        assert key not in repos_and_revisions, f"duplicate repo/revision: {key}"
        repos_and_revisions.add(key)
        candidate = candidate_by_model_id(pinned.logical_model_id)
        assert candidate.kind == pinned.kind
    kinds = {pinned.kind for pinned in PINNED_MODELS}
    assert kinds == {"dense", "late_interaction"}
    assert sum(1 for pinned in PINNED_MODELS if pinned.kind == "late_interaction") == 1


def test_pinned_model_rejects_mismatched_file_and_hash_keys():
    with pytest.raises(ModelMaterializationError, match="must exactly match"):
        PinnedModel(
            logical_model_id="BAAI/bge-small-en-v1.5",
            source_repository="fake/repo",
            resolved_revision="a" * 40,
            model_files=("model.onnx",),
            expected_model_file_sha256={"other.onnx": "b" * 64},
            expected_selected_total_bytes=1,
        )


def test_pinned_model_by_id_lookup_and_unknown_raises():
    pinned = pinned_model_by_id("BAAI/bge-small-en-v1.5")
    assert pinned.source_repository == "Qdrant/bge-small-en-v1.5-onnx-Q"
    with pytest.raises(ModelMaterializationError):
        pinned_model_by_id("not/a-real-model")


def _fake_downloader_factory(model_bytes: dict[str, bytes], plant_symlinked_cache: bool = False):
    def _downloader(*, repo_id, revision, local_dir, allow_patterns):
        _downloader.calls.append(
            {"repo_id": repo_id, "revision": revision, "allow_patterns": tuple(allow_patterns)}
        )
        local_dir = Path(local_dir)
        for relative, data in model_bytes.items():
            path = local_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if plant_symlinked_cache:
            bookkeeping = local_dir / ".cache" / "huggingface"
            bookkeeping.mkdir(parents=True, exist_ok=True)
            real = local_dir / ".cache" / "real.lock"
            real.write_text("lock")
            (bookkeeping / "link").symlink_to(real)
        return str(local_dir)

    _downloader.calls = []
    return _downloader


def _test_pinned_model(model_bytes: bytes, *, logical_model_id="BAAI/bge-small-en-v1.5") -> PinnedModel:
    return PinnedModel(
        logical_model_id=logical_model_id,
        source_repository="Qdrant/bge-small-en-v1.5-onnx-Q",
        resolved_revision="a" * 40,
        model_files=("model_optimized.onnx",),
        expected_model_file_sha256={"model_optimized.onnx": _sha256(model_bytes)},
        expected_selected_total_bytes=len(model_bytes),
    )


def test_download_pinned_model_passes_pinned_revision_and_removes_cache_bookkeeping(tmp_path):
    model_bytes = b"onnx-weights"
    pinned = _test_pinned_model(model_bytes)
    downloader = _fake_downloader_factory(
        {
            "model_optimized.onnx": model_bytes,
            "config.json": b"{}",
        },
        plant_symlinked_cache=True,
    )
    materialized_dir = tmp_path / "materialized"
    download_pinned_model(pinned, materialized_dir, downloader=downloader)

    assert downloader.calls == [
        {
            "repo_id": "Qdrant/bge-small-en-v1.5-onnx-Q",
            "revision": "a" * 40,
            "allow_patterns": (
                "model_optimized.onnx",
                "config.json",
                "special_tokens_map.json",
                "tokenizer.json",
                "tokenizer_config.json",
            ),
        }
    ]
    assert not (materialized_dir / ".cache").exists()
    assert (materialized_dir / "model_optimized.onnx").read_bytes() == model_bytes


def test_inventory_rejects_symlinked_file(tmp_path):
    materialized_dir = tmp_path / "materialized"
    materialized_dir.mkdir()
    real = materialized_dir / "real.onnx"
    real.write_bytes(b"data")
    (materialized_dir / "model.onnx").symlink_to(real)
    with pytest.raises(ModelMaterializationError, match="symlink"):
        _inventory(materialized_dir)


def test_build_model_lock_signs_and_validates_matching_hash(tmp_path):
    model_bytes = b"correct-weights"
    pinned = _test_pinned_model(model_bytes)
    materialized_dir = tmp_path / "materialized"
    materialized_dir.mkdir()
    (materialized_dir / "model_optimized.onnx").write_bytes(model_bytes)
    (materialized_dir / "config.json").write_bytes(b"{}")

    lock = build_model_lock(pinned, materialized_dir)

    validate_model_lock(lock)
    assert lock["source_repository"] == pinned.source_repository
    assert lock["resolved_revision"] == pinned.resolved_revision
    candidate = candidate_by_model_id(pinned.logical_model_id)
    assert lock["dimension"] == candidate.dimension
    assert lock["normalization"] == candidate.normalization
    assert lock["maximum_input_tokens"] == candidate.max_input_tokens
    assert lock["query_prefix"] == candidate.query_prefix
    assert lock["document_prefix"] == candidate.document_prefix
    paths = {entry["path"] for entry in lock["files"]}
    assert paths == {"model_optimized.onnx", "config.json"}


def test_build_model_lock_rejects_pinned_hash_mismatch(tmp_path):
    pinned = _test_pinned_model(b"expected-weights")
    materialized_dir = tmp_path / "materialized"
    materialized_dir.mkdir()
    (materialized_dir / "model_optimized.onnx").write_bytes(b"tampered-weights")

    with pytest.raises(ModelMaterializationError, match="sha256 mismatch"):
        build_model_lock(pinned, materialized_dir)


def test_build_model_lock_rejects_missing_pinned_model_file(tmp_path):
    pinned = _test_pinned_model(b"expected-weights")
    materialized_dir = tmp_path / "materialized"
    materialized_dir.mkdir()
    (materialized_dir / "config.json").write_bytes(b"{}")

    with pytest.raises(ModelMaterializationError, match="was not materialized"):
        build_model_lock(pinned, materialized_dir)


def test_build_model_lock_rejects_kind_mismatch(tmp_path):
    model_bytes = b"colbert-weights"
    pinned = PinnedModel(
        logical_model_id="answerdotai/answerai-colbert-small-v1",
        source_repository="answerdotai/answerai-colbert-small-v1",
        resolved_revision="a" * 40,
        model_files=("vespa_colbert.onnx",),
        expected_model_file_sha256={"vespa_colbert.onnx": _sha256(model_bytes)},
        expected_selected_total_bytes=len(model_bytes),
        kind="dense",  # wrong on purpose: this model is declared late_interaction
    )
    materialized_dir = tmp_path / "materialized"
    materialized_dir.mkdir()
    (materialized_dir / "vespa_colbert.onnx").write_bytes(model_bytes)

    with pytest.raises(ModelMaterializationError, match="does not match pinned kind"):
        build_model_lock(pinned, materialized_dir)


def test_materialize_and_sign_writes_atomic_lock_file(tmp_path):
    model_bytes = b"end-to-end-weights"
    pinned = _test_pinned_model(model_bytes)
    downloader = _fake_downloader_factory({"model_optimized.onnx": model_bytes})
    materialized_root = tmp_path / "models"
    lock_output_dir = tmp_path / "locks"

    lock = materialize_and_sign(
        pinned,
        materialized_root=materialized_root,
        lock_output_dir=lock_output_dir,
        downloader=downloader,
    )

    lock_path = lock_output_dir / f"{model_slug(pinned)}.json"
    assert lock_path.exists()
    on_disk = json.loads(lock_path.read_text(encoding="utf-8"))
    assert on_disk == lock
    validate_model_lock(on_disk)
    assert (materialized_root / model_slug(pinned) / "model_optimized.onnx").read_bytes() == (
        model_bytes
    )
