import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "benchmarking"))

from evaluate_development_fusion import (  # noqa: E402
    DevelopmentFusionError,
    _channel_maps,
    _comparison_deltas,
    evaluate_development,
    _exact_title_parity,
    _metric_vector,
    _load_addendum_relevance,
    _pairwise_cv_rankings,
    _paired_ndcg_outcome,
    _rrf_ranking,
    _selection_recommendation,
    _score_fusion_ranking,
    _validate_signed_bundle,
    load_inputs,
)
from bakeoff_state import sign_artifact  # noqa: E402
from judge_pool import ADDENDUM_JUDGES, judge_version_fingerprint  # noqa: E402


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "scratch"
    / "eval_results"
    / "accuracy-bakeoff-20260812"
    / "gate_d_devrun2"
)


def _fixture_paths():
    return (
        FIXTURE / "queries_dev.json",
        FIXTURE / "judging_matrix.json",
        FIXTURE / "merged" / "merged_dev_labels.json",
        FIXTURE / "retrieval_runs" / "lexical__bm25__entity.json",
        FIXTURE / "retrieval_runs" / "dense__BAAI__bge-small-en-v1.5__entity.json",
        FIXTURE / "bakeoff_spec.json",
        Path(__file__).resolve().parents[1]
        / "scratch"
        / "eval_results"
        / "accuracy-bakeoff-20260812"
        / "corpus_representation_manifest.json",
        Path(__file__).resolve().parents[1]
        / "scratch"
        / "eval_results"
        / "accuracy-bakeoff-20260812"
        / "corpus_export.json",
    )


REAL_FUSION_INPUTS = tuple(_fixture_paths())
REAL_FUSION_AVAILABLE = all(path.exists() for path in REAL_FUSION_INPUTS)


def test_bm25_transform_is_lower_is_better_to_higher_is_better():
    union, lexical_ranks, dense_ranks, lexical_scores, dense_scores, latency = _channel_maps(
        {
            "top20": [
                {"entity_id": "a", "raw_bm25_score": -10.0},
                {"entity_id": "b", "raw_bm25_score": -2.0},
            ],
            "latency_ms": 1.0,
        },
        {"top20": [{"entity_id": "b", "score": 0.7}], "latency_ms": 2.0},
    )
    assert union == ["a", "b"]
    assert lexical_scores == {"a": 10.0, "b": 2.0}
    assert dense_scores == {"b": 0.7}
    assert latency == 3.0
    assert _rrf_ranking(union, lexical_ranks, dense_ranks) == ["b", "a"]


def test_score_fusion_uses_fixed_weights_and_deterministic_ties():
    features = {
        "a": {"bm25_score": 1.0, "dense_entity_score": 0.0},
        "b": {"bm25_score": 0.0, "dense_entity_score": 1.0},
    }
    assert _score_fusion_ranking(features, 1.0, 1.0) == ["a", "b"]
    assert _score_fusion_ranking(features, 0.5, 1.0) == ["b", "a"]
    assert _score_fusion_ranking(features, 0.5, 1.0, candidate_ids=["a"]) == ["a"]


def test_exact_title_parity_is_same_and_does_not_inject_absent_candidate():
    corpus_titles = {"a": "Exact Title", "b": "Other"}
    ranking, diagnostic = _exact_title_parity(["b"], "Exact Title", corpus_titles)
    assert ranking == ["a"]
    assert diagnostic["matched"] is True
    assert diagnostic["injected_out_of_union"] is True
    ranking, diagnostic = _exact_title_parity(["b"], " exact title ", corpus_titles)
    assert ranking == ["b"]
    assert diagnostic["matched"] is False
    assert diagnostic["collision_count"] == 0


def test_exact_title_parity_is_byte_exact_and_collision_safe():
    assert _exact_title_parity(["b"], "Exact Title ", {"a": "Exact Title"})[0] == ["b"]
    collision = _exact_title_parity(
        ["b", "a"], "Exact Title", {"a": "Exact Title", "b": "Exact Title"}
    )
    assert collision[0] == ["b", "a"]
    assert collision[1]["collision_count"] == 2
    first = _exact_title_parity(["b"], "Exact Title", {"a": "Exact Title"})[0]
    second = _exact_title_parity(["a", "b"], "Exact Title", {"a": "Exact Title"})[0]
    assert first == second == ["a"]


def test_evaluate_exact_title_outside_union_keeps_rerank_pool_valid():
    queries = {}
    relevance = {}
    lexical = {}
    dense = {}
    for index in range(5):
        query_id = f"q{index}"
        queries[query_id] = {
            "query": "Unique External" if index == 0 else f"query {index}",
            "topic_family_id": f"family-{index}",
            "category": "exact_sentence",
            "source_entity_ids": ["outside"] if index == 0 else [],
        }
        candidates = [f"a{index}", f"b{index}"]
        relevance[query_id] = {
            candidate: int(candidate == candidates[0]) for candidate in candidates
        }
        if index == 0:
            relevance[query_id]["outside"] = 2
        lexical[query_id] = {
            "top20": [
                {"entity_id": candidates[0], "raw_bm25_score": -2.0},
                {"entity_id": candidates[1], "raw_bm25_score": -1.0},
            ],
            "latency_ms": 1.0,
        }
        dense[query_id] = {
            "top20": [
                {"entity_id": candidates[1], "score": 0.8},
                {"entity_id": candidates[0], "score": 0.2},
            ],
            "latency_ms": 2.0,
        }
    result = evaluate_development(
        {
            "queries": queries,
            "relevance": relevance,
            "lexical": lexical,
            "dense": dense,
            "corpus_titles": {"outside": "Unique External"},
            "fingerprints": {},
        }
    )
    for rankings in result["query_rankings"].values():
        assert rankings["q0"] == ["outside"]
    missing_relevance = deepcopy(relevance)
    missing_relevance["q0"].pop("outside")
    with pytest.raises(DevelopmentFusionError, match="lacks judged relevance"):
        evaluate_development(
            {
                "queries": queries,
                "relevance": missing_relevance,
                "lexical": lexical,
                "dense": dense,
                "corpus_titles": {"outside": "Unique External"},
                "fingerprints": {},
            }
        )


@pytest.mark.skipif(
    not REAL_FUSION_AVAILABLE, reason="optional real development artifacts unavailable"
)
def test_addendum_label_merge_requires_exact_pairs_and_three_judge_provenance():
    addendum_path = (
        Path(__file__).resolve().parents[1]
        / "reports/gate-d-raw-lexical-judging-matrix-addendum-20260824.json"
    )
    if not addendum_path.exists():
        pytest.skip("raw lexical addendum is not checked out")
    parent = json.loads((FIXTURE / "judging_matrix.json").read_text())
    addendum = json.loads(addendum_path.read_text())
    pairs = [
        (query_id, candidate_id)
        for query_id, candidates in addendum["pools"].items()
        for candidate_id in candidates
    ]
    labels = [
        {
            "query_id": query_id,
            "candidate_id": candidate_id,
            "raw_grades": dict.fromkeys(
                ("agent_eval_judge_raw_a", "agent_eval_judge_raw_b", "agent_eval_judge_raw_c"),
                0,
            ),
            "median_grade": 0,
            "escalated": False,
            "arbitrated_grade": None,
            "final_grade": 0,
        }
        for query_id, candidate_id in pairs
    ]
    artifact = {
        "schema_version": 1,
        "kind": "DevelopmentJudgingAddendumLabels",
        "judges": [
            "agent_eval_judge_raw_a",
            "agent_eval_judge_raw_b",
            "agent_eval_judge_raw_c",
        ],
        "adjudicator": "agent_eval_adjudicator",
        "judge_version": "stage1-grades-0-1-2-gains-0-1-3-v1",
        "raw_labels_fingerprint": "raw-labels",
        "agreement": [
            {
                "judges": [left, right],
                "n": 241,
                "exact_agreement_rate": 1.0,
                "cohens_kappa": 1.0,
                "confusion": {"0:0": 241},
            }
            for index, left in enumerate(ADDENDUM_JUDGES)
            for right in ADDENDUM_JUDGES[index + 1 :]
        ],
        "escalation": {"count": 0, "total": 241, "rate": 0.0},
        "labels": labels,
        "addendum_binding": {
            key: addendum[key]
            for key in (
                "artifact_fingerprint",
                "spec_fingerprint",
                "query_manifest_fingerprint",
                "query_manifest_file_sha256",
                "query_split",
                "corpus_root_hash",
                "corpus_manifest_fingerprint",
                "corpus_export_file_sha256",
                "raw_bundle_fingerprint",
                "worklist_fingerprint",
            )
        },
    }
    import hashlib

    artifact["fingerprint"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    raw_artifacts = []
    for role in (
        "agent_eval_judge_raw_a",
        "agent_eval_judge_raw_b",
        "agent_eval_judge_raw_c",
    ):
        raw = {
            "schema_version": 1,
            "kind": "RawJudgmentLabels",
            "judge": role,
            "judge_version": "stage1-grades-0-1-2-gains-0-1-3-v1",
            "judge_version_fingerprint": judge_version_fingerprint(ADDENDUM_JUDGES),
            "judge_role_set": "raw_addendum",
            "matrix_binding": artifact["addendum_binding"],
            "label_count": 241,
            "labels": [
                {"query_id": query_id, "candidate_id": candidate_id, "grade": 0}
                for query_id, candidate_id in pairs
            ],
        }
        raw["fingerprint"] = hashlib.sha256(
            json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        raw_artifacts.append(raw)
    artifact["raw_labels_fingerprint"] = hashlib.sha256(
        json.dumps(
            raw_artifacts, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    artifact.pop("fingerprint")
    artifact["fingerprint"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    old_pairs = {
        (query_id, candidate_id)
        for query_id, candidates in parent["pools"].items()
        for candidate_id in candidates
    }
    pools, relevance, _ = _load_addendum_relevance(
        addendum,
        artifact,
        parent_matrix=parent,
        query_ids=set(parent["pools"]),
        old_pairs=old_pairs,
        raw_artifacts=raw_artifacts,
    )
    assert len(pools) == len(relevance) == 172
    assert sum(map(len, relevance.values())) == 241
    broken = dict(artifact)
    broken["judges"] = ["agent_eval_judge_raw_a"]
    broken.pop("fingerprint")
    broken["fingerprint"] = hashlib.sha256(
        json.dumps(broken, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(DevelopmentFusionError, match="three raw lexical"):
        _load_addendum_relevance(
            addendum,
            broken,
            parent_matrix=parent,
            query_ids=set(parent["pools"]),
            old_pairs=old_pairs,
            raw_artifacts=raw_artifacts,
        )
    with pytest.raises(DevelopmentFusionError, match="exactly three raw"):
        _load_addendum_relevance(
            addendum,
            artifact,
            parent_matrix=parent,
            query_ids=set(parent["pools"]),
            old_pairs=old_pairs,
            raw_artifacts=raw_artifacts[:2],
        )
    with pytest.raises(DevelopmentFusionError, match="role or matrix binding"):
        _load_addendum_relevance(
            addendum,
            artifact,
            parent_matrix=parent,
            query_ids=set(parent["pools"]),
            old_pairs=old_pairs,
            raw_artifacts=[raw_artifacts[1], raw_artifacts[0], raw_artifacts[2]],
        )
    tampered_raw = dict(raw_artifacts[0])
    tampered_raw["labels"] = list(tampered_raw["labels"])
    tampered_raw["labels"][0] = dict(tampered_raw["labels"][0])
    tampered_raw["labels"][0]["grade"] = 1
    tampered_raw.pop("fingerprint")
    tampered_raw["fingerprint"] = hashlib.sha256(
        json.dumps(tampered_raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(DevelopmentFusionError, match="aggregate"):
        _load_addendum_relevance(
            addendum,
            artifact,
            parent_matrix=parent,
            query_ids=set(parent["pools"]),
            old_pairs=old_pairs,
            raw_artifacts=[tampered_raw, raw_artifacts[1], raw_artifacts[2]],
        )


def test_protected_paths_are_rejected(tmp_path):
    path = tmp_path / "blind" / "queries.json"
    path.parent.mkdir()
    path.write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(DevelopmentFusionError, match="protected"):
        load_inputs(path, path, path, path, path, path, path, path)


@pytest.mark.skipif(
    not REAL_FUSION_AVAILABLE, reason="optional real development artifacts unavailable"
)
def test_signed_query_tampering_is_rejected(tmp_path):
    query_path, matrix, labels, lexical, dense, spec, corpus_manifest, corpus_export = (
        _fixture_paths()
    )
    tampered = json.loads(query_path.read_text())
    tampered["queries"][0]["query"] += " tampered"
    tampered_path = tmp_path / "queries.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(DevelopmentFusionError, match="signed development query"):
        load_inputs(
            tampered_path, matrix, labels, lexical, dense, spec, corpus_manifest, corpus_export
        )


@pytest.mark.skipif(
    not REAL_FUSION_AVAILABLE, reason="optional real development artifacts unavailable"
)
def test_wrong_dense_model_is_rejected_even_when_resigned(tmp_path):
    query, matrix, labels, lexical, dense, spec, _corpus_manifest, _corpus_export = _fixture_paths()
    bundle = json.loads(dense.read_text())
    bundle["cell"]["model_id"] = "BAAI/bge-large-en-v1.5"
    unsigned = dict(bundle)
    unsigned.pop("artifact_fingerprint")
    rewritten = sign_artifact("RetrievalRunBundle", unsigned)
    dense_path = tmp_path / "dense.json"
    dense_path.write_text(json.dumps(rewritten), encoding="utf-8")
    query_doc = json.loads(query.read_text())
    matrix_doc = json.loads(matrix.read_text())
    binding = {
        "query_manifest_fingerprint": query_doc["manifest_fingerprint"],
        "query_manifest_file_sha256": __import__("hashlib").sha256(query.read_bytes()).hexdigest(),
        "query_split": "dev",
    }
    unsigned.update(binding)
    rewritten = sign_artifact("RetrievalRunBundle", unsigned)
    with pytest.raises(DevelopmentFusionError, match="pinned"):
        _validate_signed_bundle(
            rewritten,
            "dense",
            json.loads(spec.read_text())["artifact_fingerprint"],
            json.loads(spec.read_text())["corpus_snapshot_hash"],
            set(query_doc["queries"][i]["id"] for i in range(400)),
            matrix_doc["pools"],
            binding,
        )


@pytest.mark.skipif(
    not REAL_FUSION_AVAILABLE, reason="optional real development artifacts unavailable"
)
def test_wrong_lexical_channel_is_rejected_even_when_resigned(tmp_path):
    query, matrix, labels, lexical, dense, spec, _corpus_manifest, _corpus_export = _fixture_paths()
    bundle = json.loads(lexical.read_text())
    bundle["cell"]["channel"] = "bm25_only"
    unsigned = dict(bundle)
    unsigned.pop("artifact_fingerprint")
    rewritten = sign_artifact("RetrievalRunBundle", unsigned)
    lexical_path = tmp_path / "lexical.json"
    lexical_path.write_text(json.dumps(rewritten), encoding="utf-8")
    query_doc = json.loads(query.read_text())
    matrix_doc = json.loads(matrix.read_text())
    binding = {
        "query_manifest_fingerprint": query_doc["manifest_fingerprint"],
        "query_manifest_file_sha256": __import__("hashlib").sha256(query.read_bytes()).hexdigest(),
        "query_split": "dev",
    }
    unsigned.update(binding)
    rewritten = sign_artifact("RetrievalRunBundle", unsigned)
    with pytest.raises(DevelopmentFusionError, match="lexical"):
        _validate_signed_bundle(
            rewritten,
            "lexical",
            json.loads(spec.read_text())["artifact_fingerprint"],
            json.loads(spec.read_text())["corpus_snapshot_hash"],
            {row["id"] for row in query_doc["queries"]},
            matrix_doc["pools"],
            binding,
        )


@pytest.mark.skipif(
    not REAL_FUSION_AVAILABLE, reason="optional real development artifacts unavailable"
)
def test_wrong_bakeoff_spec_is_rejected_even_when_resigned(tmp_path):
    query, matrix, labels, lexical, dense, spec, _corpus_manifest, _corpus_export = _fixture_paths()
    frozen = json.loads(spec.read_text())
    frozen["run_id"] = "different-devrun"
    unsigned = dict(frozen)
    unsigned.pop("artifact_fingerprint")
    rewritten = sign_artifact("BakeoffSpec", unsigned)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(rewritten), encoding="utf-8")
    with pytest.raises(DevelopmentFusionError, match="run_id"):
        load_inputs(
            query, matrix, labels, lexical, dense, spec_path, _corpus_manifest, _corpus_export
        )


def _toy_pairwise_inputs():
    query_ids = [f"q{index}" for index in range(5)]
    family_ids = {query_id: f"family-{index}" for index, query_id in enumerate(query_ids)}
    features = {
        query_id: {
            "good": {
                "bm25_score": 1.0,
                "bm25_rank": 1.0,
                "dense_entity_score": 1.0,
                "dense_entity_rank": 1.0,
                "channel_agreement": 1.0,
            },
            "bad": {
                "bm25_score": 0.0,
                "bm25_rank": 0.5,
                "dense_entity_score": 0.0,
                "dense_entity_rank": 0.5,
                "channel_agreement": 0.0,
            },
        }
        for query_id in query_ids
    }
    relevance = {query_id: {"good": 2, "bad": 0} for query_id in query_ids}
    return query_ids, family_ids, features, relevance


def test_pairwise_cv_has_no_group_leakage_and_final_model_is_deterministic():
    args = _toy_pairwise_inputs()
    first = _pairwise_cv_rankings(*args)
    second = _pairwise_cv_rankings(*args)
    assert first[2] == second[2]
    assert first[2]["model_fingerprint"]
    for model in first[1]:
        assert set(model["heldout_family_ids"]).isdisjoint(model["training_family_ids"])


def test_source_id_metrics_report_exact_hits_and_denominators():
    queries = {
        "q": {
            "topic_family_id": "f",
            "source_entity_ids": ["source-a", "source-b"],
            "category": "exact_sentence",
        }
    }
    relevance = {"q": {"source-a": 2, "source-b": 2, "other": 0}}
    metrics, _ = _metric_vector(
        queries, relevance, {"q": ["source-a", "other", "source-b"]}, {"q": 3.0}
    )
    assert metrics["source_id_metrics"]["hit_at_1"] == {"hits": 1, "denominator": 2, "rate": 0.5}
    assert metrics["source_id_metrics"]["hit_at_10"]["hits"] == 2
    assert metrics["source_id_metrics"]["hit_at_20"]["denominator"] == 2
    paired = _paired_ndcg_outcome(
        queries,
        relevance,
        {"q": ["source-a", "other", "source-b"]},
        {"q": ["source-b", "other", "source-a"]},
    )
    assert paired["source_id_hit_outcomes"]["hit_at_1"] == {
        "wins": 0,
        "losses": 0,
        "ties": 1,
    }


def test_fixed_membership_rerank_preserves_recall_and_source_hit20():
    queries = {
        "q": {
            "topic_family_id": "f",
            "source_entity_ids": ["source-a"],
            "category": "exact_sentence",
        }
    }
    relevance = {"q": {"source-a": 2, "other": 0, "third": 0}}
    baseline, _ = _metric_vector(
        queries, relevance, {"q": ["source-a", "other", "third"]}, {"q": 1.0}
    )
    reranked, _ = _metric_vector(
        queries, relevance, {"q": ["other", "source-a", "third"]}, {"q": 1.0}
    )
    assert reranked["grade2_recall_at_20"] == baseline["grade2_recall_at_20"]
    assert reranked["source_id_metrics"]["hit_at_20"] == baseline["source_id_metrics"]["hit_at_20"]


def test_selection_requires_safety_gates_before_ndcg():
    base = {
        "macro_positive_ndcg_at_10": 0.50,
        "grade2_recall_at_20": 0.5,
        "same_specific_fact_grade2_top1": 0.5,
        "exact_safety": 0.8,
        "keyword_safety": 0.8,
        "strict_negative_safety": 0.8,
        "source_id_metrics": {
            key: {"rate": 0.5, "hits": 1, "denominator": 2}
            for key in ("hit_at_1", "hit_at_10", "hit_at_20")
        },
        "channel_failures": 0,
    }
    contender = deepcopy(base)
    contender["macro_positive_ndcg_at_10"] = 0.9
    contender["exact_safety"] = 0.7
    comparisons = {"deployed_hybrid_rrf": {"metrics": base}}
    comparisons["candidate"] = {"metrics": contender}
    for comparison in comparisons.values():
        comparison["delta_vs_deployed_rrf"] = _comparison_deltas(comparison["metrics"], base)
    recommendation = _selection_recommendation(comparisons)
    assert recommendation["selected_arm"] == "deployed_hybrid_rrf"
    assert not recommendation["gate_results"]["candidate"]["eligible"]
