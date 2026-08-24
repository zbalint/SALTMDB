import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/benchmarking"))

from build_development_judging_addendum import (  # noqa: E402
    JudgingAddendumError,
    build_addendum,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / "scratch/eval_results/accuracy-bakeoff-20260812"
RAW = ROOT / "reports/gate-d-raw-lexical-development-20260824.json"
WORKLIST = ROOT / "reports/gate-d-raw-lexical-judging-addendum-20260824.json"
SPEC = ARTIFACT_ROOT / "gate_d_devrun2/bakeoff_spec.json"
QUERIES = ARTIFACT_ROOT / "gate_d_devrun2/queries_dev.json"
MATRIX = ARTIFACT_ROOT / "gate_d_devrun2/judging_matrix.json"
MANIFEST = ARTIFACT_ROOT / "corpus_representation_manifest.json"
EXPORT = ARTIFACT_ROOT / "corpus_export.json"


def _build():
    return build_addendum(
        raw_bundle_path=RAW,
        worklist_path=WORKLIST,
        judging_matrix_path=MATRIX,
        spec_path=SPEC,
        query_manifest_path=QUERIES,
        corpus_manifest_path=MANIFEST,
        corpus_export_path=EXPORT,
    )


@pytest.mark.skipif(not RAW.exists(), reason="development custody artifacts are not checked out")
def test_addendum_is_exact_241_pair_worklist_with_frozen_text():
    result = _build()
    assert result["kind"] == "JudgingMatrix"
    assert result["query_count"] == 172
    assert result["missing_pair_count"] == 241
    assert len(result["pools"]) == 172
    assert sum(map(len, result["pools"].values())) == 241
    old = json.loads(MATRIX.read_text())
    for query_id, candidates in result["pools"].items():
        assert not set(candidates).intersection(old["pools"][query_id])
        assert all(item["title"] or item["full_content"] for item in candidates.values())


@pytest.mark.skipif(not RAW.exists(), reason="development custody artifacts are not checked out")
def test_addendum_binds_raw_spec_query_and_corpus_inputs():
    result = _build()
    raw = json.loads(RAW.read_text())
    spec = json.loads(SPEC.read_text())
    manifest = json.loads(MANIFEST.read_text())
    assert result["spec_fingerprint"] == spec["artifact_fingerprint"]
    assert (
        result["corpus_root_hash"] == manifest["corpus_root_hash"] == spec["corpus_snapshot_hash"]
    )
    assert result["raw_bundle_fingerprint"] == raw["artifact_fingerprint"]
    assert result["raw_cell"]["lexical_policy"] == "raw_production"
    assert result["raw_cell"]["lexical_snapshot_receipt_fingerprint"]
    assert result["query_manifest_fingerprint"]
    assert result["corpus_manifest_fingerprint"] == manifest["artifact_fingerprint"]


@pytest.mark.skipif(not RAW.exists(), reason="development custody artifacts are not checked out")
def test_addendum_fingerprint_is_deterministic_and_worklist_tampering_fails(tmp_path):
    first = _build()
    second = _build()
    assert first == second
    tampered = json.loads(WORKLIST.read_text())
    query_id = next(iter(tampered["missing_pairs_by_query"]))
    tampered["missing_pairs_by_query"][query_id] = []
    import hashlib

    unsigned = dict(tampered)
    unsigned["artifact_fingerprint"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    worklist = tmp_path / "tampered.json"
    worklist.write_text(json.dumps(unsigned))
    with pytest.raises(JudgingAddendumError, match="worklist"):
        build_addendum(
            raw_bundle_path=RAW,
            worklist_path=worklist,
            judging_matrix_path=MATRIX,
            spec_path=SPEC,
            query_manifest_path=QUERIES,
            corpus_manifest_path=MANIFEST,
            corpus_export_path=EXPORT,
        )
