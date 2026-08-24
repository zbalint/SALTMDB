"""Tests for scripts/benchmarking/build_judging_matrix.py (Gate D pooled JudgingMatrix).

Fixtures are small, hand-built synthetic artifacts -- never the real frozen
``scratch/eval_results/accuracy-bakeoff-20260812`` inventory, which is exercised by a real,
read-only dry run recorded in the task history instead of by this unit suite.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

import bakeoff_state as bs  # noqa: E402
import build_judging_matrix as bjm  # noqa: E402
from evaluation_artifacts import build_provenance  # noqa: E402
import judge_pool  # noqa: E402


# -------------------------------------------------------------------------------------------
# Fixture helpers
# -------------------------------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_entities() -> dict[str, dict[str, str]]:
    return {
        "ent-0001": {
            "title": "Entity One",
            "body": "Body text about apples and orchards.",
            "source_hash": "src-0001",
        },
        "ent-0002": {
            "title": "Entity Two",
            "body": "Body text about bananas and plantations.",
            "source_hash": "src-0002",
        },
        "ent-0003": {
            "title": "Entity Three",
            "body": "Body text about cherries and blossoms.",
            "source_hash": "src-0003",
        },
    }


def _build_corpus_manifest(entities: dict[str, dict[str, str]]) -> dict[str, Any]:
    ids = sorted(entities)
    rows = [
        {
            "entity_id": entity_id,
            "title_hash": _sha(entities[entity_id]["title"]),
            "body_hash": _sha(entities[entity_id]["body"]),
            "source_hash": entities[entity_id]["source_hash"],
            "chunk_hashes": [_sha(entities[entity_id]["body"] + "-chunk")],
        }
        for entity_id in ids
    ]
    representation_version = "gate-d-test-v1"
    corpus_root_hash = bs.fingerprint(
        {"eligible_ids": ids, "entities": rows, "representation_version": representation_version}
    )
    return bs.sign_artifact(
        "CorpusRepresentationManifest",
        {
            "eligible_ids": ids,
            "entities": rows,
            "representation_version": representation_version,
            "corpus_root_hash": corpus_root_hash,
        },
    )


def _build_corpus_export(entities: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "title": data["title"],
                "body": data["body"],
                "source_hash": data["source_hash"],
                "chunks": [data["body"] + "-chunk"],
            }
            for entity_id, data in entities.items()
        ]
    }


def _dense_cell(model_id: str = "fake/dense-a", channel: str = "entity") -> dict[str, Any]:
    return {"model_id": model_id, "kind": "dense", "channel": channel}


def _lexical_cell() -> dict[str, Any]:
    return {"kind": "lexical", "channel": "bm25_plus_current_head"}


def _build_bundle(
    *,
    spec_fingerprint: str,
    cell: dict[str, Any],
    results: list[dict[str, Any]],
    failures: list[Any] | None = None,
    blind_binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = {
        "run_id": "test-run",
        "spec_fingerprint": spec_fingerprint,
        "cell": cell,
        "complete_query_count": len(results) - len(failures or []),
        "failures": failures or [],
        "results": results,
    }
    if blind_binding:
        payload.update(blind_binding)
    return bs.sign_artifact(
        "RetrievalRunBundle",
        payload,
    )


SPEC_FINGERPRINT = "f" * 64


def _dense_results() -> list[dict[str, Any]]:
    return [
        {
            "query_id": "eval-0001",
            "top20": [{"entity_id": "ent-0001", "item_id": "i1", "score": 0.9, "rank": 1}],
            "latency_ms": 1.0,
            "failure": None,
        },
        {
            "query_id": "eval-0002",
            "top20": [
                {"entity_id": "ent-0001", "item_id": "i1", "score": 0.8, "rank": 1},
                {"entity_id": "ent-0003", "item_id": "i3", "score": 0.7, "rank": 2},
            ],
            "latency_ms": 1.0,
            "failure": None,
        },
    ]


def _lexical_results() -> list[dict[str, Any]]:
    return [
        {
            "query_id": "eval-0001",
            "top20": [
                {
                    "entity_id": "ent-0003",
                    "rank": 1,
                    "raw_bm25_score": -1.2,
                    "lifecycle_included": False,
                }
            ],
            "latency_ms": 1.0,
            "failure": None,
        },
        {
            "query_id": "eval-0002",
            "top20": [
                {
                    "entity_id": "ent-0002",
                    "rank": 1,
                    "raw_bm25_score": -1.5,
                    "lifecycle_included": False,
                }
            ],
            "latency_ms": 1.0,
            "failure": None,
        },
    ]


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False))


# -------------------------------------------------------------------------------------------
# 1. Happy path: load_bundles + build_pools + manual sign/validate round-trip
# -------------------------------------------------------------------------------------------


def test_happy_path_produces_expected_pools_and_valid_signed_artifact(tmp_path: Path) -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)

    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    dense_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
    )
    lexical_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_lexical_cell(), results=_lexical_results()
    )
    _write(runs_dir / "dense.json", dense_bundle)
    _write(runs_dir / "lexical.json", lexical_bundle)

    spec = {
        "artifact_fingerprint": SPEC_FINGERPRINT,
        "contenders": ["dense:fake/dense-a:entity", "lexical:bm25"],
    }
    bundles = bjm.load_bundles(runs_dir, spec)
    assert set(bundles) == {"dense:fake/dense-a:entity", "lexical:bm25"}

    queries = [
        {"id": "eval-0001", "source_entity_ids": ["ent-0002"]},
        {"id": "eval-0002", "source_entity_ids": []},
    ]
    pools = bjm.build_pools(queries, bundles, entity_text, pool_top_n=20)

    expected = {
        "eval-0001": {
            "ent-0001": {
                "title": "Entity One",
                "full_content": "Body text about apples and orchards.",
                "ground_truth_forced_include": False,
            },
            "ent-0003": {
                "title": "Entity Three",
                "full_content": "Body text about cherries and blossoms.",
                "ground_truth_forced_include": False,
            },
            "ent-0002": {
                "title": "Entity Two",
                "full_content": "Body text about bananas and plantations.",
                "ground_truth_forced_include": True,
            },
        },
        "eval-0002": {
            "ent-0001": {
                "title": "Entity One",
                "full_content": "Body text about apples and orchards.",
                "ground_truth_forced_include": False,
            },
            "ent-0003": {
                "title": "Entity Three",
                "full_content": "Body text about cherries and blossoms.",
                "ground_truth_forced_include": False,
            },
            "ent-0002": {
                "title": "Entity Two",
                "full_content": "Body text about bananas and plantations.",
                "ground_truth_forced_include": False,
            },
        },
    }
    assert pools == expected

    payload = {
        "spec_fingerprint": SPEC_FINGERPRINT,
        "corpus_root_hash": manifest["corpus_root_hash"],
        "contenders": sorted(bundles),
        "query_count": len(queries),
        "pool_top_n": 20,
        "pools": pools,
    }
    matrix = bs.sign_artifact("JudgingMatrix", payload)
    # Assert the fingerprint is actually correct, not just present.
    unsigned = dict(matrix)
    unsigned.pop("artifact_fingerprint")
    assert matrix["artifact_fingerprint"] == bs.fingerprint(unsigned)
    validated = bs.validate_signed_artifact(matrix, kind="JudgingMatrix")
    assert validated["pools"] == expected


# -------------------------------------------------------------------------------------------
# 2. contender_id_for_cell
# -------------------------------------------------------------------------------------------


def test_contender_id_for_cell_dense() -> None:
    assert bjm.contender_id_for_cell(_dense_cell()) == "dense:fake/dense-a:entity"


def test_contender_id_for_cell_late_interaction() -> None:
    cell = {"kind": "late_interaction", "model_id": "fake/late-a", "channel": "entity"}
    assert bjm.contender_id_for_cell(cell) == "late_interaction:fake/late-a:entity"


def test_contender_id_for_cell_lexical_ignores_channel() -> None:
    assert bjm.contender_id_for_cell(_lexical_cell()) == "lexical:bm25"


def test_contender_id_for_cell_rejects_unsupported_kind() -> None:
    with pytest.raises(bjm.JudgingMatrixBuildError, match="unsupported"):
        bjm.contender_id_for_cell({"kind": "bogus"})


# -------------------------------------------------------------------------------------------
# 3. load_frozen_entity_text
# -------------------------------------------------------------------------------------------


def test_load_frozen_entity_text_happy_path() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    result = bjm.load_frozen_entity_text(export, manifest)
    assert result == {
        entity_id: {"title": data["title"], "body": data["body"]}
        for entity_id, data in entities.items()
    }


def test_load_frozen_entity_text_rejects_title_hash_mismatch() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    export["entities"][0]["title"] = "Tampered Title"
    with pytest.raises(bjm.JudgingMatrixBuildError, match="title hash"):
        bjm.load_frozen_entity_text(export, manifest)


def test_load_frozen_entity_text_rejects_body_hash_mismatch() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    export["entities"][0]["body"] = "Tampered body content."
    with pytest.raises(bjm.JudgingMatrixBuildError, match="body hash"):
        bjm.load_frozen_entity_text(export, manifest)


def test_load_frozen_entity_text_rejects_source_hash_mismatch() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    export["entities"][0]["source_hash"] = "tampered-source-hash"
    with pytest.raises(bjm.JudgingMatrixBuildError, match="source hash"):
        bjm.load_frozen_entity_text(export, manifest)


def test_load_frozen_entity_text_rejects_eligible_set_mismatch() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    export["entities"].pop()  # drop one entity so the export no longer covers eligible_ids
    with pytest.raises(bjm.JudgingMatrixBuildError, match="eligible"):
        bjm.load_frozen_entity_text(export, manifest)


# -------------------------------------------------------------------------------------------
# 4. load_bundles
# -------------------------------------------------------------------------------------------


def test_load_bundles_rejects_spec_fingerprint_mismatch(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle = _build_bundle(
        spec_fingerprint="wrong-fingerprint" + "0" * 47,
        cell=_dense_cell(),
        results=_dense_results(),
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="spec_fingerprint"):
        bjm.load_bundles(runs_dir, spec)


def test_load_bundles_rejects_same_ids_with_different_dev_manifest(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT,
        cell={**_dense_cell(), "representation_root": "c" * 64},
        results=_dense_results(),
        blind_binding={
            "query_manifest_fingerprint": "a" * 64,
            "query_manifest_file_sha256": "b" * 64,
            "query_split": "dev",
        },
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="exact query manifest"):
        bjm.load_bundles(
            runs_dir,
            spec,
            expected_query_binding={
                "query_manifest_fingerprint": "d" * 64,
                "query_manifest_file_sha256": "b" * 64,
                "query_split": "dev",
            },
        )


def test_load_bundles_rejects_cross_corpus_representation_root(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT,
        cell={**_dense_cell(), "representation_root": "a" * 64},
        results=_dense_results(),
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="representation_root"):
        bjm.load_bundles(runs_dir, spec, expected_representation_root="b" * 64)


def test_load_bundles_rejects_same_ids_with_stale_blind_binding(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    binding = {
        "authorized_query_manifest_fingerprint": "a" * 64,
        "blind_manifest_receipt_fingerprint": "b" * 64,
        "blind_manifest_file_sha256": "c" * 64,
    }
    bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT,
        cell=_dense_cell(),
        results=_dense_results(),
        blind_binding={**binding, "authorized_query_manifest_fingerprint": "d" * 64},
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="authorized blind manifest"):
        bjm.load_bundles(runs_dir, spec, expected_blind_binding=binding)


def test_load_bundles_rejects_non_empty_failures(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT,
        cell=_dense_cell(),
        results=_dense_results(),
        failures=[{"query_id": "eval-0001", "failure": "timeout"}],
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="failures"):
        bjm.load_bundles(runs_dir, spec)


def test_load_bundles_rejects_duplicate_contender_id_across_files(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle_a = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_lexical_cell(), results=_lexical_results()
    )
    bundle_b = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_lexical_cell(), results=_lexical_results()
    )
    _write(runs_dir / "lexical_a.json", bundle_a)
    _write(runs_dir / "lexical_b.json", bundle_b)
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["lexical:bm25"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="duplicate contender ID"):
        bjm.load_bundles(runs_dir, spec)


def test_load_bundles_rejects_missing_contender(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
    )
    _write(runs_dir / "dense.json", bundle)
    spec = {
        "artifact_fingerprint": SPEC_FINGERPRINT,
        "contenders": ["dense:fake/dense-a:entity", "lexical:bm25"],
    }
    with pytest.raises(bjm.JudgingMatrixBuildError, match="missing"):
        bjm.load_bundles(runs_dir, spec)


def test_load_bundles_rejects_extra_contender(tmp_path: Path) -> None:
    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    dense_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
    )
    lexical_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_lexical_cell(), results=_lexical_results()
    )
    _write(runs_dir / "dense.json", dense_bundle)
    _write(runs_dir / "lexical.json", lexical_bundle)
    # spec only declares the dense contender -- lexical:bm25 is an unexpected extra.
    spec = {"artifact_fingerprint": SPEC_FINGERPRINT, "contenders": ["dense:fake/dense-a:entity"]}
    with pytest.raises(bjm.JudgingMatrixBuildError, match="extra"):
        bjm.load_bundles(runs_dir, spec)


# -------------------------------------------------------------------------------------------
# 5. build_pools
# -------------------------------------------------------------------------------------------


def test_build_pools_rejects_pooled_entity_missing_from_entity_text() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)
    del entity_text["ent-0001"]  # simulate a corpus export missing an entity a bundle returned
    bundles = {
        "dense:fake/dense-a:entity": _build_bundle(
            spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
        )
    }
    queries = [
        {"id": "eval-0001", "source_entity_ids": []},
        {"id": "eval-0002", "source_entity_ids": []},
    ]
    with pytest.raises(
        bjm.JudgingMatrixBuildError, match="not present in the hash-verified corpus export"
    ):
        bjm.build_pools(queries, bundles, entity_text)


def test_build_pools_rejects_source_entity_id_missing_from_entity_text() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)
    bundles = {
        "dense:fake/dense-a:entity": _build_bundle(
            spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
        )
    }
    queries = [
        {"id": "eval-0001", "source_entity_ids": ["ent-9999"]},
        {"id": "eval-0002", "source_entity_ids": []},
    ]
    with pytest.raises(
        bjm.JudgingMatrixBuildError, match="not present in the hash-verified corpus export"
    ):
        bjm.build_pools(queries, bundles, entity_text)


def test_build_pools_rejects_bundle_missing_result_row_for_query() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)
    bundles = {
        "dense:fake/dense-a:entity": _build_bundle(
            spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
        )
    }
    queries = [
        {"id": "eval-0001", "source_entity_ids": []},
        {"id": "eval-9999", "source_entity_ids": []},  # not present in the bundle's results
    ]
    with pytest.raises(bjm.JudgingMatrixBuildError, match="missing a result row"):
        bjm.build_pools(queries, bundles, entity_text)


def test_build_pools_rejects_duplicate_query_id_in_bundle_results() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)
    results = _dense_results()
    results.append(dict(results[0]))  # duplicate query_id
    bundles = {
        "dense:fake/dense-a:entity": _build_bundle(
            spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=results
        )
    }
    queries = [{"id": "eval-0001", "source_entity_ids": []}]
    with pytest.raises(bjm.JudgingMatrixBuildError, match="duplicate query_id"):
        bjm.build_pools(queries, bundles, entity_text)


def test_build_pools_rejects_empty_resulting_pool() -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)
    bundles = {
        "dense:fake/dense-a:entity": _build_bundle(
            spec_fingerprint=SPEC_FINGERPRINT,
            cell=_dense_cell(),
            results=[
                {"query_id": "eval-0001", "top20": [], "latency_ms": 1.0, "failure": None},
            ],
        )
    }
    queries = [{"id": "eval-0001", "source_entity_ids": []}]
    with pytest.raises(bjm.JudgingMatrixBuildError, match="empty candidate pool"):
        bjm.build_pools(queries, bundles, entity_text)


# -------------------------------------------------------------------------------------------
# 6. Integration: judge_pool.build_judge_packets accepts the produced matrix end-to-end
# -------------------------------------------------------------------------------------------


def test_build_judge_packets_accepts_produced_matrix(tmp_path: Path) -> None:
    entities = _build_entities()
    manifest = _build_corpus_manifest(entities)
    export = _build_corpus_export(entities)
    entity_text = bjm.load_frozen_entity_text(export, manifest)

    runs_dir = tmp_path / "retrieval_runs"
    runs_dir.mkdir()
    dense_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_dense_cell(), results=_dense_results()
    )
    lexical_bundle = _build_bundle(
        spec_fingerprint=SPEC_FINGERPRINT, cell=_lexical_cell(), results=_lexical_results()
    )
    _write(runs_dir / "dense.json", dense_bundle)
    _write(runs_dir / "lexical.json", lexical_bundle)

    spec = {
        "artifact_fingerprint": SPEC_FINGERPRINT,
        "contenders": ["dense:fake/dense-a:entity", "lexical:bm25"],
    }
    bundles = bjm.load_bundles(runs_dir, spec)

    queries = [
        {
            "id": "eval-0001",
            "query": "What grows in the orchard?",
            "split": "dev",
            "source_entity_ids": ["ent-0002"],
        },
        {
            "id": "eval-0002",
            "query": "Tell me about cherries.",
            "split": "dev",
            "source_entity_ids": [],
        },
    ]
    pools = bjm.build_pools(queries, bundles, entity_text, pool_top_n=20)
    matrix = bs.sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": SPEC_FINGERPRINT,
            "corpus_root_hash": manifest["corpus_root_hash"],
            "contenders": sorted(bundles),
            "query_count": len(queries),
            "pool_top_n": 20,
            "pools": pools,
        },
    )

    packet, private = judge_pool.build_judge_packets(
        queries, matrix, judge_pool.JUDGES[0], "dev", base_seed=0
    )
    assert len(packet["tasks"]) == len(queries)
    assert isinstance(private, dict)
    assert set(private["tasks"]) == {task["task_id"] for task in packet["tasks"]}
    judge_pool.validate_judge_packet(packet)


# -------------------------------------------------------------------------------------------
# 7. Blind gateway: authorization, fixed 800-query shape, and upstream integrity failures
# -------------------------------------------------------------------------------------------


def _blind_gateway_setup(
    tmp_path: Path, *, query_count: int = 800, split: str = "blind"
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    winner_id = "late_interaction:answerdotai/answerai-colbert-small-v1:entity"
    spec = {
        "artifact_fingerprint": "a" * 64,
        "corpus_snapshot_hash": "c" * 64,
        "contenders": [winner_id, "lexical:bm25"],
    }
    winner = bs.sign_artifact("DevelopmentWinner", {"pipeline": {"contender_id": winner_id}})
    queries = {
        "queries": [
            {
                "id": f"blind-{i:04d}",
                "split": split,
                "topic_family_id": f"family-{i % 4}",
                "query": f"sealed text {i}",
                "lang": "en",
                "category": "body_text",
                "subtype": "fixture",
                "source_entity_ids": [],
                "length_bucket": "short",
                "provenance": {"fixture": True},
            }
            for i in range(query_count)
        ]
    }
    manifest = {
        "queries": queries["queries"],
        "queries_fingerprint": bs.fingerprint(queries["queries"]),
        "corpus_fingerprint": spec["corpus_snapshot_hash"],
        "schema_version": 1,
        "slot_fingerprint": "s" * 64,
        "provenance": build_provenance(
            commit="a" * 64,
            corpus=spec["corpus_snapshot_hash"],
            query="q" * 64,
            config="c" * 64,
            judge_version_fingerprint="j" * 64,
            random_seed=1,
            machine="m" * 64,
        ),
    }
    manifest["manifest_fingerprint"] = bs.fingerprint(manifest)
    return spec, winner, json.dumps(manifest).encode()


def _patch_blind_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
    spec: dict[str, Any],
    winner: dict[str, Any],
) -> None:
    _write(tmp_path / "spec.json", spec)
    _write(tmp_path / "winner.json", winner)
    for name in ("manifest.json", "corpus.json", "export.json", "unlock.json", "receipt.json"):
        _write(tmp_path / name, {})
    _write(
        tmp_path / "receipt.json",
        {
            "artifact_fingerprint": "r" * 64,
            "file_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    monkeypatch.setattr(bjm, "validate_bakeoff_spec", lambda value: spec)
    monkeypatch.setattr(bjm, "validate_development_winner", lambda value, supplied_spec: value)
    monkeypatch.setattr(
        bjm, "validate_blind_unlock", lambda value, supplied_spec, supplied_winner: value
    )
    monkeypatch.setattr(
        bjm,
        "validate_blind_manifest_receipt",
        lambda value, supplied_spec, supplied_winner, supplied_unlock: value,
    )
    monkeypatch.setattr(
        bjm,
        "validate_corpus_manifest",
        lambda value: {"corpus_root_hash": spec["corpus_snapshot_hash"]},
    )
    monkeypatch.setattr(
        bjm,
        "load_frozen_entity_text",
        lambda export, manifest: {"ent": {"title": "T", "body": "B"}},
    )
    monkeypatch.setattr(
        bjm,
        "load_bundles",
        lambda directory,
        value,
        expected_contenders=None,
        expected_blind_binding=None,
        expected_query_binding=None,
        expected_representation_root=None: {name: {} for name in expected_contenders or []},
    )
    monkeypatch.setattr(
        bjm,
        "build_pools",
        lambda queries, bundles, entity_text, pool_top_n=20: {
            query["id"]: {
                "ent": {"title": "T", "full_content": "B", "ground_truth_forced_include": False}
            }
            for query in queries
        },
    )
    monkeypatch.setattr(bs, "authorize_blind_file", lambda *args, **kwargs: payload)


def test_build_blind_judging_matrix_is_authorized_and_opaque(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    matrix = bjm.build_blind_judging_matrix(
        spec_path=tmp_path / "spec.json",
        retrieval_runs_dir=tmp_path / "runs",
        corpus_manifest_path=tmp_path / "manifest.json",
        corpus_export_path=tmp_path / "export.json",
        queries_blind_path=tmp_path / "sealed.json",
        vault_dir=tmp_path,
        winner_path=tmp_path / "winner.json",
        unlock_path=tmp_path / "unlock.json",
        manifest_receipt_path=tmp_path / "receipt.json",
    )
    assert bs.validate_signed_artifact(matrix, kind="JudgingMatrix")["query_count"] == 800
    assert len(matrix["pools"]) == 800
    assert "sealed text" not in json.dumps(matrix)


@pytest.mark.parametrize(
    ("query_count", "split", "error"),
    [(799, "blind", "exactly 800"), (800, "dev", "non-blind")],
)
def test_build_blind_judging_matrix_rejects_invalid_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, query_count: int, split: str, error: str
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path, query_count=query_count, split=split)
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    with pytest.raises(bjm.JudgingMatrixBuildError, match=error):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


def test_build_blind_judging_matrix_rejects_manifest_from_other_corpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    manifest = json.loads(payload)
    manifest["corpus_fingerprint"] = "d" * 64
    manifest["manifest_fingerprint"] = bs.fingerprint(
        {key: value for key, value in manifest.items() if key != "manifest_fingerprint"}
    )
    _patch_blind_gateway(monkeypatch, tmp_path, json.dumps(manifest).encode(), spec, winner)
    with pytest.raises(bjm.JudgingMatrixBuildError, match="corpus_fingerprint"):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [("provenance", "provenance"), ("canonical", "canonical key set")],
)
def test_build_blind_judging_matrix_rejects_schema_or_provenance_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    manifest = json.loads(payload)
    if mutation == "provenance":
        manifest["provenance"] = None
    else:
        manifest["queries"][0].pop("length_bucket")
        manifest["queries_fingerprint"] = bs.fingerprint(manifest["queries"])
    unsigned = dict(manifest)
    unsigned.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = bs.fingerprint(unsigned)
    payload = json.dumps(manifest).encode()
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    with pytest.raises(bjm.JudgingMatrixBuildError, match=error):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


def test_build_blind_judging_matrix_rejects_receipt_swapped_after_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    monkeypatch.setattr(
        bjm,
        "validate_blind_manifest_receipt",
        lambda value, supplied_spec, supplied_winner, supplied_unlock: {
            "artifact_fingerprint": "r" * 64,
            "file_sha256": "s" * 64,
        },
    )
    with pytest.raises(bjm.JudgingMatrixBuildError, match="does not match authorized payload"):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


def test_build_blind_judging_matrix_rejects_corpus_manifest_from_other_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    monkeypatch.setattr(
        bjm,
        "validate_corpus_manifest",
        lambda value: {"corpus_root_hash": "d" * 64},
    )
    with pytest.raises(bjm.JudgingMatrixBuildError, match="representation root"):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )


@pytest.mark.parametrize("failure", ["bundle", "corpus"])
def test_build_blind_judging_matrix_propagates_invalid_bundle_or_corpus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    spec, winner, payload = _blind_gateway_setup(tmp_path)
    _patch_blind_gateway(monkeypatch, tmp_path, payload, spec, winner)
    if failure == "bundle":
        monkeypatch.setattr(
            bjm,
            "load_bundles",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                bjm.JudgingMatrixBuildError("invalid blind bundle")
            ),
        )
    else:
        monkeypatch.setattr(
            bjm,
            "load_frozen_entity_text",
            lambda *args: (_ for _ in ()).throw(
                bjm.JudgingMatrixBuildError("invalid corpus export")
            ),
        )
    with pytest.raises(bjm.JudgingMatrixBuildError, match="invalid"):
        bjm.build_blind_judging_matrix(
            spec_path=tmp_path / "spec.json",
            retrieval_runs_dir=tmp_path / "runs",
            corpus_manifest_path=tmp_path / "manifest.json",
            corpus_export_path=tmp_path / "export.json",
            queries_blind_path=tmp_path / "sealed.json",
            vault_dir=tmp_path,
            winner_path=tmp_path / "winner.json",
            unlock_path=tmp_path / "unlock.json",
            manifest_receipt_path=tmp_path / "receipt.json",
        )
