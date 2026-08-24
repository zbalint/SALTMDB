import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "benchmarking"))

from bakeoff_state import fingerprint, sign_artifact  # noqa: E402
from fresh_development_protocol import (  # noqa: E402
    CANDIDATE_ARM,
    FreshDevelopmentError,
    SUBTYPE_QUOTAS,
    build_fresh_development_spec,
    build_fresh_query_manifest,
)
from run_fresh_development_retrieval import (  # noqa: E402
    FreshRetrievalError,
    build_timing_runner,
    _build_test_timing_runner,
    run_public_fresh_retrieval,
    run_public_fresh_retrieval_from_paths,
)
import run_fresh_development_retrieval as runner_module  # noqa: E402
from saltmdb.db import connection as db_connection  # noqa: E402


PINNED_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"


def test_timing_executor_closes_dense_resources_when_lexical_close_fails(monkeypatch):
    class DenseStack:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    dense_stack = DenseStack()
    lexical_conn = object()

    def fail_lexical_close(connection):
        assert connection is lexical_conn
        raise RuntimeError("synthetic lexical close failure")

    monkeypatch.setattr(db_connection, "close_connection", fail_lexical_close)
    executor = runner_module.PublicTimingExecutor(
        spec={},
        manifest={},
        documents=[],
        title_index={},
        lexical_search=lambda _query: [],
        dense_search=lambda _query, _documents: [],
        lexical_conn=lexical_conn,
        dense_stack=dense_stack,
    )

    with pytest.raises(RuntimeError, match="synthetic lexical close failure"):
        executor.close()

    assert dense_stack.closed is True
    assert executor._lexical_conn is None
    assert executor._dense_stack is None
    assert executor._closed is True
    executor.close()


def _records():
    rows = []
    for facet, subtypes in SUBTYPE_QUOTAS.items():
        ordinal = 0
        for subtype, count in subtypes.items():
            for index in range(count):
                row = {
                    "id": f"fresh-{facet}-{subtype}-{index:03d}",
                    "query": f"fresh {facet} {subtype} query {index}",
                    "category": facet,
                    "subtype": subtype,
                    "topic_family_id": (
                        f"current-family-{index}"
                        if facet == "current_vs_superseded"
                        else f"{facet}-family-{ordinal % 28}"
                    ),
                    "source_entity_ids": []
                    if facet == "strict_negative"
                    else [f"doc-{facet}-{subtype}-{index:03d}"],
                }
                if facet == "multilingual":
                    row["language"] = subtype
                rows.append(row)
                ordinal += 1
    return rows


def _model_lock(tmp_path):
    cache = tmp_path / "model-cache"
    cache.mkdir()
    payload = {
        "source_repository": "Qdrant/bge-small-en-v1.5-onnx-Q",
        "resolved_revision": PINNED_REVISION,
        "files": [{"path": "model.onnx", "sha256": "a" * 64, "size_bytes": 1}],
        "dimension": 384,
        "normalization": "l2",
        "maximum_input_tokens": 512,
        "query_prefix": "",
        "document_prefix": "",
    }
    return sign_artifact("ModelLock", payload), cache


def _spec_and_manifest(tmp_path):
    model, cache = _model_lock(tmp_path)
    spec = build_fresh_development_spec(
        experiment_id="runner-synthetic",
        production_commit="a" * 40,
        corpus_snapshot_hash="0" * 64,
        bge_model_revision=PINNED_REVISION,
        bge_model_lock_fingerprint=model["artifact_fingerprint"],
        lexical_adapter_fingerprint="1" * 64,
        exact_title_rule_fingerprint="2" * 64,
        machine_fingerprint="9" * 64,
        rubric_fingerprint="4" * 64,
        bootstrap_resamples=1000,
    )
    records = _records()
    manifest = build_fresh_query_manifest(
        records,
        spec=spec,
        source_artifact_id="public-runner-synthetic",
        source_artifact_fingerprint="5" * 64,
        source_kind="unprotected_development_export",
        protected_query_ids=["old-query"],
        protected_topic_family_ids=["old-family"],
        protected_source_entity_ids=["old-source"],
        prior_assignment_fingerprint="6" * 64,
        protected_query_manifest_fingerprint="7" * 64,
    )
    return spec, manifest, model, cache


def _corpus(spec, manifest):
    ids = sorted(
        {entity_id for query in manifest["queries"] for entity_id in query["source_entity_ids"]}
        | {"other", "none"}
    )
    entities = []
    export_rows = []
    for entity_id in ids:
        matching = [
            query["query"]
            for query in manifest["queries"]
            if query["category"] == "exact_title"
            and query["subtype"] == "unique_byte_exact_singleton"
            and query["source_entity_ids"] == [entity_id]
        ]
        title = matching[0] if matching else f"title {entity_id}"
        body = f"body {entity_id}"
        chunks = [f"chunk {entity_id}"]
        source_hash = hashlib.sha256(f"source {entity_id}".encode()).hexdigest()
        entities.append(
            {
                "entity_id": entity_id,
                "title_hash": hashlib.sha256(title.encode()).hexdigest(),
                "body_hash": hashlib.sha256(body.encode()).hexdigest(),
                "source_hash": source_hash,
                "chunk_hashes": [hashlib.sha256(chunks[0].encode()).hexdigest()],
            }
        )
        export_rows.append(
            {
                "entity_id": entity_id,
                "title": title,
                "body": body,
                "chunks": chunks,
                "source_hash": source_hash,
            }
        )
    root = fingerprint(
        {"eligible_ids": ids, "entities": entities, "representation_version": "synthetic-v1"}
    )
    # The runner spec is intentionally rebuilt by the caller with this immutable root.
    return (
        sign_artifact(
            "CorpusRepresentationManifest",
            {
                "eligible_ids": ids,
                "entities": entities,
                "representation_version": "synthetic-v1",
                "corpus_root_hash": root,
            },
        ),
        {"entities": export_rows},
        root,
    )


def _inputs(tmp_path):
    model, cache = _model_lock(tmp_path)
    corpus_root = "8" * 64
    spec = build_fresh_development_spec(
        experiment_id="runner-synthetic",
        production_commit="a" * 40,
        corpus_snapshot_hash=corpus_root,
        bge_model_revision=PINNED_REVISION,
        bge_model_lock_fingerprint=model["artifact_fingerprint"],
        lexical_adapter_fingerprint="1" * 64,
        exact_title_rule_fingerprint="2" * 64,
        machine_fingerprint="9" * 64,
        rubric_fingerprint="4" * 64,
        bootstrap_resamples=1000,
    )
    manifest = build_fresh_query_manifest(
        _records(),
        spec=spec,
        source_artifact_id="public-runner-synthetic",
        source_artifact_fingerprint="5" * 64,
        source_kind="unprotected_development_export",
        protected_query_ids=["old-query"],
        protected_topic_family_ids=["old-family"],
        protected_source_entity_ids=["old-source"],
        prior_assignment_fingerprint="6" * 64,
        protected_query_manifest_fingerprint="7" * 64,
    )
    # Construct the corpus first, then rebuild the frozen spec with its content root.
    corpus, export, root = _corpus(spec, manifest)
    spec = build_fresh_development_spec(
        experiment_id="runner-synthetic",
        production_commit="a" * 40,
        corpus_snapshot_hash=root,
        bge_model_revision=PINNED_REVISION,
        bge_model_lock_fingerprint=model["artifact_fingerprint"],
        lexical_adapter_fingerprint="1" * 64,
        exact_title_rule_fingerprint="2" * 64,
        machine_fingerprint="9" * 64,
        rubric_fingerprint="4" * 64,
        bootstrap_resamples=1000,
    )
    manifest = build_fresh_query_manifest(
        _records(),
        spec=spec,
        source_artifact_id="public-runner-synthetic",
        source_artifact_fingerprint="5" * 64,
        source_kind="unprotected_development_export",
        protected_query_ids=["old-query"],
        protected_topic_family_ids=["old-family"],
        protected_source_entity_ids=["old-source"],
        prior_assignment_fingerprint="6" * 64,
        protected_query_manifest_fingerprint="7" * 64,
    )
    db = tmp_path / "lexical-snapshot.sqlite"
    db.write_bytes(b"synthetic lexical snapshot")
    lexical = sign_artifact(
        "LexicalSnapshotReceipt",
        {
            "corpus_root_hash": root,
            "db_path": str(db.resolve()),
            "db_sha256_informational": hashlib.sha256(db.read_bytes()).hexdigest(),
        },
    )
    sidecar = tmp_path / "fresh-sidecar.sqlite"
    return spec, manifest, corpus, export, lexical, db, model, cache, sidecar


def _run(
    tmp_path,
    lexical_search=None,
    dense_search=None,
    retain_timing_resources=False,
    runtime_probe=None,
):
    inputs = _inputs(tmp_path)
    spec, manifest, corpus, export, lexical, db, model, cache, sidecar = inputs
    calls = {"lexical": 0, "dense": 0}

    def lexical_fn(text):
        calls["lexical"] += 1
        query = next(row for row in manifest["queries"] if row["query"] == text)
        entity = query["source_entity_ids"][0] if query["source_entity_ids"] else "none"
        return [
            {"entity_id": entity, "raw_bm25_score": -10.0},
            {"entity_id": "other", "raw_bm25_score": -1.0},
        ]

    def dense_fn(text, _documents):
        calls["dense"] += 1
        query = next(row for row in manifest["queries"] if row["query"] == text)
        entity = query["source_entity_ids"][0] if query["source_entity_ids"] else "none"
        return [{"entity_id": entity, "score": 1.0}, {"entity_id": "other", "score": 0.0}]

    result = run_public_fresh_retrieval(
        spec=spec,
        manifest=manifest,
        corpus_manifest=corpus,
        corpus_export=export,
        lexical_snapshot_receipt=lexical,
        db_path=db,
        model_lock=model,
        model_cache=cache,
        sidecar_path=sidecar,
        environment_fingerprint="9" * 64,
        lexical_search=lexical_search or lexical_fn,
        dense_search=dense_search or dense_fn,
        retain_timing_resources=retain_timing_resources,
        _runtime_identity_probe=runtime_probe
        or (
            lambda: {
                "git_commit": spec["production"]["git_commit"],
                "git_object_format": spec["production"]["git_object_format"],
                "machine_fingerprint": "9" * 64,
            }
        ),
    )
    return result, manifest, calls


def test_public_runner_derives_evidence_and_skips_exact_singleton_channels(tmp_path):
    result, manifest, calls = _run(tmp_path)
    exact = next(
        row for row in manifest["queries"] if row["subtype"] == "unique_byte_exact_singleton"
    )
    cell = result["retrieval_evidence"]["cells"][exact["id"]]
    assert cell["exact_title"]["triggered"] is True
    assert cell["lexical"]["ids"] == []
    assert cell["dense"]["ids"] == []
    # Sixteen byte-exact singleton rows take the deployed corpus identity fast path; four
    # mismatch controls still execute both channels.
    assert calls["lexical"] == calls["dense"] == 384
    assert result["candidate_texts"][exact["id"]]
    assert result["production_receipt"]["kind"] == "ProductionConfigReceipt"


def test_public_runner_rejects_protected_and_live_paths_before_open(tmp_path):
    paths = {
        "spec_path": tmp_path / "blind" / "spec.json",
        "manifest_path": tmp_path / "manifest.json",
        "corpus_manifest_path": tmp_path / "corpus.json",
        "corpus_export_path": tmp_path / "export.json",
        "lexical_receipt_path": tmp_path / "lexical.json",
        "db_path": tmp_path / "lexical.sqlite",
        "model_lock_path": tmp_path / "model.json",
        "model_cache": tmp_path / "cache",
        "sidecar_path": tmp_path / "sidecar.sqlite",
        "environment_fingerprint": "a" * 64,
    }
    with pytest.raises(FreshRetrievalError):
        run_public_fresh_retrieval_from_paths(**paths)
    paths["spec_path"] = tmp_path / "spec.json"
    paths["db_path"] = tmp_path / "saltmdb.db"
    with pytest.raises(FreshRetrievalError):
        run_public_fresh_retrieval_from_paths(**paths)


def test_runtime_identity_mismatch_fails_closed(tmp_path):
    with pytest.raises(FreshRetrievalError):
        _run(
            tmp_path,
            runtime_probe=lambda: {
                "git_commit": "f" * 40,
                "git_object_format": "sha1",
                "machine_fingerprint": "9" * 64,
            },
        )


def test_timing_runner_executes_two_distinct_end_to_end_measurements(tmp_path):
    result, manifest, _ = _run(tmp_path)
    calls = []
    ticks = iter((0.0, 0.001, 0.010, 0.012, 0.020, 0.023, 0.030, 0.034))

    def execute(arm, query):
        calls.append((arm, query["id"]))
        return result["rankings"][arm][query["id"]]

    runner = _build_test_timing_runner(result, execute_query=execute, clock=lambda: next(ticks))
    query = manifest["queries"][0]
    ranking, samples = runner("deployed_hybrid_rrf", query, "measure")
    assert ranking == result["rankings"]["deployed_hybrid_rrf"][query["id"]]
    assert len(samples) == 2 and samples[0] != samples[1]
    assert len(calls) == 2


def test_timing_runner_requires_real_query_callback(tmp_path):
    result, _, _ = _run(tmp_path)
    with pytest.raises(FreshRetrievalError):
        build_timing_runner(result)


def test_retained_public_executor_repeats_full_fusion_without_rebuilding_index(tmp_path):
    result, manifest, calls = _run(tmp_path, retain_timing_resources=True)
    executor = result["timing_executor"]
    query = next(
        row for row in manifest["queries"] if row["subtype"] == "byte_mismatch_fallthrough"
    )
    before = dict(calls)
    try:
        runner = build_timing_runner(result, executor=executor)
        _, samples = runner(CANDIDATE_ARM, query, "measure")
        assert len(samples) == 2
        ranking = executor.execute_query(CANDIDATE_ARM, query)
        assert ranking == result["rankings"][CANDIDATE_ARM][query["id"]]
        assert calls["lexical"] == before["lexical"] + 3
        assert calls["dense"] == before["dense"] + 3
    finally:
        executor.close()


def test_dense_startup_failure_closes_pre_executor_lexical_connection(tmp_path, monkeypatch):
    spec, manifest, corpus, export, lexical, db, model, cache, sidecar = _inputs(tmp_path)
    fake_connection = object()
    closed = []
    monkeypatch.setattr("saltmdb.db.connection.get_connection", lambda _path: fake_connection)
    monkeypatch.setattr(
        "saltmdb.db.connection.close_connection", lambda connection: closed.append(connection)
    )
    monkeypatch.setattr(
        runner_module,
        "adapter_model_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic startup failure")),
    )
    with pytest.raises(FreshRetrievalError):
        runner_module.run_public_fresh_retrieval(
            spec=spec,
            manifest=manifest,
            corpus_manifest=corpus,
            corpus_export=export,
            lexical_snapshot_receipt=lexical,
            db_path=db,
            model_lock=model,
            model_cache=cache,
            sidecar_path=sidecar,
            environment_fingerprint="9" * 64,
            _runtime_identity_probe=lambda: {
                "git_commit": spec["production"]["git_commit"],
                "git_object_format": spec["production"]["git_object_format"],
                "machine_fingerprint": "9" * 64,
            },
        )
    assert closed == [fake_connection]


def test_persisted_bundle_contains_raw_retrieval_only(tmp_path):
    result, _, _ = _run(tmp_path)
    output = tmp_path / "public-bundle.json"
    artifact = runner_module.persist_public_retrieval_bundle(result, output)
    assert artifact["kind"] == "FreshPublicRetrievalBundle"
    assert "labels" not in artifact
    assert output.exists()


def test_persisted_bundle_refuses_existing_output_without_overwrite(tmp_path):
    result, _, _ = _run(tmp_path)
    output = tmp_path / "existing-bundle.json"
    original = b"first-evidence"
    output.write_bytes(original)
    with pytest.raises(FreshRetrievalError):
        runner_module.persist_public_retrieval_bundle(result, output)
    assert output.read_bytes() == original


def test_native_git_sha256_identity_and_bound_commit_digest():
    commit = "a" * 64
    spec = build_fresh_development_spec(
        experiment_id="sha256-git-synthetic",
        production_commit=commit,
        git_object_format="sha256",
        commit_id_sha256=hashlib.sha256(commit.encode()).hexdigest(),
        corpus_snapshot_hash="0" * 64,
        bge_model_revision="b" * 64,
        bge_model_lock_fingerprint="c" * 64,
        lexical_adapter_fingerprint="d" * 64,
        exact_title_rule_fingerprint="e" * 64,
        machine_fingerprint="f" * 64,
        rubric_fingerprint="0" * 64,
        bootstrap_resamples=1000,
    )
    assert spec["production"]["git_object_format"] == "sha256"
    with pytest.raises(FreshDevelopmentError):
        build_fresh_development_spec(
            experiment_id="sha256-git-forged",
            production_commit=commit,
            git_object_format="sha256",
            commit_id_sha256="1" * 64,
            corpus_snapshot_hash="0" * 64,
            bge_model_revision="b" * 40,
            bge_model_lock_fingerprint="c" * 64,
            lexical_adapter_fingerprint="d" * 64,
            exact_title_rule_fingerprint="e" * 64,
            machine_fingerprint="f" * 64,
            rubric_fingerprint="0" * 64,
            bootstrap_resamples=1000,
        )


def test_manifest_builder_rejects_duplicate_query_text(tmp_path):
    records = _records()
    records[1]["query"] = records[0]["query"]
    model, _ = _model_lock(tmp_path)
    spec = build_fresh_development_spec(
        experiment_id="duplicate-query-synthetic",
        production_commit="a" * 40,
        corpus_snapshot_hash="0" * 64,
        bge_model_revision=PINNED_REVISION,
        bge_model_lock_fingerprint=model["artifact_fingerprint"],
        lexical_adapter_fingerprint="1" * 64,
        exact_title_rule_fingerprint="2" * 64,
        machine_fingerprint="9" * 64,
        rubric_fingerprint="4" * 64,
        bootstrap_resamples=1000,
    )
    with pytest.raises(FreshDevelopmentError):
        build_fresh_query_manifest(
            records,
            spec=spec,
            source_artifact_id="public-runner-synthetic",
            source_artifact_fingerprint="5" * 64,
            source_kind="unprotected_development_export",
            protected_query_ids=["old-query"],
            protected_topic_family_ids=["old-family"],
            protected_source_entity_ids=["old-source"],
            prior_assignment_fingerprint="6" * 64,
            protected_query_manifest_fingerprint="7" * 64,
        )
