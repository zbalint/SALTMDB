"""Focused tests for the local-only retrieval adapter boundary.

Every backend in this file is a fake.  The tests intentionally never import FastEmbed, download
models, open the corpus, or use an existing model cache.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from retrieval_adapters import (  # noqa: E402
    BackendContractError,
    ColBERTAdapter,
    DenseEmbeddingAdapter,
    LateInteractionEmbeddingAdapter,
    ModelLock,
    ModelLockError,
    PrefixContractError,
    maxsim,
    verify_model_lock,
)
from retrieval_architecture import EmbeddingSpec, LateInteractionError  # noqa: E402


def _spec(
    *,
    model_id: str = "fake/dense",
    dimension: int = 3,
    kind: str = "dense",
    normalization: str = "l2",
    query_prefix: str = "query: ",
    document_prefix: str = "passage: ",
) -> EmbeddingSpec:
    return EmbeddingSpec(
        model_id=model_id,
        revision="a" * 40,
        dimension=dimension,
        query_prefix=query_prefix,
        document_prefix=document_prefix,
        normalization=normalization,
        tokenizer="fake-tokenizer",
        max_input_tokens=32,
        kind=kind,
    )


def _write_inventory(root: Path) -> None:
    (root / "model.onnx").write_bytes(b"model-bytes")
    (root / "tokenizer.json").write_bytes(b"tokenizer-bytes")


class FakeDense:
    def __init__(self, *, vectors: list[np.ndarray] | None = None) -> None:
        self.vectors = vectors or [np.array([1.0, 0.0, 0.0], dtype=np.float32)]
        self.documents: list[str] = []
        self.queries: list[str] = []

    def embed(self, documents: list[str]) -> list[np.ndarray]:
        self.documents.extend(documents)
        return [self.vectors[index % len(self.vectors)] for index in range(len(documents))]

    def query_embed(self, query: str) -> np.ndarray:
        self.queries.append(query)
        return self.vectors[0]


class FakeLate:
    def __init__(self) -> None:
        self.documents: list[str] = []
        self.queries: list[str] = []

    def passage_embed(self, documents: list[str]) -> list[np.ndarray]:
        self.documents.extend(documents)
        return [
            np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
            for _ in documents
        ]

    def query_embed(self, query: str) -> np.ndarray:
        self.queries.append(query)
        return np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)


def _dense_adapter(
    tmp_path: Path,
    *,
    backend: object | None = None,
    spec: EmbeddingSpec | None = None,
    factory: object | None = None,
) -> tuple[DenseEmbeddingAdapter, object, dict[str, object]]:
    root = tmp_path / "dense-cache"
    root.mkdir()
    _write_inventory(root)
    actual_spec = spec or _spec()
    lock = ModelLock.from_directory(actual_spec, root)
    selected_backend = backend or FakeDense()
    calls: dict[str, object] = {}

    def make_backend(**kwargs: object) -> object:
        calls.update(kwargs)
        return selected_backend

    selected_factory = factory or make_backend
    adapter = DenseEmbeddingAdapter(actual_spec, lock, selected_factory)
    return adapter, selected_backend, calls


def _late_adapter(
    tmp_path: Path,
    *,
    backend: object | None = None,
    spec: EmbeddingSpec | None = None,
) -> tuple[LateInteractionEmbeddingAdapter, object, dict[str, object]]:
    root = tmp_path / "late-cache"
    root.mkdir()
    _write_inventory(root)
    actual_spec = spec or _spec(
        model_id="answerdotai/answerai-colbert-small-v1",
        dimension=2,
        kind="late_interaction",
        normalization="l2",
    )
    lock = ModelLock.from_directory(actual_spec, root)
    selected_backend = backend or FakeLate()
    calls: dict[str, object] = {}

    def make_backend(**kwargs: object) -> object:
        calls.update(kwargs)
        return selected_backend

    adapter = LateInteractionEmbeddingAdapter(actual_spec, lock, make_backend)
    return adapter, selected_backend, calls


def test_dense_adapter_verifies_cache_before_factory_and_renders_prefixes_once(tmp_path: Path):
    adapter, backend, calls = _dense_adapter(tmp_path)

    vectors = adapter.embed_documents(["first", "second"])
    query = adapter.embed_query("question")

    assert vectors.shape == (2, 3)
    assert np.allclose(query, [1.0, 0.0, 0.0])
    assert calls == {
        "model_name": "fake/dense",
        "cache_dir": str(tmp_path / "dense-cache"),
        "local_files_only": True,
        "specific_model_path": str(tmp_path / "dense-cache"),
    }
    assert isinstance(backend, FakeDense)
    assert backend.documents == ["passage: first", "passage: second"]
    assert backend.queries == ["query: question"]

    with pytest.raises(PrefixContractError, match="double rendering"):
        adapter.embed_documents(["passage: already rendered"])
    with pytest.raises(PrefixContractError, match="double rendering"):
        adapter.embed_query("query: already rendered")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing files"),
        ("unexpected", "unexpected files"),
        ("hash", "hash mismatch"),
        ("size", "size mismatch"),
    ],
)
def test_model_lock_rejects_every_inventory_mismatch(
    tmp_path: Path, mutation: str, message: str
):
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    spec = _spec()
    lock = ModelLock.from_directory(spec, root)
    factory_calls: list[dict[str, object]] = []

    if mutation == "missing":
        (root / "model.onnx").unlink()
    elif mutation == "unexpected":
        (root / "unexpected.bin").write_bytes(b"not locked")
    elif mutation == "hash":
        (root / "model.onnx").write_bytes(b"other-bytes")
    elif mutation == "size":
        (root / "model.onnx").write_bytes(b"changed-size")

    with pytest.raises(ModelLockError, match=message):
        verify_model_lock(lock)

    def factory(**kwargs: object) -> object:
        factory_calls.append(kwargs)
        return FakeDense()

    with pytest.raises(ModelLockError, match=message):
        DenseEmbeddingAdapter(spec, lock, factory)
    assert factory_calls == []


def test_model_lock_hash_and_size_are_checked_independently(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    spec = _spec()
    lock = ModelLock.from_directory(spec, root)
    (root / "model.onnx").write_bytes(b"new-size-and-hash")

    with pytest.raises(ModelLockError) as error:
        verify_model_lock(lock)
    assert "size mismatch" in str(error.value)
    assert "hash mismatch" in str(error.value)


def test_dense_adapter_fails_closed_for_colbert_even_when_shape_is_dense(tmp_path: Path):
    spec = _spec(
        model_id="answerdotai/answerai-colbert-small-v1",
        dimension=2,
        kind="late_interaction",
        normalization="none",
        query_prefix="",
        document_prefix="",
    )
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    lock = ModelLock.from_directory(spec, root)
    factory_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> object:
        factory_calls.append(kwargs)
        return FakeDense()

    with pytest.raises(LateInteractionError, match="dense adapter"):
        DenseEmbeddingAdapter(spec, lock, factory)
    assert factory_calls == []


@pytest.mark.parametrize(
    ("vectors", "message"),
    [
        ([np.array([1.0, 0.0], dtype=np.float32)], "dimension mismatch"),
        ([np.array([np.nan, 0.0, 0.0], dtype=np.float32)], "non-finite"),
        ([np.array([0.0, 0.0, 0.0], dtype=np.float32)], "cannot be l2-normalized"),
    ],
)
def test_dense_adapter_rejects_dimension_nonfinite_and_degenerate_zero_vectors(
    tmp_path: Path, vectors: list[np.ndarray], message: str
):
    backend = FakeDense(vectors=vectors)
    adapter, _, _ = _dense_adapter(tmp_path, backend=backend)
    with pytest.raises(BackendContractError, match=message):
        adapter.embed_documents(["document"])


def test_dense_adapter_l2_normalizes_raw_backend_output_before_validating(tmp_path: Path):
    """The declared "l2" contract is now guaranteed by the adapter, not merely observed.

    Reproduces the real nomic-embed-text-v1.5 failure mode: FastEmbed's ``PooledEmbedding`` class
    (used for that model) does not itself L2-normalize, so a raw backend vector with norm != 1.0
    must be rescaled here rather than rejected -- as long as it is finite and nonzero.
    """
    backend = FakeDense(vectors=[np.array([3.0, 4.0, 0.0], dtype=np.float32)])  # norm == 5.0
    adapter, _, _ = _dense_adapter(tmp_path, backend=backend)
    result = adapter.embed_document("document")
    assert np.allclose(result, [0.6, 0.8, 0.0])
    assert np.isclose(float(np.linalg.norm(result)), 1.0)


def test_dense_adapter_l2_normalizes_query_embedding_too(tmp_path: Path):
    backend = FakeDense(vectors=[np.array([0.0, 0.0, 19.301439], dtype=np.float32)])
    adapter, _, _ = _dense_adapter(tmp_path, backend=backend)
    result = adapter.embed_query("query")
    assert np.allclose(result, [0.0, 0.0, 1.0])


def test_dense_adapter_l2_normalization_is_a_stable_noop_for_already_unit_vectors(tmp_path: Path):
    """Already-correct backends (BGE, Snowflake Arctic) must be unaffected by this change."""
    already_unit = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    backend = FakeDense(vectors=[already_unit])
    adapter, _, _ = _dense_adapter(tmp_path, backend=backend)
    result = adapter.embed_document("document")
    assert np.allclose(result, already_unit)


def test_dense_adapter_accepts_explicit_none_normalization_without_mutating_vectors(tmp_path: Path):
    spec = _spec(normalization="none")
    backend = FakeDense(vectors=[np.array([2.0, 0.0, 0.0], dtype=np.float32)])
    adapter, _, _ = _dense_adapter(tmp_path, backend=backend, spec=spec)
    result = adapter.embed_document("document")
    assert np.array_equal(result, [2.0, 0.0, 0.0])


def test_dense_adapter_passes_specific_model_path_equal_to_cache_path(tmp_path: Path):
    """fastembed's hub-cache file discovery must be bypassed via specific_model_path.

    Gate A's flat snapshot_download(..., local_dir=...) layout is not a hub-cache layout, so
    cache_dir alone is not enough; specific_model_path must be forwarded and must equal the
    ModelLock's own cache_path (as a string), never merely a directory that happens to be near it.
    """
    _adapter, _backend, calls = _dense_adapter(tmp_path)
    assert calls["specific_model_path"] == str(tmp_path / "dense-cache")
    assert calls["cache_dir"] == str(tmp_path / "dense-cache")


def test_late_adapter_passes_specific_model_path_equal_to_cache_path(tmp_path: Path):
    _adapter, _backend, calls = _late_adapter(tmp_path)
    assert calls["specific_model_path"] == str(tmp_path / "late-cache")
    assert calls["cache_dir"] == str(tmp_path / "late-cache")


def test_fastembed_dense_factory_forwards_specific_model_path_kwarg(monkeypatch):
    """Proves the real (not fake) factory function forwards specific_model_path to fastembed."""
    captured: dict[str, object] = {}

    class SpyTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import fastembed

    monkeypatch.setattr(fastembed, "TextEmbedding", SpyTextEmbedding)

    from retrieval_adapters import fastembed_dense_factory

    fastembed_dense_factory(
        model_name="BAAI/bge-small-en-v1.5",
        cache_dir="/some/cache",
        local_files_only=True,
        specific_model_path="/some/cache",
    )
    assert captured == {
        "model_name": "BAAI/bge-small-en-v1.5",
        "cache_dir": "/some/cache",
        "local_files_only": True,
        "specific_model_path": "/some/cache",
    }


def test_fastembed_late_interaction_factory_forwards_specific_model_path_kwarg(monkeypatch):
    captured: dict[str, object] = {}

    class SpyLateInteractionTextEmbedding:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import fastembed

    monkeypatch.setattr(fastembed, "LateInteractionTextEmbedding", SpyLateInteractionTextEmbedding)

    from retrieval_adapters import fastembed_late_interaction_factory

    fastembed_late_interaction_factory(
        model_name="answerdotai/answerai-colbert-small-v1",
        cache_dir="/some/cache",
        local_files_only=True,
        specific_model_path="/some/cache",
    )
    assert captured == {
        "model_name": "answerdotai/answerai-colbert-small-v1",
        "cache_dir": "/some/cache",
        "local_files_only": True,
        "specific_model_path": "/some/cache",
    }


def test_dense_backend_factory_failure_has_no_online_fallback(tmp_path: Path):
    attempted: list[dict[str, object]] = []

    def factory(**kwargs: object) -> object:
        attempted.append(kwargs)
        raise RuntimeError("cache miss")

    with pytest.raises(BackendContractError, match="no online fallback"):
        _dense_adapter(tmp_path, factory=factory)
    assert attempted and attempted[0]["local_files_only"] is True


def test_late_adapter_uses_separate_passage_backend_and_pure_maxsim(tmp_path: Path):
    adapter, backend, calls = _late_adapter(tmp_path)

    documents = adapter.embed_documents(["one", "two"])
    query = adapter.embed_query("question")
    score = adapter.score("question", "one")

    assert len(documents) == 2
    assert query.shape == (2, 2)
    assert score == pytest.approx(2.0)
    assert calls["local_files_only"] is True
    assert calls["specific_model_path"] == str(tmp_path / "late-cache")
    assert isinstance(backend, FakeLate)
    assert backend.documents == ["passage: one", "passage: two", "passage: one"]
    assert backend.queries == ["query: question", "query: question"]
    assert ColBERTAdapter is LateInteractionEmbeddingAdapter


def test_maxsim_is_pure_and_supports_sum_and_mean_reduction():
    query = np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32)
    document = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    assert maxsim(query, document) == pytest.approx(1.5)
    assert maxsim(query, document, reduction="mean") == pytest.approx(0.75)
    with pytest.raises(BackendContractError, match="matching dimensions"):
        maxsim(query, np.ones((1, 3), dtype=np.float32))
    with pytest.raises(BackendContractError, match="non-finite"):
        maxsim(query, np.array([[np.inf, 0.0]], dtype=np.float32))
    with pytest.raises(BackendContractError, match="at least one token"):
        maxsim(np.empty((0, 2), dtype=np.float32), document)


@pytest.mark.parametrize(
    ("method", "value", "message"),
    [
        ("embed_documents", [np.ones((2, 3), dtype=np.float32)], "shape"),
        ("embed_documents", [np.array([[np.nan, 0.0], [0.0, 1.0]], dtype=np.float32)], "non-finite"),
        ("embed_documents", [np.array([[2.0, 0.0], [0.0, 1.0]], dtype=np.float32)], "normalization"),
    ],
)
def test_late_adapter_rejects_token_matrix_contract_violations(
    tmp_path: Path, method: str, value: list[np.ndarray], message: str
):
    class InvalidLate(FakeLate):
        def passage_embed(self, documents: list[str]) -> list[np.ndarray]:
            self.documents.extend(documents)
            return value

    adapter, _, _ = _late_adapter(tmp_path, backend=InvalidLate())
    with pytest.raises(BackendContractError, match=message):
        getattr(adapter, method)(["document"])


def test_late_adapter_requires_late_spec_and_separate_backend_surface(tmp_path: Path):
    dense_spec = _spec()
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    dense_lock = ModelLock.from_directory(dense_spec, root)
    with pytest.raises(LateInteractionError, match="requires late_interaction"):
        LateInteractionEmbeddingAdapter(dense_spec, dense_lock, lambda **_: FakeLate())

    class MissingPassage:
        def embed(self, documents: list[str]) -> list[np.ndarray]:
            return [np.ones(2, dtype=np.float32) for _ in documents]

        def query_embed(self, query: str) -> np.ndarray:
            return np.ones((1, 2), dtype=np.float32)

    late_spec = _spec(
        model_id="answerdotai/answerai-colbert-small-v1",
        dimension=2,
        kind="late_interaction",
        normalization="none",
    )
    late_lock = ModelLock.from_directory(late_spec, root)
    with pytest.raises(BackendContractError, match="passage_embed"):
        LateInteractionEmbeddingAdapter(late_spec, late_lock, lambda **_: MissingPassage())


def test_model_lock_inventory_is_content_addressed_and_deterministic(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    spec = _spec()
    lock = ModelLock.from_directory(spec, root)
    assert [entry.path for entry in lock.files] == ["model.onnx", "tokenizer.json"]
    for entry in lock.files:
        content = (root / entry.path).read_bytes()
        assert entry.sha256 == hashlib.sha256(content).hexdigest()
        assert entry.size_bytes == len(content)
    verified = verify_model_lock(lock)
    assert verified.file_count == 2


def test_model_lock_rejects_symlinked_cache_entries(tmp_path: Path):
    root = tmp_path / "cache"
    root.mkdir()
    _write_inventory(root)
    spec = _spec()
    lock = ModelLock.from_directory(spec, root)
    (root / "linked.onnx").symlink_to(root / "model.onnx")

    with pytest.raises(ModelLockError, match="symlink"):
        verify_model_lock(lock)
