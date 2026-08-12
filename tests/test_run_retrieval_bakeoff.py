import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from bakeoff_state import sign_artifact, fingerprint  # noqa: E402
from run_retrieval_bakeoff import (  # noqa: E402
    RetrievalBakeoffError,
    adapter_model_lock,
    execute_dense_cell,
    load_frozen_documents,
)


class DenseBackend:
    def embed(self, documents):
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in documents]

    def query_embed(self, _query):
        return np.array([1.0, 0.0], dtype=np.float32)


def artifacts(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.onnx").write_bytes(b"model")
    file_hash = hashlib.sha256(b"model").hexdigest()
    lock = sign_artifact(
        "ModelLock",
        {
            "source_repository": "fake/model",
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
