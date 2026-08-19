import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import run_retrieval_bakeoff  # noqa: E402
from bakeoff_state import sign_artifact, fingerprint  # noqa: E402
from run_retrieval_bakeoff import (  # noqa: E402
    RetrievalBakeoffError,
    adapter_model_lock,
    execute_dense_cell,
    execute_late_cell,
    load_frozen_documents,
)


class DenseBackend:
    def embed(self, documents):
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in documents]

    def query_embed(self, _query):
        return np.array([1.0, 0.0], dtype=np.float32)


class LateBackend:
    """A single-dimension token matrix (shape (tokens, 1)) is used deliberately: a real 2D
    numpy matrix with more than one element trips an unrelated, pre-existing bug in
    retrieval_index_runner._pack_matrix (`if not matrix:` is ambiguous for size>1 arrays,
    independently confirmed and out of scope for this file's changes -- see session report).
    dimension=1 keeps every array at size<=1 so this test-only backend can still exercise the
    real build()/search() pipeline instead of dodging it with a bare kwarg spy."""

    def passage_embed(self, documents):
        return [np.array([[1.0]], dtype=np.float32) for _ in documents]

    def query_embed(self, _query):
        return np.array([[1.0]], dtype=np.float32)


def late_lock_artifact(cache):
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model.onnx").write_bytes(b"model")
    file_hash = hashlib.sha256(b"model").hexdigest()
    return sign_artifact(
        "ModelLock",
        {
            "source_repository": "Qdrant/bge-small-en-v1.5-onnx-Q",
            "resolved_revision": "a" * 40,
            "files": [{"path": "model.onnx", "sha256": file_hash, "size_bytes": 5}],
            "dimension": 1,
            "normalization": "l2",
            "maximum_input_tokens": 32,
            "query_prefix": "query: ",
            "document_prefix": "passage: ",
        },
    )


def artifacts(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.onnx").write_bytes(b"model")
    file_hash = hashlib.sha256(b"model").hexdigest()
    lock = sign_artifact(
        "ModelLock",
        {
            "source_repository": "Qdrant/bge-small-en-v1.5-onnx-Q",
            "resolved_revision": "a" * 40,
            "files": [{"path": "model.onnx", "sha256": file_hash, "size_bytes": 5}],
            "dimension": 2,
            "normalization": "l2",
            "maximum_input_tokens": 32,
            "query_prefix": "query: ",
            "document_prefix": "passage: ",
        },
    )
    entity = {
        "entity_id": "e1",
        "title_hash": hashlib.sha256(b"Title").hexdigest(),
        "body_hash": hashlib.sha256(b"Body").hexdigest(),
        "source_hash": fingerprint("source"),
        "chunk_hashes": [hashlib.sha256(b"Chunk").hexdigest()],
    }
    manifest_payload = {
        "eligible_ids": ["e1"],
        "entities": [entity],
        "representation_version": "v1",
    }
    manifest_payload["corpus_root_hash"] = fingerprint(manifest_payload)
    manifest = sign_artifact("CorpusRepresentationManifest", manifest_payload)
    export = {
        "entities": [
            {
                "entity_id": "e1",
                "title": "Title",
                "body": "Body",
                "chunks": ["Chunk"],
                "source_hash": entity["source_hash"],
            }
        ]
    }
    spec = {"run_id": "run", "artifact_fingerprint": fingerprint("spec")}
    return cache, lock, manifest, export, spec


def test_frozen_document_loading_and_dense_execution_with_fake_backend(tmp_path):
    cache, lock_artifact, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(lock_artifact, cache, kind="dense")
    documents = load_frozen_documents(export, manifest, "entity")
    result = execute_dense_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        channel="entity",
        backend_factory=lambda **_kwargs: DenseBackend(),
    )
    assert result["complete_query_count"] == 1
    assert result["results"][0]["top20"][0]["entity_id"] == "e1"
    assert result["index_receipt"]["ready"] == 1


def test_corpus_export_set_and_source_hash_mismatch_fail_closed(tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    _cache, _lock, manifest, export, _spec = artifacts(second)
    with pytest.raises(RetrievalBakeoffError, match="eligible set"):
        load_frozen_documents({"entities": []}, manifest, "entity")
    export["entities"][0]["source_hash"] = fingerprint("tampered")
    with pytest.raises(RetrievalBakeoffError, match="source hash"):
        load_frozen_documents(export, manifest, "entity")


def test_corpus_export_content_hash_mismatch_fails_closed(tmp_path):
    _cache, _lock, manifest, export, _spec = artifacts(tmp_path)
    export["entities"][0]["body"] = "Changed"
    with pytest.raises(RetrievalBakeoffError, match="body hash"):
        load_frozen_documents(export, manifest, "entity")

    export["entities"][0]["body"] = "Body"
    export["entities"][0]["chunks"] = ["Changed"]
    with pytest.raises(RetrievalBakeoffError, match="chunk hash"):
        load_frozen_documents(export, manifest, "entity")


def test_adapter_model_lock_resolves_fastembed_canonical_model_id(tmp_path):
    """model_id must be the fastembed-canonical logical_model_id, never the raw download repo."""
    cache, lock_artifact, _manifest, _export, _spec = artifacts(tmp_path)
    lock = adapter_model_lock(lock_artifact, cache, kind="dense")
    assert lock.spec.model_id == "BAAI/bge-small-en-v1.5"
    # The raw Gate-A download repo id must never leak into the resolved spec's model_id.
    assert lock.spec.model_id != "Qdrant/bge-small-en-v1.5-onnx-Q"
    # tokenizer is intentionally left as the raw source_repository (unaffected by this fix).
    assert lock.spec.tokenizer == "Qdrant/bge-small-en-v1.5-onnx-Q"


def test_adapter_model_lock_rejects_unpinned_source_repository(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.onnx").write_bytes(b"model")
    file_hash = hashlib.sha256(b"model").hexdigest()
    lock_artifact = sign_artifact(
        "ModelLock",
        {
            "source_repository": "nobody/not-a-pinned-model",
            "resolved_revision": "a" * 40,
            "files": [{"path": "model.onnx", "sha256": file_hash, "size_bytes": 5}],
            "dimension": 2,
            "normalization": "l2",
            "maximum_input_tokens": 32,
            "query_prefix": "query: ",
            "document_prefix": "passage: ",
        },
    )
    with pytest.raises(RetrievalBakeoffError, match="no pinned model"):
        adapter_model_lock(lock_artifact, cache, kind="dense")


def test_execute_dense_cell_default_batch_size_omits_build_kwarg(tmp_path, monkeypatch):
    """When --batch-size is omitted, index.build() must be called with no batch_size kwarg at
    all (not the same literal default), so existing successful runs are provably unaffected."""
    cache, lock_artifact, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(lock_artifact, cache, kind="dense")
    documents = load_frozen_documents(export, manifest, "entity")

    calls: list[dict] = []
    original_build = run_retrieval_bakeoff.DenseIndexRunner.build

    def spy_build(self, documents, **kwargs):
        calls.append(kwargs)
        return original_build(self, documents, **kwargs)

    monkeypatch.setattr(run_retrieval_bakeoff.DenseIndexRunner, "build", spy_build)

    execute_dense_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        channel="entity",
        backend_factory=lambda **_kwargs: DenseBackend(),
    )
    assert calls == [{}]


def test_execute_dense_cell_explicit_batch_size_threads_kwarg(tmp_path, monkeypatch):
    cache, lock_artifact, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(lock_artifact, cache, kind="dense")
    documents = load_frozen_documents(export, manifest, "entity")

    calls: list[dict] = []
    original_build = run_retrieval_bakeoff.DenseIndexRunner.build

    def spy_build(self, documents, **kwargs):
        calls.append(kwargs)
        return original_build(self, documents, **kwargs)

    monkeypatch.setattr(run_retrieval_bakeoff.DenseIndexRunner, "build", spy_build)

    execute_dense_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        channel="entity",
        backend_factory=lambda **_kwargs: DenseBackend(),
        batch_size=4,
    )
    assert calls == [{"batch_size": 4}]


def test_execute_late_cell_default_batch_size_omits_build_kwarg(tmp_path, monkeypatch):
    _cache, _lock, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(
        late_lock_artifact(tmp_path / "late_cache"),
        tmp_path / "late_cache",
        kind="late_interaction",
    )
    documents = load_frozen_documents(export, manifest, "entity")

    calls: list[dict] = []
    original_build = run_retrieval_bakeoff.LateInteractionIndexRunner.build

    def spy_build(self, documents, **kwargs):
        calls.append(kwargs)
        return original_build(self, documents, **kwargs)

    monkeypatch.setattr(run_retrieval_bakeoff.LateInteractionIndexRunner, "build", spy_build)

    result = execute_late_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        backend_factory=lambda **_kwargs: LateBackend(),
    )
    assert calls == [{}]
    assert result["complete_query_count"] == 1


def test_execute_late_cell_explicit_batch_size_threads_kwarg(tmp_path, monkeypatch):
    _cache, _lock, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(
        late_lock_artifact(tmp_path / "late_cache"),
        tmp_path / "late_cache",
        kind="late_interaction",
    )
    documents = load_frozen_documents(export, manifest, "entity")

    calls: list[dict] = []
    original_build = run_retrieval_bakeoff.LateInteractionIndexRunner.build

    def spy_build(self, documents, **kwargs):
        calls.append(kwargs)
        return original_build(self, documents, **kwargs)

    monkeypatch.setattr(run_retrieval_bakeoff.LateInteractionIndexRunner, "build", spy_build)

    result = execute_late_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        backend_factory=lambda **_kwargs: LateBackend(),
        batch_size=1,
    )
    assert calls == [{"batch_size": 1}]
    assert result["complete_query_count"] == 1


# -------------------------------------------------------------------------------------------
# _apply_memory_ceiling
# -------------------------------------------------------------------------------------------


def test_apply_memory_ceiling_contains_a_runaway_allocation():
    """A deliberate oversized allocation under a tight ceiling must fail cleanly in-process.

    This is the actual safety property the ceiling exists for: proving a runaway allocation
    raises MemoryError instantly rather than being accepted by the kernel and satisfied through
    swap (the failure mode that previously froze the host). Runs in a real subprocess so the
    RLIMIT_AS change (irreversible for a process's lifetime -- it can only be lowered further,
    never raised) never leaks into the test runner's own process.
    """
    import subprocess

    script = (
        "import sys; sys.path.insert(0, 'scripts/benchmarking'); "
        "from run_retrieval_bakeoff import _apply_memory_ceiling; "
        "_apply_memory_ceiling(64); "
        "import numpy as np\n"
        "try:\n"
        "    np.zeros((2_000_000_000,), dtype=np.float64)\n"
        "    print('ALLOCATED')\n"
        "except MemoryError:\n"
        "    print('CONTAINED')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "CONTAINED", result.stdout + result.stderr


def test_apply_memory_ceiling_none_or_zero_leaves_process_unbounded(monkeypatch):
    calls = []
    monkeypatch.setattr(
        run_retrieval_bakeoff.resource,
        "setrlimit",
        lambda *args: calls.append(args),
    )
    run_retrieval_bakeoff._apply_memory_ceiling(None)
    run_retrieval_bakeoff._apply_memory_ceiling(0)
    assert calls == []


def test_cli_defaults_to_the_nonzero_memory_ceiling(monkeypatch):
    """--memory-limit-mb must default to a positive value: the ceiling is opt-out, not opt-in."""
    assert run_retrieval_bakeoff.DEFAULT_MEMORY_LIMIT_MB > 0
    seen: list[int | None] = []
    monkeypatch.setattr(run_retrieval_bakeoff, "_apply_memory_ceiling", seen.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_retrieval_bakeoff.py",
            "--spec",
            "missing.json",
            "--corpus-manifest",
            "missing.json",
            "--queries-dev",
            "missing.json",
            "--kind",
            "lexical",
            "--db-path",
            "missing.db",
            "--out",
            str(Path("unused") / "out.json"),
        ],
    )
    with pytest.raises(Exception):  # noqa: B017 - fails later loading missing.json; we only care
        run_retrieval_bakeoff.main()  # that the ceiling was applied first, with the CLI default.
    assert seen == [run_retrieval_bakeoff.DEFAULT_MEMORY_LIMIT_MB]
