import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import run_retrieval_bakeoff  # noqa: E402
from bakeoff_state import BlindAccessError, sign_artifact, fingerprint  # noqa: E402
from build_evaluation_queries import write_manifest  # noqa: E402
from run_retrieval_bakeoff import (  # noqa: E402
    RetrievalBakeoffError,
    adapter_model_lock,
    execute_dense_cell,
    execute_late_cell,
    execute_lexical_cell,
    load_query_manifest,
    load_frozen_documents,
    validate_lexical_snapshot_receipt,
)
from lexical_adapter import LexicalHit  # noqa: E402


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


def late_lock_artifact(cache, source_repository="Qdrant/bge-small-en-v1.5-onnx-Q"):
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model.onnx").write_bytes(b"model")
    file_hash = hashlib.sha256(b"model").hexdigest()
    return sign_artifact(
        "ModelLock",
        {
            "source_repository": source_repository,
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


def _blind_manifest(tmp_path, count=800):
    queries = [
        {
            "id": f"blind-{index:04d}",
            "query": f"synthetic blind query {index}",
            "lang": "en",
            "category": "strict_negative",
            "subtype": "synthetic",
            "split": "blind",
            "source_entity_ids": [],
            "topic_family_id": f"family-{index:04d}",
            "length_bucket": "short",
            "provenance": "synthetic",
        }
        for index in range(count)
    ]
    return write_manifest(
        queries,
        tmp_path / "manifest.json",
        corpus_fingerprint="a" * 64,
        slot_fingerprint="b" * 64,
        commit_fingerprint="c" * 64,
        random_seed=7,
        config_fingerprint="d" * 64,
        judge_version_fingerprint="e" * 64,
    )


def test_blind_path_requires_all_custody_controls_before_read(tmp_path):
    target = tmp_path / "sealed.json"
    target.write_bytes(b"protected sentinel")
    with pytest.raises(RetrievalBakeoffError, match="vault, spec, development winner"):
        load_query_manifest(target, split="blind")
    assert target.read_bytes() == b"protected sentinel"


def test_authorized_blind_manifest_is_parsed_from_exact_800_returned_bytes(monkeypatch, tmp_path):
    manifest = _blind_manifest(tmp_path)
    payload = json.dumps(manifest, separators=(",", ":")).encode()
    calls = []

    def authorize(*args, **kwargs):
        calls.append((args, kwargs))
        return payload

    monkeypatch.setattr(run_retrieval_bakeoff, "authorize_blind_file", authorize)
    paths = tuple(tmp_path / name for name in ("vault", "spec", "winner", "unlock", "receipt"))
    loaded = load_query_manifest(
        paths[0] / "queries.json",
        split="blind",
        vault_dir=paths[0],
        spec_path=paths[1],
        winner_path=paths[2],
        unlock_path=paths[3],
        manifest_receipt_path=paths[4],
    )
    assert len(loaded["queries"]) == 800
    assert calls == [
        (
            (
                paths[0] / "queries.json",
                paths[0],
                paths[1],
                paths[2],
                paths[3],
                paths[4],
            ),
            {},
        )
    ]


def test_blind_manifest_rejects_wrong_count_and_manifest_fingerprint(monkeypatch, tmp_path):
    manifest = _blind_manifest(tmp_path, count=799)
    payload = json.dumps(manifest, separators=(",", ":")).encode()
    monkeypatch.setattr(run_retrieval_bakeoff, "authorize_blind_file", lambda *a, **k: payload)
    with pytest.raises(RetrievalBakeoffError, match="exactly 800"):
        load_query_manifest(
            tmp_path / "sealed.json",
            split="blind",
            vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json",
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )

    tampered = json.loads(json.dumps(_blind_manifest(tmp_path)))
    tampered["queries"][0]["query"] = "tampered"
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "authorize_blind_file",
        lambda *a, **k: json.dumps(tampered, separators=(",", ":")).encode(),
    )
    with pytest.raises(RetrievalBakeoffError, match="manifest_fingerprint mismatch"):
        load_query_manifest(
            tmp_path / "sealed.json",
            split="blind",
            vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json",
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


def test_blind_authorization_failure_from_stale_receipt_is_not_bypassed(monkeypatch, tmp_path):
    def reject(*args, **kwargs):
        raise BlindAccessError("stale receipt")

    monkeypatch.setattr(run_retrieval_bakeoff, "authorize_blind_file", reject)
    with pytest.raises(BlindAccessError, match="stale receipt"):
        load_query_manifest(
            tmp_path / "sealed.json",
            split="blind",
            vault_dir=tmp_path,
            spec_path=tmp_path / "spec.json",
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


def test_blind_execution_rejects_a_losing_cell_before_backend_start():
    binding = {
        "authorized_query_manifest_fingerprint": "a" * 64,
        "blind_manifest_receipt_fingerprint": "b" * 64,
        "blind_manifest_file_sha256": "c" * 64,
    }
    with pytest.raises(RetrievalBakeoffError, match="only the selected development winner"):
        run_retrieval_bakeoff._validate_blind_execution(
            "dense:BAAI/bge-small-en-v1.5:entity", binding
        )


def test_blind_binding_rejects_receipt_swapped_after_authorization(monkeypatch, tmp_path):
    payload = b"authorized manifest bytes"
    monkeypatch.setattr(run_retrieval_bakeoff, "_load_json", lambda path: {})
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "validate_development_winner",
        lambda artifact, spec: {
            "pipeline": {"contender_id": run_retrieval_bakeoff.BLIND_WINNER_ID}
        },
    )
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "validate_blind_unlock",
        lambda artifact, spec, winner: {},
    )
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "validate_blind_manifest_receipt",
        lambda artifact, spec, winner, unlock: {
            "artifact_fingerprint": "r" * 64,
            "file_sha256": "s" * 64,
        },
    )
    with pytest.raises(RetrievalBakeoffError, match="does not match authorized payload"):
        run_retrieval_bakeoff._load_blind_binding(
            query_manifest={"manifest_fingerprint": "m" * 64},
            spec={},
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            receipt_path=tmp_path / "receipt.json",
            authorized_payload_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_dev_manifest_path_retains_400_query_contract(tmp_path):
    manifest = _blind_manifest(tmp_path, count=399)
    for query in manifest["queries"]:
        query["split"] = "dev"
    manifest["queries_fingerprint"] = run_retrieval_bakeoff.artifact_fingerprint(
        manifest["queries"]
    )
    unsigned = dict(manifest)
    unsigned.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = run_retrieval_bakeoff.artifact_fingerprint(unsigned)
    path = tmp_path / "queries_dev.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RetrievalBakeoffError, match="exactly 400"):
        load_query_manifest(path, split="dev")


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


def test_blind_late_bundle_carries_authorized_manifest_and_receipt_binding(tmp_path):
    _cache, _lock, manifest, export, spec = artifacts(tmp_path)
    lock = adapter_model_lock(
        late_lock_artifact(
            tmp_path / "late_cache", source_repository="answerdotai/answerai-colbert-small-v1"
        ),
        tmp_path / "late_cache",
        kind="late_interaction",
    )
    documents = load_frozen_documents(export, manifest, "entity")
    binding = {
        "authorized_query_manifest_fingerprint": "a" * 64,
        "blind_manifest_receipt_fingerprint": "b" * 64,
        "blind_manifest_file_sha256": "c" * 64,
    }
    result = execute_late_cell(
        spec=spec,
        manifest=manifest,
        queries=[{"id": "q1", "query": "question"}],
        documents=documents,
        lock=lock,
        sidecar_path=tmp_path / "index.sqlite",
        blind_binding=binding,
        backend_factory=lambda **_kwargs: LateBackend(),
    )
    assert {field: result[field] for field in binding} == binding


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


def test_raw_production_lexical_policy_skips_current_head_inclusion(monkeypatch, tmp_path):
    class Connection:
        def close(self):
            return None

    monkeypatch.setattr(run_retrieval_bakeoff, "get_connection", lambda _path: Connection())
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "bm25_search",
        lambda _conn, _query, limit=20: [
            LexicalHit("raw-a", -1.0, 1, False),
            LexicalHit("raw-b", -2.0, 2, False),
        ][:limit],
    )
    monkeypatch.setattr(
        run_retrieval_bakeoff,
        "include_current_heads",
        lambda *_args, **_kwargs: pytest.fail("raw policy must not include current heads"),
    )
    bundle = execute_lexical_cell(
        spec={"run_id": "run", "artifact_fingerprint": "spec-fingerprint"},
        queries=[{"id": "q1", "query": "raw query"}],
        db_path=tmp_path / "snapshot.db",
        lexical_policy="raw_production",
        representation_root="root-hash",
        lexical_snapshot_receipt={
            "artifact_fingerprint": "receipt-fingerprint",
            "db_sha256_informational": "d" * 64,
        },
    )
    assert bundle["cell"] == {
        "kind": "lexical",
        "channel": "bm25_raw_production",
        "lexical_policy": "raw_production",
        "production_faithful": True,
        "representation_root": "root-hash",
        "lexical_snapshot_receipt_fingerprint": "receipt-fingerprint",
        "lexical_snapshot_db_sha256": "d" * 64,
    }
    assert [row["entity_id"] for row in bundle["results"][0]["top20"]] == ["raw-a", "raw-b"]


def test_raw_production_receipt_rejects_wrong_db_or_corpus(monkeypatch, tmp_path):
    db_path = tmp_path / "snapshot.db"
    db_path.write_bytes(b"frozen-db")
    import hashlib

    receipt = {
        "schema_version": 1,
        "kind": "LexicalSnapshotReceipt",
        "corpus_root_hash": "root-hash",
        "db_path": str(db_path),
        "db_sha256_informational": hashlib.sha256(b"frozen-db").hexdigest(),
    }
    receipt = sign_artifact("LexicalSnapshotReceipt", receipt)
    assert (
        validate_lexical_snapshot_receipt(
            receipt, db_path=db_path, expected_corpus_root="root-hash"
        )["artifact_fingerprint"]
        == receipt["artifact_fingerprint"]
    )
    with pytest.raises(RetrievalBakeoffError, match="corpus root"):
        validate_lexical_snapshot_receipt(
            receipt,
            db_path=db_path,
            expected_corpus_root="wrong-root",
        )
    wrong = dict(receipt)
    wrong["db_sha256_informational"] = "0" * 64
    wrong = sign_artifact(
        "LexicalSnapshotReceipt", {k: v for k, v in wrong.items() if k != "artifact_fingerprint"}
    )
    with pytest.raises(RetrievalBakeoffError, match="SHA-256"):
        validate_lexical_snapshot_receipt(wrong, db_path=db_path, expected_corpus_root="root-hash")
