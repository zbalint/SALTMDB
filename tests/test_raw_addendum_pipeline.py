import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/benchmarking"))

from evaluate_development_fusion import DevelopmentFusionError, load_inputs  # noqa: E402
from judge_pool import ADDENDUM_JUDGES, build_judge_packets, ingest_labels  # noqa: E402
from build_development_parent_binding import build_parent_binding  # noqa: E402
from merge_judgments import (  # noqa: E402
    addendum_binding,
    apply_arbitration_results,
    build_arbitration_packet,
    merge_all_judgments,
    merged_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
ADDENDUM = ROOT / "reports/gate-d-raw-lexical-judging-matrix-addendum-20260824.json"
QUERIES = ROOT / "scratch/eval_results/accuracy-bakeoff-20260812/gate_d_devrun2/queries_dev.json"
RUN = ROOT / "scratch/eval_results/accuracy-bakeoff-20260812/gate_d_devrun2"
CORPUS_ROOT = ROOT / "scratch/eval_results/accuracy-bakeoff-20260812"
RAW_BUNDLE = ROOT / "reports/gate-d-raw-lexical-development-20260824.json"
DENSE_BUNDLE = ROOT / "reports/gate-d-bge-small-development-20260824.json"
PARENT_BINDING = ROOT / "reports/gate-d-development-parent-binding-20260824.json"
PARENT_SPEC = RUN / "bakeoff_spec.json"
PARENT_MATRIX = RUN / "judging_matrix.json"
PARENT_LABELS = RUN / "merged" / "merged_dev_labels.json"
PARENT_MAPPINGS = [
    RUN / "judge_mappings" / f"judge_mapping_{judge}_dev.json"
    for judge in ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
]
PARENT_PACKETS = [
    RUN / "judge_packets" / f"judge_packet_{judge}.json"
    for judge in ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
]
PARENT_RAW_LABELS = [
    RUN / "ingested_labels" / f"{judge}_dev_labels.json"
    for judge in ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
]
REAL_DEV_ARTIFACTS = (
    ADDENDUM,
    QUERIES,
    RAW_BUNDLE,
    DENSE_BUNDLE,
    PARENT_BINDING,
    PARENT_SPEC,
    PARENT_MATRIX,
    PARENT_LABELS,
    *PARENT_MAPPINGS,
    *PARENT_PACKETS,
    *PARENT_RAW_LABELS,
)


@pytest.mark.skipif(
    not all(path.exists() for path in REAL_DEV_ARTIFACTS),
    reason="optional real development artifacts are not checked out",
)
def test_raw_addendum_packets_ingest_merge_arbitrate_end_to_end(tmp_path):
    matrix = json.loads(ADDENDUM.read_text())
    all_queries = json.loads(QUERIES.read_text())["queries"]
    queries = [query for query in all_queries if query["id"] in matrix["pools"]]
    target_query = next(iter(matrix["pools"]))
    target_entity = next(iter(matrix["pools"][target_query]))
    artifacts = []
    raw_paths = []
    for judge_index, judge in enumerate(ADDENDUM_JUDGES):
        packet, mapping = build_judge_packets(queries, matrix, judge, "dev", base_seed=11)
        response = {"labels": []}
        for task_id, task in mapping["tasks"].items():
            for candidate_id in task["candidate_ids"]:
                entity_id = task["candidate_entity_ids"][candidate_id]
                response["labels"].append(
                    {
                        "task_id": task_id,
                        "candidate_id": candidate_id,
                        "grade": judge_index if entity_id == target_entity else 0,
                    }
                )
        response_path = tmp_path / f"{judge}.response.json"
        mapping_path = tmp_path / f"{judge}.mapping.json"
        output_path = tmp_path / f"{judge}.labels.json"
        response_path.write_text(json.dumps(response))
        mapping_path.write_text(json.dumps(mapping))
        artifacts.append(ingest_labels(response_path, mapping_path, output_path, judge))
        raw_paths.append(output_path)
    assert all(len(artifact["labels"]) == 241 for artifact in artifacts)
    merged = merge_all_judgments(queries, artifacts, matrix, judges=ADDENDUM_JUDGES)
    arbitration_packet = build_arbitration_packet(merged, queries, matrix)
    assert arbitration_packet["tasks"]
    apply_arbitration_results(
        merged,
        {
            "adjudicator": "agent_eval_adjudicator",
            "labels": [
                {"task_id": task["task_id"], "grade": 2} for task in arbitration_packet["tasks"]
            ],
        },
    )
    final = merged_artifact(
        merged,
        artifacts,
        queries,
        judges=ADDENDUM_JUDGES,
        binding=addendum_binding(matrix),
    )
    assert final["kind"] == "DevelopmentJudgingAddendumLabels"
    assert len(final["labels"]) == 241
    assert final["addendum_binding"]["artifact_fingerprint"] == matrix["artifact_fingerprint"]
    assert any(label["arbitrated_grade"] == 2 for label in final["labels"])
    final_path = tmp_path / "merged-addendum.json"
    final_path.write_text(json.dumps(final))
    inputs = load_inputs(
        RUN / "queries_dev.json",
        RUN / "judging_matrix.json",
        RUN / "merged" / "merged_dev_labels.json",
        RAW_BUNDLE,
        DENSE_BUNDLE,
        RUN / "bakeoff_spec.json",
        CORPUS_ROOT / "corpus_representation_manifest.json",
        CORPUS_ROOT / "corpus_export.json",
        ADDENDUM,
        final_path,
        PARENT_BINDING,
        raw_paths,
    )
    assert len(inputs["queries"]) == 400
    assert sum(map(len, inputs["relevance"].values())) == 33_997
    stale = dict(final)
    stale["addendum_binding"] = dict(final["addendum_binding"])
    stale["addendum_binding"]["artifact_fingerprint"] = "0" * 64
    stale.pop("fingerprint")
    stale["fingerprint"] = hashlib.sha256(
        json.dumps(stale, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    stale_path = tmp_path / "stale-addendum.json"
    stale_path.write_text(json.dumps(stale))
    with pytest.raises(DevelopmentFusionError, match="exact frozen-input binding"):
        load_inputs(
            RUN / "queries_dev.json",
            RUN / "judging_matrix.json",
            RUN / "merged" / "merged_dev_labels.json",
            RAW_BUNDLE,
            DENSE_BUNDLE,
            RUN / "bakeoff_spec.json",
            CORPUS_ROOT / "corpus_representation_manifest.json",
            CORPUS_ROOT / "corpus_export.json",
            ADDENDUM,
            stale_path,
            PARENT_BINDING,
            raw_paths,
        )


@pytest.mark.skipif(
    not all(path.exists() for path in REAL_DEV_ARTIFACTS),
    reason="optional real development artifacts are not checked out",
)
def test_historical_parent_binding_rejects_tampered_mapping(tmp_path):
    tampered_path = tmp_path / "tampered-mapping.json"
    tampered = json.loads(PARENT_MAPPINGS[0].read_text())
    tampered["queries_fingerprint"] = "0" * 64
    tampered.pop("fingerprint")
    tampered["fingerprint"] = hashlib.sha256(
        json.dumps(tampered, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    tampered_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="mapping binding"):
        build_parent_binding(
            spec_path=PARENT_SPEC,
            queries_path=QUERIES,
            matrix_path=PARENT_MATRIX,
            labels_path=PARENT_LABELS,
            mapping_paths=[tampered_path, *PARENT_MAPPINGS[1:]],
            packet_paths=PARENT_PACKETS,
            ingested_label_paths=PARENT_RAW_LABELS,
        )
