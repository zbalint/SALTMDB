import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "benchmarking"))

from bakeoff_state import sign_artifact  # noqa: E402
from build_development_judging_addendum import build_addendum  # noqa: E402
from build_development_parent_binding import build_parent_binding  # noqa: E402
from build_development_judging_addendum import _load_queries as load_addendum_queries  # noqa: E402
from build_development_parent_binding import _load_query_manifest  # noqa: E402
from build_evaluation_queries import artifact_fingerprint  # noqa: E402
from evaluation_artifacts import build_provenance  # noqa: E402
from judge_pool import build_judge_packets, ingest_labels  # noqa: E402
from merge_judgments import merge_all_judgments, merged_artifact  # noqa: E402
from evaluate_development_fusion import (  # noqa: E402
    DevelopmentFusionError,
    _load_addendum_relevance,
)
from fixtures.development_fusion_synthetic import (  # noqa: E402
    build_addendum_contract,
    build_lexical_snapshot_receipt,
)
from run_retrieval_bakeoff import validate_lexical_snapshot_receipt  # noqa: E402


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _write_synthetic_query_manifest(path: Path, *, corpus_root: str) -> None:
    queries = [
        {
            "id": f"q{index:03d}",
            "query": f"synthetic query {index}",
            "lang": "English",
            "category": "body_text",
            "subtype": "synthetic",
            "split": "dev",
            "source_entity_ids": [],
            "topic_family_id": f"family-{index:03d}",
            "length_bucket": "short",
            "provenance": "synthetic",
        }
        for index in range(400)
    ]
    query_fp = artifact_fingerprint(queries)
    manifest = {
        "schema_version": 1,
        "kind": "DevelopmentQueryManifest",
        "queries": queries,
        "queries_fingerprint": query_fp,
        "corpus_fingerprint": corpus_root,
        "provenance": build_provenance(
            commit="a" * 64,
            corpus=corpus_root,
            query=query_fp,
            seed=0,
            config="b" * 64,
            rubric="c" * 64,
            machine="d" * 64,
        ),
    }
    manifest["manifest_fingerprint"] = _sha(manifest)
    path.write_text(json.dumps(manifest))


def test_query_builders_read_manifest_bytes_once(tmp_path, monkeypatch):
    query_path = tmp_path / "queries.json"
    corpus_root = "e" * 64
    _write_synthetic_query_manifest(query_path, corpus_root=corpus_root)
    original_read_bytes = Path.read_bytes
    reads = []

    def counted_read_bytes(path):
        if path == query_path:
            reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    queries, _ = load_addendum_queries(
        query_path, expected_spec={"corpus_snapshot_hash": corpus_root}
    )
    assert len(queries) == 400
    assert len(reads) == 1
    _, payload = _load_query_manifest(query_path, corpus_root_hash=corpus_root)
    assert payload == original_read_bytes(query_path)
    assert len(reads) == 2


def test_real_addendum_and_parent_builders_use_only_temp_synthetic_artifacts(  # noqa: PLR0915
    tmp_path,
):
    corpus_ids = [
        f"new-{index:03d}-{candidate}"
        for index in range(172)
        for candidate in range(2 if index < 69 else 1)
    ]
    corpus_rows = []
    export_rows = []
    for entity_id in corpus_ids:
        title = f"Synthetic {entity_id}"
        body = f"Body for {entity_id}"
        title_hash = hashlib.sha256(title.encode()).hexdigest()
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        source_hash = hashlib.sha256(f"{title}\n\n{body}".encode()).hexdigest()
        corpus_rows.append(
            {
                "entity_id": entity_id,
                "title_hash": title_hash,
                "body_hash": body_hash,
                "source_hash": source_hash,
                "chunk_hashes": [body_hash],
            }
        )
        export_rows.append(
            {"entity_id": entity_id, "title": title, "body": body, "source_hash": source_hash}
        )
    corpus_root = _sha(
        {
            "eligible_ids": sorted(corpus_ids),
            "entities": sorted(corpus_rows, key=lambda row: row["entity_id"]),
            "representation_version": "synthetic-v1",
        }
    )
    corpus_manifest = sign_artifact(
        "CorpusRepresentationManifest",
        {
            "eligible_ids": sorted(corpus_ids),
            "entities": sorted(corpus_rows, key=lambda row: row["entity_id"]),
            "representation_version": "synthetic-v1",
            "corpus_root_hash": corpus_root,
        },
    )
    corpus_manifest_path = tmp_path / "corpus_manifest.json"
    corpus_manifest_path.write_text(json.dumps(corpus_manifest))
    corpus_export_path = tmp_path / "corpus_export.json"
    corpus_export_path.write_text(json.dumps({"entities": export_rows}))

    spec_payload = {
        "run_id": "synthetic",
        "commit": "a" * 64,
        "corpus_snapshot_hash": corpus_root,
        "query_slots_hash": "b" * 64,
        "query_prompt_hash": "c" * 64,
        "rubric_hash": "d" * 64,
        "configuration_hash": "e" * 64,
        "seeds": {"synthetic": 0},
        "machine_fingerprint": "f" * 64,
        "contenders": ["lexical:bm25", "dense:synthetic:entity"],
        "hyperparameter_grids": {"synthetic": [1]},
        "required_metrics": [
            "macro_positive_ndcg_at_10",
            "grade2_recall_at_20",
            "same_specific_fact_grade2_top1",
            "exact_safety",
            "keyword_safety",
            "strict_negative_safety",
            "warm_latency_p50_seconds",
            "warm_latency_p95_seconds",
            "benchmark_failures",
        ],
        "software_versions": {"synthetic": "1"},
        "holm_comparison_family": ["synthetic"],
        "split_targets": {"dev": 400, "blind": 800},
        "facet_targets": {
            split: {
                facet: (1 + (391 if split == "dev" else 791) if facet == "exact_sentence" else 1)
                for facet in (
                    "exact_sentence",
                    "keyword",
                    "typo",
                    "short_memory",
                    "long_body",
                    "current_vs_superseded",
                    "close_sibling",
                    "multilingual",
                    "strict_negative",
                )
            }
            for split in ("dev", "blind")
        },
    }
    spec = sign_artifact("BakeoffSpec", spec_payload)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    query_path = tmp_path / "queries.json"
    _write_synthetic_query_manifest(query_path, corpus_root=corpus_root)
    queries = json.loads(query_path.read_text())["queries"]
    old_pools = {query["id"]: {f"old-{index:03d}": {}} for index, query in enumerate(queries)}
    matrix = sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": spec["artifact_fingerprint"],
            "corpus_root_hash": corpus_root,
            "query_count": 400,
            "pools": old_pools,
        },
    )
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix))
    missing = {
        query["id"]: [f"new-{index:03d}-{candidate}" for candidate in range(2 if index < 69 else 1)]
        for index, query in enumerate(queries[:172])
    }
    worklist = sign_artifact(
        "RawLexicalPoolEscapeWorklist",
        {
            "missing_pairs_by_query": missing,
            "counts": {"affected_query_count": 172, "missing_pair_count": 241},
        },
    )
    worklist_path = tmp_path / "worklist.json"
    worklist_path.write_text(json.dumps(worklist))
    raw_bundle = sign_artifact(
        "RetrievalRunBundle",
        {
            "spec_fingerprint": spec["artifact_fingerprint"],
            "query_manifest_fingerprint": json.loads(query_path.read_text())[
                "manifest_fingerprint"
            ],
            "query_manifest_file_sha256": hashlib.sha256(query_path.read_bytes()).hexdigest(),
            "query_split": "dev",
            "cell": {
                "kind": "lexical",
                "channel": "bm25_raw_production",
                "lexical_policy": "raw_production",
                "production_faithful": True,
                "representation_root": corpus_root,
                "lexical_snapshot_receipt_fingerprint": "1" * 64,
                "lexical_snapshot_db_sha256": "2" * 64,
            },
            "failures": [],
            "results": [
                {
                    "query_id": query["id"],
                    "top20": [
                        {"entity_id": f"old-{index:03d}"},
                        *[{"entity_id": entity_id} for entity_id in missing.get(query["id"], [])],
                    ],
                }
                for index, query in enumerate(queries)
            ],
        },
    )
    raw_bundle_path = tmp_path / "raw_bundle.json"
    raw_bundle_path.write_text(json.dumps(raw_bundle))
    addendum = build_addendum(
        raw_bundle_path=raw_bundle_path,
        worklist_path=worklist_path,
        judging_matrix_path=matrix_path,
        spec_path=spec_path,
        query_manifest_path=query_path,
        corpus_manifest_path=corpus_manifest_path,
        corpus_export_path=corpus_export_path,
    )
    assert addendum["query_count"] == 172
    assert addendum["missing_pair_count"] == 241

    raw_paths = []
    mappings = []
    packets = []
    raw_artifacts = []
    for role in ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c"):
        packet, mapping = build_judge_packets(queries, matrix, role, "dev", base_seed=0)
        packet_path = tmp_path / f"{role}.packet.json"
        mapping_path = tmp_path / f"{role}.mapping.json"
        packet_path.write_text(json.dumps(packet))
        mapping_path.write_text(json.dumps(mapping))
        response = {
            "labels": [
                {"task_id": task_id, "candidate_id": candidate_id, "grade": 0}
                for task_id, task in mapping["tasks"].items()
                for candidate_id in task["candidate_ids"]
            ]
        }
        response_path = tmp_path / f"{role}.response.json"
        response_path.write_text(json.dumps(response))
        raw_path = tmp_path / f"{role}.labels.json"
        raw_artifacts.append(ingest_labels(response_path, mapping_path, raw_path, role))
        raw_paths.append(raw_path)
        mappings.append(mapping_path)
        packets.append(packet_path)
    merged = merged_artifact(
        merge_all_judgments(queries, raw_artifacts, matrix),
        raw_artifacts,
        queries,
    )
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(merged))
    parent = build_parent_binding(
        spec_path=spec_path,
        queries_path=query_path,
        matrix_path=matrix_path,
        labels_path=labels_path,
        mapping_paths=mappings,
        packet_paths=packets,
        ingested_label_paths=raw_paths,
    )
    assert parent["kind"] == "DevelopmentParentBinding"


def test_committed_synthetic_receipt_and_addendum_contracts(tmp_path):
    db_path = tmp_path / "synthetic.sqlite"
    db_path.write_bytes(b"synthetic-db")
    receipt = build_lexical_snapshot_receipt(db_path, corpus_root_hash="d" * 64)
    assert validate_lexical_snapshot_receipt(
        receipt, db_path=db_path, expected_corpus_root="d" * 64
    )["db_sha256_informational"]

    contract = build_addendum_contract()
    pools, relevance, _ = _load_addendum_relevance(
        contract["addendum"],
        contract["merged"],
        parent_matrix=contract["parent"],
        query_ids=contract["query_ids"],
        old_pairs=contract["old_pairs"],
        raw_artifacts=contract["raw_artifacts"],
    )
    assert len(pools) == len(relevance) == 172
    assert sum(map(len, relevance.values())) == 241

    tampered = dict(contract["merged"])
    tampered["labels"] = list(tampered["labels"])
    tampered["labels"][0] = dict(tampered["labels"][0])
    tampered["labels"][0]["median_grade"] = 2
    tampered.pop("fingerprint")
    tampered["fingerprint"] = _sha(tampered)
    with pytest.raises(DevelopmentFusionError, match="disagrees with raw grades"):
        _load_addendum_relevance(
            contract["addendum"],
            tampered,
            parent_matrix=contract["parent"],
            query_ids=contract["query_ids"],
            old_pairs=contract["old_pairs"],
            raw_artifacts=contract["raw_artifacts"],
        )
    agreement_tampered = dict(contract["merged"])
    agreement_tampered["agreement"] = list(agreement_tampered["agreement"])
    agreement_tampered["agreement"][0] = dict(agreement_tampered["agreement"][0])
    agreement_tampered["agreement"][0]["n"] = 0
    agreement_tampered.pop("fingerprint")
    agreement_tampered["fingerprint"] = _sha(agreement_tampered)
    with pytest.raises(DevelopmentFusionError, match="agreement"):
        _load_addendum_relevance(
            contract["addendum"],
            agreement_tampered,
            parent_matrix=contract["parent"],
            query_ids=contract["query_ids"],
            old_pairs=contract["old_pairs"],
            raw_artifacts=contract["raw_artifacts"],
        )


def test_synthetic_parent_binding_records_all_three_roles(tmp_path):
    from evaluate_development_fusion import _validate_parent_binding

    spec = {"artifact_fingerprint": "a" * 64, "corpus_snapshot_hash": "d" * 64}
    pools = {"q": {"e": {}}}
    matrix = {"artifact_fingerprint": "m" * 64, "pools": pools}
    labels = {
        "fingerprint": "l" * 64,
        "labels": [{"query_id": "q", "candidate_id": "e"}],
        "raw_labels_fingerprint": "r" * 64,
    }
    queries_path = tmp_path / "queries.json"
    labels_path = tmp_path / "labels.json"
    queries_path.write_text("query-bytes")
    labels_path.write_text("label-bytes")
    query_fp = "q" * 64
    pool_fp = _sha(pools)
    records = [
        {
            "judge": role,
            "queries_fingerprint": query_fp,
            "matrix_pools_fingerprint": pool_fp,
            "packet_fingerprint": f"p{index}".ljust(64, "0"),
            "fingerprint": f"m{index}".ljust(64, "0"),
            "coverage_fingerprint": "c" * 64,
        }
        for index, role in enumerate(
            ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
        )
    ]
    envelope = sign_artifact(
        "DevelopmentParentBinding",
        {
            "development_only": True,
            "evidence_tier": "legacy_parent_compatibility_proof",
            "binding": {
                "parent_matrix_fingerprint": matrix["artifact_fingerprint"],
                "spec_fingerprint": spec["artifact_fingerprint"],
                "query_manifest_fingerprint": query_fp,
                "query_manifest_file_sha256": hashlib.sha256(queries_path.read_bytes()).hexdigest(),
                "query_split": "dev",
                "historical_labels_fingerprint": labels["fingerprint"],
                "historical_labels_file_sha256": hashlib.sha256(
                    labels_path.read_bytes()
                ).hexdigest(),
                "corpus_root_hash": spec["corpus_snapshot_hash"],
                "label_pair_count": 1,
                "canonical_queries_fingerprint": query_fp,
                "canonical_matrix_pools_fingerprint": pool_fp,
                "canonical_raw_labels_fingerprint": labels["raw_labels_fingerprint"],
                "mapping_records": records,
                "packet_records": [
                    {"judge": row["judge"], "fingerprint": row["packet_fingerprint"]}
                    for row in records
                ],
                "ingested_label_records": [
                    {
                        "judge": row["judge"],
                        "mapping_fingerprint": row["fingerprint"],
                        "coverage_fingerprint": row["coverage_fingerprint"],
                    }
                    for row in records
                ],
                "judge_roles": [row["judge"] for row in records],
            },
        },
    )
    envelope_path = tmp_path / "parent-binding.json"
    envelope_path.write_text(json.dumps(envelope))
    _validate_parent_binding(
        envelope_path,
        spec=spec,
        matrix=matrix,
        labels=labels,
        queries_path=queries_path,
        labels_path=labels_path,
        query_fingerprint=query_fp,
        canonical_query_fingerprint=query_fp,
        query_file_sha256=hashlib.sha256(queries_path.read_bytes()).hexdigest(),
    )
