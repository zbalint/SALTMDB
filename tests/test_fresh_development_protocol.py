import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "benchmarking"))

from bakeoff_state import fingerprint, sign_artifact  # noqa: E402
from fresh_development_protocol import (  # noqa: E402
    ADDENDUM_JUDGES,
    ARMS,
    BASELINE_ARM,
    CANDIDATE_ARM,
    METRIC_CONTRACT,
    METRIC_CONTRACT_FINGERPRINT,
    POSITIVE_FACETS,
    SUBTYPE_QUOTAS,
    FreshDevelopmentError,
    assert_later_transition_readiness,
    build_fresh_development_spec,
    build_fresh_query_manifest,
    build_query_review_packets,
    build_relevance_judge_packets,
    evaluate_fresh_development,
    execute_two_arm_development,
    _expected_measurement_schedule,
    _derive_rankings_from_retrieval,
    normalize_source_row,
)
from merge_judgments import (  # noqa: E402
    apply_arbitration_results,
    merged_artifact,
    merge_all_judgments,
)
from evaluate_development_fusion import (  # noqa: E402
    _channel_maps,
    _features,
    _rrf_ranking,
    _score_fusion_ranking,
)


def _spec():
    return build_fresh_development_spec(
        experiment_id="fresh-gate-d-synthetic",
        production_commit="a" * 40,
        corpus_snapshot_hash="b" * 64,
        bge_model_revision="c" * 40,
        bge_model_lock_fingerprint="e" * 64,
        lexical_adapter_fingerprint="d" * 64,
        exact_title_rule_fingerprint="e" * 64,
        machine_fingerprint="f" * 64,
        rubric_fingerprint="0" * 64,
        bootstrap_resamples=1000,
    )


def _records():
    records = []
    for facet, subtypes in SUBTYPE_QUOTAS.items():
        ordinal = 0
        for subtype, count in subtypes.items():
            for subtype_index in range(count):
                family = (
                    f"current-family-{subtype_index}"
                    if facet == "current_vs_superseded"
                    else f"{facet}-family-{ordinal % 28}"
                )
                row = {
                    "id": f"fresh-{facet}-{subtype}-{subtype_index:03d}",
                    "query": f"fresh {facet} {subtype} query {subtype_index}",
                    "category": facet,
                    "subtype": subtype,
                    "topic_family_id": family,
                    "source_entity_ids": []
                    if facet == "strict_negative"
                    else [f"doc-{facet}-{subtype}-{subtype_index:03d}"],
                }
                if facet == "multilingual":
                    row["language"] = subtype
                records.append(row)
                ordinal += 1
    return records


def _manifest(spec, records=None):
    return build_fresh_query_manifest(
        _records() if records is None else records,
        spec=spec,
        source_artifact_id="public-dev-source-v1",
        source_artifact_fingerprint="1" * 64,
        source_kind="unprotected_development_export",
        protected_query_ids=["old-query-a"],
        protected_topic_family_ids=["old-family-a"],
        protected_source_entity_ids=["old-source-a"],
        prior_assignment_fingerprint="2" * 64,
        protected_query_manifest_fingerprint="3" * 64,
    )


def _rankings(manifest):
    result = {arm: {} for arm in ARMS}
    for query in manifest["queries"]:
        source = query.get("source_entity_ids", [])
        if query["category"] == "exact_title" and query["subtype"] == "unique_byte_exact_singleton":
            baseline = candidate = [source[0]]
        else:
            baseline = [source[0], "other"] if source else ["none"]
            candidate = (
                baseline
                if query["category"] == "strict_negative"
                else ([source[0], "other"] if source else ["none"])
            )
        result[BASELINE_ARM][query["id"]] = baseline
        result[CANDIDATE_ARM][query["id"]] = candidate
    return result


def _evidence(spec, manifest, rankings, *, positive_miss_id=None, negative_grade2=False):
    query_rows = manifest["queries"]
    pools = {
        query["id"]: list(
            dict.fromkeys(
                rankings[BASELINE_ARM][query["id"]] + rankings[CANDIDATE_ARM][query["id"]]
            )
        )
        for query in query_rows
    }
    matrix = {"pools": pools}
    cells = {}
    for query in query_rows:
        query_id = query["id"]
        lexical = rankings[BASELINE_ARM][query_id]
        dense = rankings[CANDIDATE_ARM][query_id]
        diagnostic = {
            "triggered": False,
            "match_ids": [],
            "output_id": None,
            "output_rank": None,
            "unique_corpus_match": False,
        }
        if query["category"] == "exact_title":
            if query["subtype"] == "unique_byte_exact_singleton":
                diagnostic.update(
                    triggered=True,
                    match_ids=[query["source_entity_ids"][0]],
                    output_id=query["source_entity_ids"][0],
                    output_rank=1,
                    unique_corpus_match=True,
                )
            else:
                diagnostic["match_ids"] = []
        cells[query_id] = {
            "lexical": {"ids": lexical, "raw_bm25_scores": [-1.0] * len(lexical)},
            "dense": {"ids": dense, "scores": list(range(len(dense), 0, -1))},
            "exact_title": diagnostic,
        }
    raw = []
    for judge in ADDENDUM_JUDGES:
        labels = []
        for query in query_rows:
            for candidate in pools[query["id"]]:
                grade = 0
                if (
                    query["category"] != "strict_negative"
                    and candidate in query["source_entity_ids"]
                ):
                    grade = 2
                if query["id"] == positive_miss_id:
                    grade = 0
                if (
                    negative_grade2
                    and query["category"] == "strict_negative"
                    and candidate == pools[query["id"]][0]
                ):
                    grade = 2
                labels.append({"query_id": query["id"], "candidate_id": candidate, "grade": grade})
        artifact = {
            "judge": judge,
            "judge_version": "stage1-grades-0-1-2-gains-0-1-3-v1",
            "label_count": len(labels),
            "labels": labels,
        }
        artifact["fingerprint"] = fingerprint(artifact)
        raw.append(artifact)
    merged = merge_all_judgments(query_rows, raw, matrix, judges=ADDENDUM_JUDGES)
    arbitration_labels = []
    if positive_miss_id is not None:
        missed_query = next(query for query in query_rows if query["id"] == positive_miss_id)
        arbitration_labels = [
            {
                "task_id": f"arbitration:{positive_miss_id}:{candidate_id}",
                "grade": 0,
            }
            for candidate_id in missed_query["source_entity_ids"]
        ]
    arbitration = {"adjudicator": "agent_eval_adjudicator", "labels": arbitration_labels}
    arbitration["fingerprint"] = fingerprint(arbitration)
    merged = apply_arbitration_results(merged, arbitration)
    matrix_fingerprint = fingerprint(pools)
    merged_value = merged_artifact(
        merged,
        raw,
        query_rows,
        judges=ADDENDUM_JUDGES,
        binding={"matrix_fingerprint": matrix_fingerprint},
    )
    warmup_order, schedule = _expected_measurement_schedule(
        {query["id"] for query in query_rows}, 11
    )
    measurements = [
        {"query_id": query_id, "arm": arm, "samples": [10.0, 11.0]} for query_id, arm in schedule
    ]
    production = sign_artifact(
        "ProductionConfigReceipt",
        {
            "production": spec["production"],
            "candidate": spec["candidate"],
            "environment_fingerprint": "4" * 64,
        },
    )
    timing = {
        "warmups": [{"arm": arm, "samples": [10.0, 11.0]} for arm in warmup_order],
        "measurements": measurements,
        "schedule": [list(item) for item in schedule],
        "environment_fingerprint": "4" * 64,
        "schedule_seed": 11,
    }
    execution = sign_artifact(
        "TwoArmExecutionReceipt",
        {
            "arms": list(ARMS),
            "schedule": [list(item) for item in schedule],
            "schedule_seed": 11,
            "spec_fingerprint": spec["artifact_fingerprint"],
            "manifest_fingerprint": manifest["artifact_fingerprint"],
            "production_receipt_fingerprint": production["artifact_fingerprint"],
            "rankings_fingerprint": fingerprint(rankings),
            "timing_trace_fingerprint": fingerprint(timing),
            "schedule_fingerprint": fingerprint(schedule),
            "environment_fingerprint": "4" * 64,
            "warmup_count": len(warmup_order),
            "measurement_count": len(schedule),
            "configuration_fingerprint": fingerprint(spec["candidate"]),
            "metric_contract_fingerprint": spec["metric_contract_fingerprint"],
        },
    )
    return {
        "retrieval_evidence": {"cells": cells, "fingerprint": fingerprint(cells)},
        "judging_matrix": {"pools": pools, "fingerprint": fingerprint(pools)},
        "raw_judgment_artifacts": raw,
        "arbitration_response": arbitration,
        "merged_labels": merged_value,
        "adjudication_receipt": sign_artifact(
            "AdjudicationReceipt",
            {
                "raw_labels_fingerprint": fingerprint(raw),
                "arbitration_response_fingerprint": fingerprint(arbitration),
                "merged_labels_fingerprint": fingerprint(merged_value),
            },
        ),
        "timing_trace": timing,
        "production_receipt": production,
        "execution_receipt": execution,
        "channel_failures": dict.fromkeys(ARMS, 0),
    }


def test_exact_quota_and_packets_are_split():
    spec = _spec()
    manifest = _manifest(spec)
    writer = build_query_review_packets(manifest, spec, packet_size=37)
    assert sum(len(packet["queries"]) for packet in writer["packets"]) == 400
    assert all(
        set(row) == {"query_id", "query", "category"}
        for packet in writer["packets"]
        for row in packet["queries"]
    )
    rankings = {
        arm: {
            query["id"]: [
                query.get("source_entity_ids", ["none"])[0]
                if query.get("source_entity_ids")
                else "none",
                "other",
            ]
            for query in manifest["queries"]
        }
        for arm in ARMS
    }
    texts = {
        query["id"]: {
            candidate: {"title": candidate, "full_content": f"content for {candidate}"}
            for candidate in set(
                rankings[BASELINE_ARM][query["id"]] + rankings[CANDIDATE_ARM][query["id"]]
            )
        }
        for query in manifest["queries"]
    }
    judged = build_relevance_judge_packets(manifest, spec, rankings, texts)
    assert judged["judges"] == list(ADDENDUM_JUDGES)


def test_evaluation_recomputes_raw_evidence_and_rejects_unjudged():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = {arm: {} for arm in ARMS}
    for query in manifest["queries"]:
        source = query.get("source_entity_ids", [])
        if query["category"] == "exact_title" and query["subtype"] == "unique_byte_exact_singleton":
            baseline = candidate = [source[0]]
        else:
            baseline = ["other", source[0]] if source else ["none"]
            candidate = [source[0], "other"] if source else ["none"]
        if query["category"] == "strict_negative":
            candidate = baseline
        rankings[BASELINE_ARM][query["id"]] = baseline
        rankings[CANDIDATE_ARM][query["id"]] = candidate
    evidence = _evidence(spec, manifest, rankings)
    decision = evaluate_fresh_development(spec, manifest, rankings, evidence)
    assert decision["kind"] == "DevelopmentWinner"
    tampered = dict(decision)
    tampered["metrics"] = dict(
        decision["metrics"], **{BASELINE_ARM: {"macro_positive_ndcg_at_10": 999}}
    )
    tampered["artifact_fingerprint"] = fingerprint(
        {key: value for key, value in tampered.items() if key != "artifact_fingerprint"}
    )
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_development_decision

        validate_development_decision(tampered, spec, manifest)


def test_evaluation_keeps_positive_misses_in_recall_and_excludes_negative_grade2_from_accuracy():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    positive_query = next(
        query for query in manifest["queries"] if query["category"] in POSITIVE_FACETS
    )
    positive_count = sum(1 for query in manifest["queries"] if query["category"] in POSITIVE_FACETS)
    assert positive_count == 316
    miss_evidence = _evidence(
        spec,
        manifest,
        rankings,
        positive_miss_id=positive_query["id"],
    )
    noisy_evidence = _evidence(
        spec,
        manifest,
        rankings,
        positive_miss_id=positive_query["id"],
        negative_grade2=True,
    )

    clean_decision = evaluate_fresh_development(spec, manifest, rankings, miss_evidence)
    noisy_decision = evaluate_fresh_development(spec, manifest, rankings, noisy_evidence)
    clean_baseline = clean_decision["metrics"][BASELINE_ARM]
    clean_candidate = clean_decision["metrics"][CANDIDATE_ARM]
    noisy_baseline = noisy_decision["metrics"][BASELINE_ARM]
    noisy_candidate = noisy_decision["metrics"][CANDIDATE_ARM]

    # The positive miss contributes zero to recall, so the denominator remains every positive
    # query rather than silently dropping the miss.  Both arms have the same synthetic labels.
    expected_recall = 315 / 316
    assert expected_recall == pytest.approx((positive_count - 1) / positive_count)
    assert noisy_baseline["grade2_recall_at_20"] == pytest.approx(expected_recall)
    assert noisy_candidate["grade2_recall_at_20"] == pytest.approx(expected_recall)
    assert noisy_baseline["same_specific_fact_grade2_top1"] == pytest.approx(expected_recall)
    # The strict-negative grade-2 judgment affects only strict-negative safety; it must not
    # inflate NDCG or create an accuracy-family observation.
    assert clean_baseline["strict_negative_safety"] == 1.0
    assert noisy_baseline["strict_negative_safety"] == 0.0
    assert noisy_baseline["macro_positive_ndcg_at_10"] == pytest.approx(
        clean_baseline["macro_positive_ndcg_at_10"]
    )
    assert noisy_candidate["macro_positive_ndcg_at_10"] == pytest.approx(
        clean_candidate["macro_positive_ndcg_at_10"]
    )


def test_manifest_rejects_missing_attestation_and_bad_quota():
    spec = _spec()
    records = _records()
    records[-1] = dict(records[-1], category="keyword")
    with pytest.raises(FreshDevelopmentError):
        build_fresh_query_manifest(
            records,
            spec=spec,
            source_artifact_id="x",
            source_artifact_fingerprint="1" * 64,
            source_kind="public",
            protected_query_ids=[],
            protected_topic_family_ids=[],
            protected_source_entity_ids=[],
            prior_assignment_fingerprint="2" * 64,
            protected_query_manifest_fingerprint="3" * 64,
        )


def _resign(kind, artifact):
    return sign_artifact(
        kind,
        {
            key: value
            for key, value in artifact.items()
            if key not in {"schema_version", "kind", "artifact_fingerprint"}
        },
    )


def test_metric_contract_is_required_and_canonical_for_future_specs():
    spec = _spec()
    assert spec["metric_contract"] == METRIC_CONTRACT
    assert spec["metric_contract_fingerprint"] == METRIC_CONTRACT_FINGERPRINT

    missing = copy.deepcopy(spec)
    missing.pop("metric_contract")
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_fresh_development_spec

        validate_fresh_development_spec(_resign("FreshDevelopmentSpec", missing))

    tampered = copy.deepcopy(spec)
    tampered["metric_contract"]["version"] = "legacy-compatible"
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_fresh_development_spec

        validate_fresh_development_spec(_resign("FreshDevelopmentSpec", tampered))


def test_execution_receipt_binds_metric_contract_fingerprint():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = copy.deepcopy(_evidence(spec, manifest, rankings))
    receipt = evidence["execution_receipt"]
    payload = {key: value for key, value in receipt.items() if key != "artifact_fingerprint"}
    payload["metric_contract_fingerprint"] = "0" * 64
    evidence["execution_receipt"] = sign_artifact("TwoArmExecutionReceipt", payload)
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, evidence)


def test_decision_binds_metric_contract_fingerprint():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    decision = evaluate_fresh_development(
        spec, manifest, rankings, _evidence(spec, manifest, rankings)
    )
    assert decision["metric_contract_fingerprint"] == spec["metric_contract_fingerprint"]
    assert (
        decision["evidence_fingerprints"]["metric_contract_fingerprint"]
        == spec["metric_contract_fingerprint"]
    )
    forged = dict(decision, metric_contract_fingerprint="0" * 64)
    forged = _resign(decision["kind"], forged)
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_development_decision

        validate_development_decision(forged, spec, manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["production"].update(extra=True),
        lambda value: value["production"]["lexical"].update(extra=True),
        lambda value: value["production"]["dense"].update(extra=True),
        lambda value: value["production"]["exact_title"].update(tie_break="forged"),
    ],
)
def test_resigned_spec_nested_production_forgery_is_rejected(mutate):
    forged = copy.deepcopy(_spec())
    mutate(forged)
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_fresh_development_spec

        validate_fresh_development_spec(_resign("FreshDevelopmentSpec", forged))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(provenance="forged"),
        lambda row: row.update(id="bad id"),
        lambda row: row.update(extra="forged"),
        lambda row: row.update(source_entity_ids=[row["source_entity_ids"][0]] * 2),
    ],
)
def test_resigned_manifest_row_forgery_is_rejected(mutate):
    spec = _spec()
    manifest = _manifest(spec)
    forged = copy.deepcopy(manifest)
    mutate(forged["queries"][0])
    forged["queries_fingerprint"] = fingerprint(forged["queries"])
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_fresh_query_manifest

        validate_fresh_query_manifest(_resign("FreshDevelopmentQueryManifest", forged), spec)


@pytest.mark.parametrize("field", ["artifact_id", "kind"])
def test_resigned_manifest_empty_attestation_identity_is_rejected(field):
    spec = _spec()
    manifest = _manifest(spec)
    forged = copy.deepcopy(manifest)
    forged["source_attestation"][field] = ""
    with pytest.raises(FreshDevelopmentError):
        from fresh_development_protocol import validate_fresh_query_manifest

        validate_fresh_query_manifest(_resign("FreshDevelopmentQueryManifest", forged), spec)


def test_evidence_fails_closed_for_unjudged_or_noninterleaved_or_nan_timing():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = _evidence(spec, manifest, rankings)
    incomplete = dict(evidence, raw_judgment_artifacts=evidence["raw_judgment_artifacts"][:2])
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, incomplete)
    malformed_timing = dict(evidence)
    malformed_timing["timing_trace"] = dict(evidence["timing_trace"])
    malformed_timing["timing_trace"]["measurements"] = list(
        evidence["timing_trace"]["measurements"]
    )
    malformed_timing["timing_trace"]["measurements"][0] = dict(
        malformed_timing["timing_trace"]["measurements"][0], samples=[float("nan"), 11.0]
    )
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, malformed_timing)


def test_arbitration_response_is_applied_and_bound_end_to_end():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = _evidence(spec, manifest, rankings)
    evidence = copy.deepcopy(evidence)
    query_id = manifest["queries"][0]["id"]
    candidate_id = evidence["judging_matrix"]["pools"][query_id][0]
    for index, artifact in enumerate(evidence["raw_judgment_artifacts"]):
        next_grade = index
        for label in artifact["labels"]:
            if label["query_id"] == query_id and label["candidate_id"] == candidate_id:
                label["grade"] = next_grade
        artifact["fingerprint"] = fingerprint(
            {key: value for key, value in artifact.items() if key != "fingerprint"}
        )
    raw = evidence["raw_judgment_artifacts"]
    matrix = {
        "pools": {
            query_id: {candidate: {} for candidate in candidates}
            for query_id, candidates in evidence["judging_matrix"]["pools"].items()
        }
    }
    merged = merge_all_judgments(manifest["queries"], raw, matrix, judges=ADDENDUM_JUDGES)
    arbitration = {
        "adjudicator": "agent_eval_adjudicator",
        "labels": [{"task_id": f"arbitration:{query_id}:{candidate_id}", "grade": 2}],
    }
    arbitration["fingerprint"] = fingerprint(arbitration)
    apply_arbitration_results(merged, arbitration)
    evidence["arbitration_response"] = arbitration
    evidence["merged_labels"] = merged_artifact(
        merged,
        raw,
        manifest["queries"],
        judges=ADDENDUM_JUDGES,
        binding={"matrix_fingerprint": evidence["judging_matrix"]["fingerprint"]},
    )
    evidence["adjudication_receipt"] = sign_artifact(
        "AdjudicationReceipt",
        {
            "raw_labels_fingerprint": fingerprint(raw),
            "arbitration_response_fingerprint": fingerprint(arbitration),
            "merged_labels_fingerprint": fingerprint(evidence["merged_labels"]),
        },
    )
    decision = evaluate_fresh_development(spec, manifest, rankings, evidence)
    assert decision["kind"] in {"DevelopmentWinner", "BaselineDecision"}
    missing = dict(
        evidence,
        arbitration_response={
            "adjudicator": "agent_eval_adjudicator",
            "labels": [],
            "fingerprint": fingerprint({"adjudicator": "agent_eval_adjudicator", "labels": []}),
        },
    )
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, missing)


def test_readiness_never_authorizes():
    spec = _spec()
    manifest = _manifest(spec)
    with pytest.raises(FreshDevelopmentError):
        assert_later_transition_readiness(spec, {"kind": "DevelopmentWinner"}, manifest)


def test_executor_records_interleaved_exact_once_timing_receipt():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = _evidence(spec, manifest, rankings)
    calls = []

    def runner(arm, query, phase):
        calls.append((arm, query["id"], phase))
        ranking = rankings[arm][query["id"]]
        return ranking, [10.0, 11.0]

    decision = execute_two_arm_development(spec, manifest, runner, evidence)
    assert decision["kind"] == "BaselineDecision"
    assert len(calls) == 2 + (2 * spec["query_count"])
    assert all(phase == "warmup" for _, _, phase in calls[:2])


def test_retrieval_receipt_is_the_only_ranking_authority():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = _evidence(spec, manifest, rankings)
    forged = copy.deepcopy(evidence)
    query_id = next(
        query["id"]
        for query in manifest["queries"]
        if len(evidence["retrieval_evidence"]["cells"][query["id"]]["dense"]["ids"]) > 1
    )
    forged["retrieval_evidence"]["cells"][query_id]["dense"]["ids"] = list(
        reversed(forged["retrieval_evidence"]["cells"][query_id]["dense"]["ids"])
    )
    forged["retrieval_evidence"]["fingerprint"] = fingerprint(forged["retrieval_evidence"]["cells"])
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, forged)


def test_execution_receipt_rejects_seed_or_schedule_replay():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = copy.deepcopy(_evidence(spec, manifest, rankings))
    receipt = evidence["execution_receipt"]
    payload = {key: value for key, value in receipt.items() if key != "artifact_fingerprint"}
    payload["schedule_seed"] = 12
    evidence["execution_receipt"] = sign_artifact("TwoArmExecutionReceipt", payload)
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, evidence)


def test_arbitration_requires_exact_escalation_response():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = copy.deepcopy(_evidence(spec, manifest, rankings))
    response = evidence["arbitration_response"]
    response["labels"] = [{"task_id": "arbitration:not-present", "grade": 2}]
    response["fingerprint"] = fingerprint(
        {key: value for key, value in response.items() if key != "fingerprint"}
    )
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, evidence)


def test_production_bm25_sign_and_missing_channel_floor_are_frozen():
    manifest = {
        "queries": [
            {"id": "q", "category": "keyword", "subtype": "terse", "source_entity_ids": ["a"]}
        ]
    }
    retrieval = {
        "cells": {
            "q": {
                "lexical": {"ids": ["a", "b"], "raw_bm25_scores": [-10.0, -1.0]},
                "dense": {"ids": ["b", "a"], "scores": [1.0, 0.0]},
                "exact_title": {
                    "triggered": False,
                    "match_ids": [],
                    "output_id": None,
                    "output_rank": None,
                    "unique_corpus_match": False,
                },
            }
        }
    }
    retrieval["fingerprint"] = fingerprint(retrieval["cells"])
    derived = _derive_rankings_from_retrieval(retrieval, spec={}, manifest=manifest)
    assert derived[BASELINE_ARM]["q"] == ["a", "b"]
    assert derived[CANDIDATE_ARM]["q"] == ["a", "b"]

    empty = copy.deepcopy(retrieval)
    empty["cells"]["q"]["lexical"] = {"ids": [], "raw_bm25_scores": []}
    empty["fingerprint"] = fingerprint(empty["cells"])
    empty_derived = _derive_rankings_from_retrieval(empty, spec={}, manifest=manifest)
    assert empty_derived[BASELINE_ARM]["q"] == ["b", "a"]


def test_exact_title_singleton_can_be_outside_channel_window_and_source_labels_normalize():
    manifest = {
        "queries": [
            {
                "id": "q",
                "category": "exact_title",
                "subtype": "unique_byte_exact_singleton",
                "source_entity_ids": ["outside"],
            }
        ]
    }
    retrieval = {
        "cells": {
            "q": {
                "lexical": {"ids": ["window"], "raw_bm25_scores": [-1.0]},
                "dense": {"ids": ["window"], "scores": [0.1]},
                "exact_title": {
                    "triggered": True,
                    "match_ids": ["outside"],
                    "output_id": "outside",
                    "output_rank": 1,
                    "unique_corpus_match": True,
                },
            }
        }
    }
    retrieval["fingerprint"] = fingerprint(retrieval["cells"])
    derived = _derive_rankings_from_retrieval(retrieval, spec={}, manifest=manifest)
    assert derived[BASELINE_ARM]["q"] == ["outside"]
    assert derived[CANDIDATE_ARM]["q"] == ["outside"]
    assert normalize_source_row({"facet": "exact_sentence", "subtype": "sentence_reserve"}) == {
        "category": "exact_sentence",
        "subtype": "default",
    }
    assert normalize_source_row({"facet": "paraphrase", "subtype": "paraphrase_primary"}) == {
        "category": "paraphrase",
        "subtype": "default",
    }
    assert normalize_source_row({"facet": "short_memory", "subtype": "short_primary"}) == {
        "category": "short_memory",
        "subtype": "default",
    }
    assert normalize_source_row({"facet": "exact_title", "subtype": "byte_exact_singleton"}) == {
        "category": "exact_title",
        "subtype": "unique_byte_exact_singleton",
    }
    assert normalize_source_row(
        {
            "facet": "current_vs_superseded",
            "subtype": "current_text_only",
            "variant_index": 3,
            "topic_family_id": "family-1",
        }
    ) == {"category": "current_vs_superseded", "subtype": "variant_3"}
    assert normalize_source_row(
        {"facet": "controls", "subtype": "mismatch_collision_fallthrough"}
    ) == {"category": "exact_title", "subtype": "byte_mismatch_fallthrough"}


def test_pool_only_score_normalization_matches_canonical_helpers_above_twenty_union():
    lexical_ids = [f"l{index:02d}" for index in range(20)]
    dense_ids = [f"l{index:02d}" for index in range(19)] + ["d19"]
    lexical_raw = [-float(index + 1) for index in range(20)]
    dense_scores = [float(index + 1) for index in range(19)] + [10_000.0]
    manifest = {
        "queries": [
            {"id": "q", "category": "keyword", "subtype": "terse", "source_entity_ids": ["l00"]}
        ]
    }
    cell = {
        "lexical": {"ids": lexical_ids, "raw_bm25_scores": lexical_raw},
        "dense": {"ids": dense_ids, "scores": dense_scores},
        "exact_title": {
            "triggered": False,
            "match_ids": [],
            "output_id": None,
            "output_rank": None,
            "unique_corpus_match": False,
        },
    }
    retrieval = {"cells": {"q": cell}}
    retrieval["fingerprint"] = fingerprint(retrieval["cells"])
    derived = _derive_rankings_from_retrieval(retrieval, spec={}, manifest=manifest)
    lexical_row = {
        "top20": [
            {"entity_id": item, "raw_bm25_score": score}
            for item, score in zip(lexical_ids, lexical_raw)
        ]
    }
    dense_row = {
        "top20": [
            {"entity_id": item, "score": score} for item, score in zip(dense_ids, dense_scores)
        ]
    }
    union, lexical_ranks, dense_ranks, lexical_scores, dense_scores_map, _ = _channel_maps(
        lexical_row, dense_row
    )
    canonical_pool = _rrf_ranking(union, lexical_ranks, dense_ranks)
    canonical_features = _features(
        union, lexical_ranks, dense_ranks, lexical_scores, dense_scores_map
    )
    canonical_candidate = _score_fusion_ranking(
        canonical_features, 1.5, 1.0, candidate_ids=canonical_pool
    )
    assert derived[BASELINE_ARM]["q"] == canonical_pool
    assert derived[CANDIDATE_ARM]["q"] == canonical_candidate


def test_empty_channels_only_pass_exact_title_singleton_fast_path():
    manifest = {
        "queries": [
            {
                "id": "q",
                "category": "exact_title",
                "subtype": "unique_byte_exact_singleton",
                "source_entity_ids": ["outside"],
            }
        ]
    }
    retrieval = {
        "cells": {
            "q": {
                "lexical": {"ids": [], "raw_bm25_scores": []},
                "dense": {"ids": [], "scores": []},
                "exact_title": {
                    "triggered": True,
                    "match_ids": ["outside"],
                    "output_id": "outside",
                    "output_rank": 1,
                    "unique_corpus_match": True,
                },
            }
        }
    }
    retrieval["fingerprint"] = fingerprint(retrieval["cells"])
    derived = _derive_rankings_from_retrieval(retrieval, spec={}, manifest=manifest)
    assert derived[BASELINE_ARM]["q"] == ["outside"]
    fallthrough = copy.deepcopy(retrieval)
    fallthrough["cells"]["q"]["exact_title"]["triggered"] = False
    fallthrough["cells"]["q"]["exact_title"]["match_ids"] = []
    fallthrough["fingerprint"] = fingerprint(fallthrough["cells"])
    with pytest.raises(FreshDevelopmentError):
        _derive_rankings_from_retrieval(fallthrough, spec={}, manifest=manifest)


def test_source_hit_metrics_use_unique_query_source_pairs_not_query_any():
    spec = _spec()
    records = _records()
    two_source_row = next(row for row in records if row["category"] == "keyword")
    two_source_row["source_entity_ids"].append("doc-second-source")
    manifest = _manifest(spec, records)
    rankings = _rankings(manifest)
    evidence = _evidence(spec, manifest, rankings)
    decision = evaluate_fresh_development(spec, manifest, rankings, evidence)
    # The first query has two declared source IDs but only the first is retrieved.  The
    # source-hit metric must therefore count one miss in the pair denominator, while the
    # same-specific-fact top-1 metric remains query-level.
    assert decision["metrics"][BASELINE_ARM]["source_hit_at_1"] == pytest.approx(
        316 / 317, abs=1e-12
    )
    assert decision["metrics"][BASELINE_ARM]["source_hit_unit"] == "unique_query_source_pair"


def test_nontriggered_exact_title_diagnostics_are_empty_and_nonunique():
    spec = _spec()
    manifest = _manifest(spec)
    rankings = _rankings(manifest)
    evidence = copy.deepcopy(_evidence(spec, manifest, rankings))
    query = next(row for row in manifest["queries"] if row["category"] == "keyword")
    diagnostics = evidence["retrieval_evidence"]["cells"][query["id"]]["exact_title"]
    diagnostics["unique_corpus_match"] = True
    evidence["retrieval_evidence"]["fingerprint"] = fingerprint(
        evidence["retrieval_evidence"]["cells"]
    )
    with pytest.raises(FreshDevelopmentError):
        evaluate_fresh_development(spec, manifest, rankings, evidence)
