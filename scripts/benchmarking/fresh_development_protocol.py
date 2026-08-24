"""Preregistered, two-arm development confirmation for Gate-D search ranking.

This module is intentionally independent of the blind custody implementation.  It accepts only
caller-supplied, content-addressed production identities and a *new* development manifest.  It never opens
or discovers protected material.  The manifest builder therefore requires an explicit external
freshness attestation; it cannot turn an old query file into "fresh" evidence by assertion.

The executable confirmation has exactly two immutable arms:

* ``deployed_hybrid_rrf``: production lexical/BGE entity retrieval, rank RRF, ``k=60``;
* ``score_rerank_rrf_pool_fts_1.5_dense_1``: the same top-20 RRF pool, reordered once with fixed
  1.5:1 min-max score weights.

The runner is injected so this contract can be tested without a database or model download.  A
real caller must provide end-to-end timings and independently adjudicated labels; no tuning data
or protected artifact is inferred here. Caller hashes and freshness attestations preserve an
externally established provenance boundary; they cannot prove freshness or authenticate a
custody signer. ``sign_artifact`` fingerprints provide integrity after acquisition only.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import statistics
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from bakeoff_state import BakeoffContractError, fingerprint, sign_artifact, validate_signed_artifact
from eval_stats import cluster_bootstrap_delta_ci, ndcg_at_10, semantic_recall_at_20
from judge_pool import ADDENDUM_JUDGES, build_judge_packets
from merge_judgments import apply_arbitration_results, merge_all_judgments, merged_artifact


BASELINE_ARM = "deployed_hybrid_rrf"
CANDIDATE_ARM = "score_rerank_rrf_pool_fts_1.5_dense_1"
ARMS = (BASELINE_ARM, CANDIDATE_ARM)
TOP_N = 20
RRF_K = 60
SPEC_KIND = "FreshDevelopmentSpec"
MANIFEST_KIND = "FreshDevelopmentQueryManifest"
PACKETS_KIND = "FreshDevelopmentQueryReviewPackets"
DECISION_KINDS = frozenset({"DevelopmentWinner", "BaselineDecision"})
FACET_QUOTAS = {
    "exact_title": 20,
    "exact_sentence": 20,
    "keyword": 48,
    "typo": 24,
    "paraphrase": 28,
    "short_memory": 20,
    "long_body": 48,
    "multilingual": 28,
    "current_vs_superseded": 28,
    "close_sibling": 28,
    "contradiction": 24,
    "strict_negative": 84,
}
POSITIVE_FACETS = frozenset(FACET_QUOTAS) - {"strict_negative"}
# This contract is deliberately explicit and content-addressed.  It is part of every future
# FreshDevelopmentSpec so a metric implementation change cannot silently reinterpret a run.
_METRIC_CONTRACT_BODY = {
    "version": "gate-d-metrics-v2",
    "positive_facets": sorted(POSITIVE_FACETS),
    "ndcg_at_10": {
        "included_facets": "positive_facets_only",
        "no_positive_grade_item": "exclude_query_from_ndcg_aggregate",
        "aggregation": "macro_topic_family_mean_of_defined_query_values",
    },
    "grade2_recall_at_20": {
        "included_facets": "all_positive_facets",
        "no_grade2_judged_item": "zero",
        "aggregation": "arithmetic_mean_over_all_positive_queries",
    },
    "strict_negative": {
        "excluded_from_accuracy": True,
        "included_metric": "top1_safety_only",
        "safe_when_top1_grade": "less_than_1",
    },
}
METRIC_CONTRACT_FINGERPRINT = fingerprint(_METRIC_CONTRACT_BODY)
METRIC_CONTRACT = {
    **_METRIC_CONTRACT_BODY,
    "fingerprint": METRIC_CONTRACT_FINGERPRINT,
}
SUBTYPE_QUOTAS = {
    "exact_title": {"unique_byte_exact_singleton": 16, "byte_mismatch_fallthrough": 4},
    "exact_sentence": {"default": 20},
    "keyword": {"terse": 24, "multi_keyword": 24},
    "typo": {"edit_transposition": 12, "keyboard_ocr": 12},
    "paraphrase": {"default": 28},
    "short_memory": {"default": 20},
    "long_body": {"distinctive_phrase": 24, "longer_description": 24},
    "multilingual": {"latin_non_english": 7, "cyrillic": 7, "rtl_arabic": 7, "cjk": 7},
    "current_vs_superseded": {"variant_0": 7, "variant_1": 7, "variant_2": 7, "variant_3": 7},
    "close_sibling": {"shared_topic_different_operation": 14, "lexical_lookalike": 14},
    "contradiction": {"explicit": 12, "qualifier_negation": 12},
    "strict_negative": {
        "semantically_adjacent_wrong": 28,
        "lexical_overlap_unsupported": 28,
        "no_answer_out_of_scope": 28,
    },
}
MANIFEST_FIELDS = frozenset(
    {
        "id",
        "query",
        "category",
        "subtype",
        "language",
        "topic_family_id",
        "source_entity_ids",
        "split",
        "provenance",
    }
)
WRITER_PACKET_FIELDS = frozenset({"query_id", "query", "category"})
SOURCE_LABEL_MAPPING = {
    "exact_title": "exact_title",
    "controls": "exact_title",
    "exact_sentence": "exact_sentence",
    "keyword": "keyword",
    "typo": "typo",
    "paraphrase": "paraphrase",
    "short_memory": "short_memory",
    "long_body": "long_body",
    "multilingual": "multilingual",
    "current_vs_superseded": "current_vs_superseded",
    "close_sibling": "close_sibling",
    "contradiction": "contradiction",
    "strict_negative": "strict_negative",
}
SOURCE_LABEL_MAPPING_FINGERPRINT = fingerprint(SOURCE_LABEL_MAPPING)
SOURCE_SUBTYPE_MAPPING = {
    "exact_sentence": {
        "sentence_primary": "default",
        "sentence_reserve": "default",
    },
    "paraphrase": {
        "paraphrase_primary": "default",
        "paraphrase_reserve": "default",
    },
    "short_memory": {"short_primary": "default", "short_reserve": "default"},
    "long_body": {"distinctive_remembered_phrase": "distinctive_phrase"},
    "current_vs_superseded": {"current_text_only": "variant_index"},
    "exact_title": {
        "byte_exact_singleton": "unique_byte_exact_singleton",
        "mismatch_collision_fallthrough": "byte_mismatch_fallthrough",
        "byte_mismatch_fallthrough": "byte_mismatch_fallthrough",
    },
    "controls": {
        "mismatch_collision_fallthrough": "byte_mismatch_fallthrough",
        "byte_mismatch_fallthrough": "byte_mismatch_fallthrough",
    },
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IMMUTABLE_ID_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PROTECTED_TERMS = frozenset(
    {"blind", "source_slots", "source-slot", "vault", "blindunlock", "blind-result"}
)


class FreshDevelopmentError(BakeoffContractError):
    """A fresh confirmation input or result violates the immutable contract."""


def _hash(value: object) -> str:
    return fingerprint(value)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_field(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise FreshDevelopmentError(f"{field} must be a lowercase SHA-256")
    return value


def _immutable_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _IMMUTABLE_ID_RE.fullmatch(value) is None:
        raise FreshDevelopmentError(f"{field} must be a lowercase 40- or 64-hex immutable ID")
    return value


def _git_commit(value: object, field: str, object_format: str = "sha1") -> str:
    expected_length = {"sha1": 40, "sha256": 64}.get(object_format)
    if expected_length is None or not isinstance(value, str) or len(value) != expected_length:
        raise FreshDevelopmentError(f"{field} length does not match Git {object_format} format")
    if re.fullmatch(r"[0-9a-f]+", value) is None:
        raise FreshDevelopmentError(f"{field} must be lowercase hexadecimal")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FreshDevelopmentError(f"{field} must be non-empty text")
    return value


def normalize_source_row(row: Mapping[str, Any]) -> dict[str, str]:
    """Normalize final-assembler construction labels before manifest construction.

    This accepts only non-protected source-row labels; it does not discover or read source-slot
    material.  ``current_text_only`` rows must carry a stable integer ``variant_index`` (0..3)
    within their already-attested seven-family lifecycle family.
    """
    if not isinstance(row, Mapping):
        raise FreshDevelopmentError("source construction row must be an object")
    facet = row.get("source_facet", row.get("facet"))
    subtype = row.get("source_subtype", row.get("subtype"))
    if facet not in SOURCE_LABEL_MAPPING:
        raise FreshDevelopmentError("source construction facet is not registered")
    category = SOURCE_LABEL_MAPPING[facet]
    if category == "current_vs_superseded" and subtype == "current_text_only":
        if not isinstance(row.get("topic_family_id"), str) or not row["topic_family_id"]:
            raise FreshDevelopmentError("current_text_only requires an attested topic family")
        index = row.get("variant_index")
        if not isinstance(index, int) or isinstance(index, bool) or index not in range(4):
            raise FreshDevelopmentError("current_text_only requires stable variant_index 0..3")
        normalized_subtype = f"variant_{index}"
    else:
        mapping = SOURCE_SUBTYPE_MAPPING.get(facet, {})
        if subtype not in mapping:
            raise FreshDevelopmentError("source construction subtype is not registered")
        normalized_subtype = mapping[subtype]
    return {"category": category, "subtype": normalized_subtype}


def _reject_protected_text(value: object, label: str) -> None:
    """Reject a protected-looking identity without touching the referenced path/artifact."""
    if not isinstance(value, str):
        return
    lowered = value.lower().replace("\\", "/")
    tokens = set(part for part in re.split(r"[/_.-]+", lowered) if part)
    if tokens & _PROTECTED_TERMS:
        raise FreshDevelopmentError(f"{label} names protected material")


def _validate_arm_list(arms: object) -> None:
    if arms != list(ARMS):
        raise FreshDevelopmentError(f"arms must be exactly {list(ARMS)!r}, in order")


def build_fresh_development_spec(  # noqa: C901, PLR0912
    *,
    experiment_id: str,
    production_commit: str,
    corpus_snapshot_hash: str,
    bge_model_revision: str,
    bge_model_lock_fingerprint: str,
    lexical_adapter_fingerprint: str,
    exact_title_rule_fingerprint: str,
    machine_fingerprint: str,
    rubric_fingerprint: str,
    query_count: int = 400,
    minimum_ndcg_effect: float = 0.01,
    max_latency_p50_increase: float = 0.10,
    max_latency_p95_increase: float = 0.10,
    bootstrap_resamples: int = 4000,
    bootstrap_seed: int = 11,
    git_object_format: str = "sha1",
    commit_id_sha256: str | None = None,
    bge_model_id: str = "BAAI/bge-small-en-v1.5",
) -> dict[str, Any]:
    """Build and sign the immutable two-arm preregistration.

    Every runtime identity is passed in by the caller and content-addressed.  This avoids the
    tempting but unsafe behavior of deriving a production identity from a mutable working tree.
    """
    if not _ID_RE.fullmatch(experiment_id):
        raise FreshDevelopmentError("experiment_id is not filename-safe")
    if git_object_format not in {"sha1", "sha256"}:
        raise FreshDevelopmentError("git_object_format must be sha1 or sha256")
    _git_commit(production_commit, "production_commit", git_object_format)
    _immutable_id(bge_model_revision, "bge_model_revision")
    if commit_id_sha256 is not None:
        _hash_field(commit_id_sha256, "commit_id_sha256")
        if commit_id_sha256 != _hash_bytes(production_commit.encode("ascii")):
            raise FreshDevelopmentError("commit_id_sha256 does not bind git_commit")
    if bge_model_id != "BAAI/bge-small-en-v1.5":
        raise FreshDevelopmentError("bge_model_id must be the pinned BGE-small model")
    for name, value in (
        ("corpus_snapshot_hash", corpus_snapshot_hash),
        ("bge_model_lock_fingerprint", bge_model_lock_fingerprint),
        ("lexical_adapter_fingerprint", lexical_adapter_fingerprint),
        ("exact_title_rule_fingerprint", exact_title_rule_fingerprint),
        ("machine_fingerprint", machine_fingerprint),
        ("rubric_fingerprint", rubric_fingerprint),
    ):  # noqa: C901, PLR0912
        _hash_field(value, name)
    if query_count != sum(FACET_QUOTAS.values()):
        raise FreshDevelopmentError("fresh development query_count must be exactly 400")
    for name, value in (
        ("minimum_ndcg_effect", minimum_ndcg_effect),
        ("max_latency_p50_increase", max_latency_p50_increase),
        ("max_latency_p95_increase", max_latency_p95_increase),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise FreshDevelopmentError(f"{name} must be finite")
        if value < 0:
            raise FreshDevelopmentError(f"{name} must be non-negative")
    if not isinstance(bootstrap_resamples, int) or bootstrap_resamples < 1000:
        raise FreshDevelopmentError("bootstrap_resamples must be >= 1000")
    if (
        not isinstance(bootstrap_seed, int)
        or isinstance(bootstrap_seed, bool)
        or bootstrap_seed < 0
    ):
        raise FreshDevelopmentError("bootstrap_seed must be a non-negative integer")
    payload = {
        "experiment_id": experiment_id,
        "arms": list(ARMS),
        "production": {
            "git_commit": production_commit,
            "git_object_format": git_object_format,
            "corpus_snapshot_hash": corpus_snapshot_hash,
            "lexical": {
                "adapter_fingerprint": lexical_adapter_fingerprint,
                "policy": "production_sanitize_fts_query_then_raw_sqlite_bm25",
                "top_n": TOP_N,
            },
            "dense": {
                "model_id": bge_model_id,
                "resolved_revision": bge_model_revision,
                "model_lock_fingerprint": bge_model_lock_fingerprint,
                "channel": "entity",
                "top_n": TOP_N,
            },
            "rrf": {"k": RRF_K, "union_order": "lexical_then_unseen_dense"},
            "exact_title": {
                "rule_fingerprint": exact_title_rule_fingerprint,
                "unique_byte_exact_full_eligible_corpus_only": True,
                "tie_break": "frozen_union_order_after_identity_check",
            },
        },
        "candidate": {
            "candidate_pool": "baseline_pre_output_top20",
            "bm25_weight": 1.5,
            "dense_weight": 1.0,
            "normalization": "per_query_minmax_over_immutable_pool",
            "tie_break": "descending_score_then_baseline_pool_order",
        },
        "query_count": query_count,
        "facet_quotas": dict(FACET_QUOTAS),
        "metric_contract": dict(METRIC_CONTRACT),
        "metric_contract_fingerprint": METRIC_CONTRACT_FINGERPRINT,
        "source_label_mapping": {
            "mapping": dict(SOURCE_LABEL_MAPPING),
            "fingerprint": SOURCE_LABEL_MAPPING_FINGERPRINT,
        },
        "diversity": {
            "minimum_topic_families": 14,
            "minimum_family_size": 1,
            "current_vs_superseded_family_count": 7,
            "current_vs_superseded_queries_per_family": 4,
        },
        "gates": {
            "zero_channel_failures": True,
            "exact_safety_non_inferior": True,
            "keyword_safety_non_inferior": True,
            "strict_negative_safety_non_inferior": True,
            "grade2_recall_at_20_non_inferior": True,
            "same_specific_fact_top1_non_inferior": True,
            "source_hit_at_1_10_20_non_inferior": True,
            "paired_family_statistic": {
                "method": "cluster_bootstrap_delta_ci_95",
                "resamples": bootstrap_resamples,
                "seed": bootstrap_seed,
                "candidate_minus_baseline_ci_low_at_least": minimum_ndcg_effect,
            },
            "minimum_ndcg_effect": minimum_ndcg_effect,
            "end_to_end_latency": {
                "p50_relative_increase_at_most": max_latency_p50_increase,
                "p95_relative_increase_at_most": max_latency_p95_increase,
                "unit": "milliseconds",
                "warmup_excluded": True,
                "schedule_seed": 11,
                "warmup_count": 2,
                "samples_per_measurement_min": 2,
            },
            "fallback": BASELINE_ARM,
        },
        "freshness": {
            "requires_external_attestation": True,
            "requires_query_id_and_topic_family_disjointness": True,
            "protected_inputs_are_never_read": True,
        },
        "machine_fingerprint": machine_fingerprint,
        "rubric_fingerprint": rubric_fingerprint,
    }
    if commit_id_sha256 is not None:
        payload["production"]["commit_id_sha256"] = commit_id_sha256
    return validate_fresh_development_spec(sign_artifact(SPEC_KIND, payload))


def validate_fresh_development_spec(artifact: object) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    try:
        value = validate_signed_artifact(artifact, kind=SPEC_KIND)
    except BakeoffContractError as exc:
        raise FreshDevelopmentError(str(exc)) from exc
    required = {
        "experiment_id",
        "arms",
        "production",
        "candidate",
        "query_count",
        "facet_quotas",
        "metric_contract",
        "metric_contract_fingerprint",
        "diversity",
        "gates",
        "freshness",
        "machine_fingerprint",
        "rubric_fingerprint",
        "source_label_mapping",
    }
    if set(value) - required - {"schema_version", "kind", "artifact_fingerprint"}:
        raise FreshDevelopmentError("spec contains unknown fields")
    if set(value) - {"schema_version", "kind", "artifact_fingerprint"} != required:
        raise FreshDevelopmentError("spec is missing required fields")
    if not _ID_RE.fullmatch(value["experiment_id"]):
        raise FreshDevelopmentError("spec experiment_id is invalid")
    _validate_arm_list(value["arms"])
    if value["query_count"] != 400 or value["facet_quotas"] != FACET_QUOTAS:
        raise FreshDevelopmentError("spec must freeze the exact 400-query facet quota")
    if (
        value["metric_contract"] != METRIC_CONTRACT
        or value["metric_contract_fingerprint"] != METRIC_CONTRACT_FINGERPRINT
        or value["metric_contract"].get("fingerprint")
        != _hash(
            {key: item for key, item in value["metric_contract"].items() if key != "fingerprint"}
        )
    ):
        raise FreshDevelopmentError("spec metric contract is not the canonical frozen contract")
    source_mapping = value["source_label_mapping"]
    if (
        not isinstance(source_mapping, dict)
        or source_mapping.get("mapping") != SOURCE_LABEL_MAPPING
        or source_mapping.get("fingerprint") != SOURCE_LABEL_MAPPING_FINGERPRINT
        or _hash(source_mapping["mapping"]) != source_mapping["fingerprint"]
    ):
        raise FreshDevelopmentError(
            "source-slot construction labels are not mapped to final facets"
        )
    if value["diversity"] != {
        "minimum_topic_families": 14,
        "minimum_family_size": 1,
        "current_vs_superseded_family_count": 7,
        "current_vs_superseded_queries_per_family": 4,
    }:
        raise FreshDevelopmentError("spec diversity contract is not frozen")
    production = value["production"]
    if not isinstance(production, dict) or not {
        "git_commit",
        "git_object_format",
        "corpus_snapshot_hash",
        "lexical",
        "dense",
        "rrf",
        "exact_title",
    }.issubset(production):
        raise FreshDevelopmentError("spec production section is malformed")
    if set(production) not in (
        {
            "git_commit",
            "git_object_format",
            "corpus_snapshot_hash",
            "lexical",
            "dense",
            "rrf",
            "exact_title",
        },
        {
            "git_commit",
            "git_object_format",
            "commit_id_sha256",
            "corpus_snapshot_hash",
            "lexical",
            "dense",
            "rrf",
            "exact_title",
        },
    ):
        raise FreshDevelopmentError("spec production identity fields are malformed")
    if production.get("git_object_format") not in {"sha1", "sha256"}:
        raise FreshDevelopmentError("production.git_object_format is unsupported")
    _git_commit(
        production.get("git_commit"),
        "production.git_commit",
        production.get("git_object_format"),
    )
    if "commit_id_sha256" in production:
        _hash_field(production["commit_id_sha256"], "production.commit_id_sha256")
        if production["commit_id_sha256"] != _hash_bytes(production["git_commit"].encode("ascii")):
            raise FreshDevelopmentError("production.commit_id_sha256 does not bind git_commit")
    _hash_field(production.get("corpus_snapshot_hash"), "production.corpus_snapshot_hash")
    lexical = production.get("lexical")
    dense = production.get("dense")
    rrf = production.get("rrf")
    exact = production.get("exact_title")
    if (
        not isinstance(lexical, dict)
        or set(lexical) != {"adapter_fingerprint", "policy", "top_n"}
        or lexical.get("policy") != "production_sanitize_fts_query_then_raw_sqlite_bm25"
        or lexical.get("top_n") != TOP_N
    ):
        raise FreshDevelopmentError("spec lexical behavior is not production-pinned")
    if (
        not isinstance(dense, dict)
        or set(dense)
        != {"model_id", "resolved_revision", "model_lock_fingerprint", "channel", "top_n"}
        or dense.get("model_id") != "BAAI/bge-small-en-v1.5"
        or dense.get("channel") != "entity"
        or dense.get("top_n") != TOP_N
    ):
        raise FreshDevelopmentError("spec dense behavior is not entity-pinned")
    if not isinstance(exact, dict) or set(exact) != {
        "rule_fingerprint",
        "unique_byte_exact_full_eligible_corpus_only",
        "tie_break",
    }:
        raise FreshDevelopmentError("spec exact-title behavior is malformed")
    for field, section in (
        ("adapter_fingerprint", lexical),
        ("model_lock_fingerprint", dense),
        ("rule_fingerprint", exact),
    ):
        _hash_field(section.get(field), f"production.{field}")
    # Git/Hugging Face resolved revisions are immutable identifiers, not
    # content hashes.  Keep their native 40-hex form and validate it as such.
    _immutable_id(dense.get("resolved_revision"), "production.dense.resolved_revision")
    if rrf != {"k": RRF_K, "union_order": "lexical_then_unseen_dense"}:
        raise FreshDevelopmentError("spec RRF behavior is not pinned")
    if (
        exact.get("unique_byte_exact_full_eligible_corpus_only") is not True
        or exact.get("tie_break") != "frozen_union_order_after_identity_check"
    ):
        raise FreshDevelopmentError("spec exact-title behavior is not pinned")
    candidate = value["candidate"]
    if candidate != {
        "candidate_pool": "baseline_pre_output_top20",
        "bm25_weight": 1.5,
        "dense_weight": 1.0,
        "normalization": "per_query_minmax_over_immutable_pool",
        "tie_break": "descending_score_then_baseline_pool_order",
    }:
        raise FreshDevelopmentError("candidate configuration is not the fixed 1.5:1 arm")
    gates = value["gates"]
    required_gates = {
        "zero_channel_failures",
        "exact_safety_non_inferior",
        "keyword_safety_non_inferior",
        "strict_negative_safety_non_inferior",
        "grade2_recall_at_20_non_inferior",
        "same_specific_fact_top1_non_inferior",
        "source_hit_at_1_10_20_non_inferior",
        "paired_family_statistic",
        "minimum_ndcg_effect",
        "end_to_end_latency",
        "fallback",
    }
    if not isinstance(gates, dict) or set(gates) != required_gates:
        raise FreshDevelopmentError("spec gates are incomplete")
    if (
        gates["fallback"] != BASELINE_ARM
        or not isinstance(gates["minimum_ndcg_effect"], (int, float))
        or isinstance(gates["minimum_ndcg_effect"], bool)
        or not math.isfinite(gates["minimum_ndcg_effect"])
        or gates["minimum_ndcg_effect"] < 0
    ):
        raise FreshDevelopmentError("spec minimum-effect/fallback gate is invalid")
    stat = gates["paired_family_statistic"]
    if (
        not isinstance(stat, dict)
        or stat.get("method") != "cluster_bootstrap_delta_ci_95"
        or not isinstance(stat.get("resamples"), int)
        or isinstance(stat.get("resamples"), bool)
        or stat.get("resamples") < 1000
        or not isinstance(stat.get("seed"), int)
        or isinstance(stat.get("seed"), bool)
        or stat.get("seed") < 0
        or stat.get("candidate_minus_baseline_ci_low_at_least") != gates["minimum_ndcg_effect"]
    ):
        raise FreshDevelopmentError("paired statistic and minimum effect are not bound")
    latency = gates["end_to_end_latency"]
    if (
        not isinstance(latency, dict)
        or latency.get("unit") != "milliseconds"
        or latency.get("warmup_excluded") is not True
        or latency.get("schedule_seed") != 11
        or latency.get("warmup_count") != 2
        or latency.get("samples_per_measurement_min") != 2
    ):
        raise FreshDevelopmentError("latency gate is not end-to-end/warm pinned")
    for field in ("p50_relative_increase_at_most", "p95_relative_increase_at_most"):
        if (
            not isinstance(latency.get(field), (int, float))
            or isinstance(latency.get(field), bool)
            or not math.isfinite(latency[field])
            or latency[field] < 0
        ):
            raise FreshDevelopmentError("latency thresholds must be finite and non-negative")
    if any(
        gates.get(name) is not True
        for name in required_gates
        - {"paired_family_statistic", "minimum_ndcg_effect", "end_to_end_latency", "fallback"}
    ):
        raise FreshDevelopmentError("all preregistered boolean gates must be true")
    if value["freshness"] != {
        "requires_external_attestation": True,
        "requires_query_id_and_topic_family_disjointness": True,
        "protected_inputs_are_never_read": True,
    }:
        raise FreshDevelopmentError("freshness boundary is incomplete")
    _hash_field(value["machine_fingerprint"], "machine_fingerprint")
    _hash_field(value["rubric_fingerprint"], "rubric_fingerprint")
    return value


def build_fresh_query_manifest(  # noqa: C901, PLR0912, PLR0915
    records: Sequence[Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    source_artifact_id: str,
    source_artifact_fingerprint: str,
    source_kind: str,
    protected_query_ids: Sequence[str],
    protected_topic_family_ids: Sequence[str],
    protected_source_entity_ids: Sequence[str],
    prior_assignment_fingerprint: str,
    protected_query_manifest_fingerprint: str,
) -> dict[str, Any]:
    """Sign a fresh manifest from caller-provided unprotected records.

    ``protected_*`` values are opaque attestations supplied by the custody owner.  This function
    compares IDs only; it never opens, lists, or resolves the protected source.
    """
    frozen_spec = validate_fresh_development_spec(spec)
    _reject_protected_text(source_artifact_id, "source_artifact_id")
    _reject_protected_text(source_kind, "source_kind")
    _text(source_artifact_id, "source_artifact_id")
    _text(source_kind, "source_kind")
    if "blind" in source_kind.lower() or source_kind.lower().startswith("vault"):
        raise FreshDevelopmentError("fresh source kind is protected")
    _hash_field(source_artifact_fingerprint, "source_artifact_fingerprint")
    _hash_field(prior_assignment_fingerprint, "prior_assignment_fingerprint")
    _hash_field(protected_query_manifest_fingerprint, "protected_query_manifest_fingerprint")
    expected = frozen_spec["query_count"]
    if len(records) != expected:
        raise FreshDevelopmentError(f"fresh manifest must contain exactly {expected} queries")
    protected_ids = set(protected_query_ids)
    protected_families = set(protected_topic_family_ids)
    protected_sources = set(protected_source_entity_ids)
    source_overlap_count = 0
    queries: list[dict[str, Any]] = []
    ids: set[str] = set()
    query_texts: set[str] = set()
    families: set[str] = set()
    for row in records:
        if not isinstance(row, Mapping):
            raise FreshDevelopmentError("fresh query record must be an object")
        query_id = row.get("id")
        family_id = row.get("topic_family_id")
        query = row.get("query")
        if not isinstance(query_id, str) or not _ID_RE.fullmatch(query_id) or query_id in ids:
            raise FreshDevelopmentError("fresh query IDs must be unique and valid")
        if query_id in protected_ids:
            raise FreshDevelopmentError("fresh query ID overlaps protected query IDs")
        if not isinstance(family_id, str) or not family_id:
            raise FreshDevelopmentError("fresh topic_family_id must be non-empty")
        if family_id in protected_families:
            raise FreshDevelopmentError("fresh topic family overlaps protected families")
        if not isinstance(query, str) or not query.strip():
            raise FreshDevelopmentError("fresh query text must be non-empty")
        if query in query_texts:
            raise FreshDevelopmentError("fresh query text must be byte-unique")
        if set(row) - {
            "id",
            "query",
            "category",
            "subtype",
            "language",
            "topic_family_id",
            "source_entity_ids",
        }:
            raise FreshDevelopmentError("fresh query has fields outside the final manifest schema")
        category = row.get("category")
        if category not in FACET_QUOTAS:
            raise FreshDevelopmentError("fresh query uses an unregistered facet")
        subtype = row.get("subtype")
        if subtype not in SUBTYPE_QUOTAS[category]:
            raise FreshDevelopmentError("fresh query uses an unregistered subtype")
        if category == "multilingual" and row.get("language") not in {
            "latin_non_english",
            "cyrillic",
            "rtl_arabic",
            "cjk",
        }:
            raise FreshDevelopmentError("multilingual queries require an allowed language family")
        if category != "multilingual" and "language" in row:
            raise FreshDevelopmentError("language is only allowed on multilingual queries")
        source_ids = row.get("source_entity_ids", [])
        if not isinstance(source_ids, list) or any(
            not isinstance(source_id, str) or not source_id for source_id in source_ids
        ):
            raise FreshDevelopmentError("source_entity_ids must be a list of non-empty IDs")
        if len(source_ids) != len(set(source_ids)):
            raise FreshDevelopmentError("source_entity_ids must not contain duplicates")
        if category in POSITIVE_FACETS and not source_ids:
            raise FreshDevelopmentError("positive fresh facets require meaningful source IDs")
        if category == "strict_negative" and source_ids:
            raise FreshDevelopmentError("strict-negative fresh queries must be source-free")
        source_overlap_count += len(set(source_ids) & protected_sources)
        clean = dict(row)
        clean["split"] = "fresh_dev"
        clean["provenance"] = "external_unprotected_source"
        ids.add(query_id)
        query_texts.add(query)
        families.add(family_id)
        queries.append(clean)
    counts = {facet: sum(row["category"] == facet for row in queries) for facet in FACET_QUOTAS}
    if counts != FACET_QUOTAS:
        raise FreshDevelopmentError("fresh manifest does not satisfy the exact facet quota")
    subtype_counts = {
        facet: {
            subtype: sum(row["category"] == facet and row["subtype"] == subtype for row in queries)
            for subtype in subtypes
        }
        for facet, subtypes in SUBTYPE_QUOTAS.items()
    }
    if subtype_counts != SUBTYPE_QUOTAS:
        raise FreshDevelopmentError("fresh manifest does not satisfy the frozen subtype quota")
    if len(families) < frozen_spec["diversity"]["minimum_topic_families"]:
        raise FreshDevelopmentError("fresh manifest has too few topic families")
    current = [row for row in queries if row["category"] == "current_vs_superseded"]
    current_families = defaultdict(int)
    for row in current:
        current_families[row["topic_family_id"]] += 1
    if set(current_families.values()) != {4} or len(current_families) != 7:
        raise FreshDevelopmentError(
            "current-vs-superseded must disclose seven repeated four-query families"
        )
    query_fp = _hash(queries)
    payload = {
        "experiment_id": frozen_spec["experiment_id"],
        "spec_fingerprint": frozen_spec["artifact_fingerprint"],
        "queries": queries,
        "queries_fingerprint": query_fp,
        "source_attestation": {
            "artifact_id": source_artifact_id,
            "artifact_fingerprint": source_artifact_fingerprint,
            "kind": source_kind,
            "protected_query_manifest_fingerprint": protected_query_manifest_fingerprint,
            "prior_assignment_fingerprint": prior_assignment_fingerprint,
            "query_id_overlap_count": 0,
            "topic_family_overlap_count": 0,
            "source_entity_overlap_count": source_overlap_count,
            "disjointness_method": "caller_attested_opaque_protected_ids",
        },
    }
    return validate_fresh_query_manifest(sign_artifact(MANIFEST_KIND, payload), frozen_spec)


def _manifest_row_valid(row: Mapping[str, Any]) -> bool:
    category = row.get("category")
    expected_fields = {
        "id",
        "query",
        "category",
        "subtype",
        "topic_family_id",
        "source_entity_ids",
        "split",
        "provenance",
    }
    if category == "multilingual":
        expected_fields.add("language")
    source_ids = row.get("source_entity_ids", [])
    return bool(
        set(row) == expected_fields
        and isinstance(row.get("id"), str)
        and _ID_RE.fullmatch(row["id"]) is not None
        and isinstance(row.get("query"), str)
        and bool(row["query"].strip())
        and row.get("split") == "fresh_dev"
        and row.get("provenance") == "external_unprotected_source"
        and category in FACET_QUOTAS
        and row.get("subtype") in SUBTYPE_QUOTAS.get(category, {})
        and (
            category != "multilingual"
            or row.get("language") in {"latin_non_english", "cyrillic", "rtl_arabic", "cjk"}
        )
        and isinstance(row.get("topic_family_id"), str)
        and bool(row["topic_family_id"])
        and isinstance(source_ids, list)
        and all(isinstance(source_id, str) and bool(source_id) for source_id in source_ids)
        and len(source_ids) == len(set(source_ids))
        and (category not in POSITIVE_FACETS or bool(source_ids))
        and (category != "strict_negative" or not source_ids)
    )


def validate_fresh_query_manifest(artifact: object, spec: Mapping[str, Any]) -> dict[str, Any]:  # noqa: C901, PLR0912, PLR0915
    frozen_spec = validate_fresh_development_spec(spec)
    try:
        value = validate_signed_artifact(artifact, kind=MANIFEST_KIND)
    except BakeoffContractError as exc:
        raise FreshDevelopmentError(str(exc)) from exc
    required = {
        "experiment_id",
        "spec_fingerprint",
        "queries",
        "queries_fingerprint",
        "source_attestation",
    }
    if (
        set(value) - required - {"schema_version", "kind", "artifact_fingerprint"}
        or set(value) - {"schema_version", "kind", "artifact_fingerprint"} != required
    ):
        raise FreshDevelopmentError("fresh query manifest fields are incomplete")
    if (
        value["experiment_id"] != frozen_spec["experiment_id"]
        or value["spec_fingerprint"] != frozen_spec["artifact_fingerprint"]
    ):
        raise FreshDevelopmentError("fresh query manifest is bound to another spec")
    queries = value["queries"]
    if (
        not isinstance(queries, list)
        or len(queries) != frozen_spec["query_count"]
        or value["queries_fingerprint"] != _hash(queries)
    ):
        raise FreshDevelopmentError("fresh query manifest content fingerprint/count mismatch")
    if any(not isinstance(row, dict) for row in queries):
        raise FreshDevelopmentError("fresh query manifest contains a non-object query")
    ids = [row.get("id") for row in queries]
    families = [row.get("topic_family_id") for row in queries]
    query_texts = [row.get("query") for row in queries]
    if (
        any(not isinstance(query_id, str) for query_id in ids)
        or any(not isinstance(family, str) for family in families)
        or len(ids) != len(set(ids))
        or any(not family for family in families)
        or any(not isinstance(query, str) for query in query_texts)
        or len(query_texts) != len(set(query_texts))
        or any(not _manifest_row_valid(row) for row in queries)
    ):
        raise FreshDevelopmentError(
            "fresh query manifest has duplicate IDs, empty families, or wrong split"
        )
    if {
        facet: {
            subtype: sum(row["category"] == facet and row["subtype"] == subtype for row in queries)
            for subtype in subtypes
        }
        for facet, subtypes in SUBTYPE_QUOTAS.items()
    } != SUBTYPE_QUOTAS:
        raise FreshDevelopmentError("fresh query manifest subtype quota mismatch")
    if len(set(families)) < frozen_spec["diversity"]["minimum_topic_families"]:
        raise FreshDevelopmentError("fresh query manifest has too few topic families")
    current_counts: dict[str, int] = defaultdict(int)
    for row in queries:
        if row["category"] == "current_vs_superseded":
            current_counts[row["topic_family_id"]] += 1
    if set(current_counts.values()) != {4} or len(current_counts) != 7:
        raise FreshDevelopmentError("current-vs-superseded family quota is incomplete")
    attestation = value["source_attestation"]
    attestation_keys = {
        "artifact_id",
        "artifact_fingerprint",
        "kind",
        "protected_query_manifest_fingerprint",
        "prior_assignment_fingerprint",
        "query_id_overlap_count",
        "topic_family_overlap_count",
        "source_entity_overlap_count",
        "disjointness_method",
    }
    if not isinstance(attestation, dict) or set(attestation) != attestation_keys:
        raise FreshDevelopmentError("fresh source attestation is incomplete")
    if (
        not isinstance(attestation["artifact_id"], str)
        or not attestation["artifact_id"].strip()
        or not isinstance(attestation["kind"], str)
        or not attestation["kind"].strip()
    ):
        raise FreshDevelopmentError("fresh source attestation artifact identity is empty")
    _reject_protected_text(attestation["artifact_id"], "source attestation artifact_id")
    _reject_protected_text(attestation["kind"], "source attestation kind")
    _hash_field(attestation["artifact_fingerprint"], "source attestation artifact_fingerprint")
    _hash_field(
        attestation["protected_query_manifest_fingerprint"],
        "source attestation protected fingerprint",
    )
    _hash_field(attestation["prior_assignment_fingerprint"], "source attestation prior assignment")
    if (
        attestation["query_id_overlap_count"] != 0
        or attestation["topic_family_overlap_count"] != 0
        or attestation["source_entity_overlap_count"] != 0
        or attestation["disjointness_method"] != "caller_attested_opaque_protected_ids"
    ):
        raise FreshDevelopmentError(
            "freshness disjointness attestation does not prove zero overlap"
        )
    return value


def build_query_review_packets(
    manifest: Mapping[str, Any], spec: Mapping[str, Any], *, packet_size: int = 20
) -> dict[str, Any]:
    """Build post-generation query-review packets without leaking source IDs or ranking configuration.

    Review packet consumers receive only the query text and its neutral category.  Retrieval arms,
    source entities, labels, and ranking configuration stay in the evaluator's separate inputs.
    """
    frozen_manifest = validate_fresh_query_manifest(manifest, spec)
    if not isinstance(packet_size, int) or isinstance(packet_size, bool) or packet_size <= 0:
        raise FreshDevelopmentError("packet_size must be a positive integer")
    packets: list[dict[str, Any]] = []
    queries = frozen_manifest["queries"]
    for start in range(0, len(queries), packet_size):
        rows = []
        for query in queries[start : start + packet_size]:
            rows.append(
                {
                    "query_id": query["id"],
                    "query": query["query"],
                    "category": query.get("category", "unknown"),
                }
            )
        packets.append({"packet_id": f"packet-{len(packets):04d}", "queries": rows})
    payload = {
        "experiment_id": spec["experiment_id"],
        "spec_fingerprint": spec["artifact_fingerprint"],
        "manifest_fingerprint": frozen_manifest["artifact_fingerprint"],
        "packet_size": packet_size,
        "packets": packets,
    }
    return validate_fresh_query_packets(sign_artifact(PACKETS_KIND, payload), spec, frozen_manifest)


def validate_fresh_query_packets(
    artifact: object, spec: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    try:
        value = validate_signed_artifact(artifact, kind=PACKETS_KIND)
    except BakeoffContractError as exc:
        raise FreshDevelopmentError(str(exc)) from exc
    required = {
        "experiment_id",
        "spec_fingerprint",
        "manifest_fingerprint",
        "packet_size",
        "packets",
    }
    if (
        set(value) - required - {"schema_version", "kind", "artifact_fingerprint"}
        or set(value) - {"schema_version", "kind", "artifact_fingerprint"} != required
    ):
        raise FreshDevelopmentError("fresh packet artifact fields are incomplete")
    if (
        value["experiment_id"] != frozen_spec["experiment_id"]
        or value["spec_fingerprint"] != frozen_spec["artifact_fingerprint"]
        or value["manifest_fingerprint"] != frozen_manifest["artifact_fingerprint"]
    ):
        raise FreshDevelopmentError("fresh packets are bound to another spec or manifest")
    packet_size = value["packet_size"]
    packets = value["packets"]
    if (
        not isinstance(packet_size, int)
        or packet_size <= 0
        or not isinstance(packets, list)
        or not packets
    ):
        raise FreshDevelopmentError("fresh packets are empty or malformed")
    expected_ids = {query["id"] for query in frozen_manifest["queries"]}
    seen_ids: set[str] = set()
    for packet in packets:
        if (
            not isinstance(packet, dict)
            or set(packet) != {"packet_id", "queries"}
            or not isinstance(packet["queries"], list)
            or not packet["queries"]
        ):
            raise FreshDevelopmentError("fresh packet row is malformed")
        for row in packet["queries"]:
            if not isinstance(row, dict) or set(row) != {"query_id", "query", "category"}:
                raise FreshDevelopmentError("fresh packet leaks metadata or has malformed fields")
            if (
                row["query_id"] in seen_ids
                or row["query_id"] not in expected_ids
                or not isinstance(row["query"], str)
                or not row["query"].strip()
            ):
                raise FreshDevelopmentError(
                    "fresh packet query IDs do not cover the manifest exactly"
                )
            seen_ids.add(row["query_id"])
    if seen_ids != expected_ids:
        raise FreshDevelopmentError("fresh packets do not cover every query exactly once")
    return value


def build_relevance_judge_packets(
    manifest: Mapping[str, Any],
    spec: Mapping[str, Any],
    rankings: Mapping[str, Mapping[str, Sequence[str]]],
    candidate_texts: Mapping[str, Mapping[str, Mapping[str, str]]],
    *,
    base_seed: int = 11,
) -> dict[str, Any]:
    """Build three randomized relevance packets plus private mappings.

    The public packets are produced by :mod:`judge_pool`; they contain redacted excerpts and
    anonymous candidate labels only.  The private mapping is retained by the caller for label
    ingestion.  Candidate pools are exactly the union of both returned arms, so no returned ID
    can silently receive an unlabelled zero.
    """
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    _validate_rankings(frozen_manifest["queries"], rankings)
    query_ids = {query["id"] for query in frozen_manifest["queries"]}
    if set(candidate_texts) != query_ids:
        raise FreshDevelopmentError("candidate excerpts must cover every fresh query")
    pools: dict[str, dict[str, Mapping[str, str]]] = {}
    for query_id in sorted(query_ids):
        union = list(
            dict.fromkeys(
                list(rankings[BASELINE_ARM][query_id]) + list(rankings[CANDIDATE_ARM][query_id])
            )
        )
        if set(candidate_texts[query_id]) != set(union):
            raise FreshDevelopmentError("candidate excerpts must exactly cover the two-arm union")
        pools[query_id] = {
            candidate_id: dict(candidate_texts[query_id][candidate_id]) for candidate_id in union
        }
    query_rows = [{**query, "split": "dev"} for query in frozen_manifest["queries"]]
    matrix = {"pools": pools, "kind": "FreshDevelopmentJudgingMatrix"}
    packets: dict[str, dict[str, Any]] = {}
    mappings: dict[str, dict[str, Any]] = {}
    for judge in ADDENDUM_JUDGES:
        try:
            packet, private = build_judge_packets(
                query_rows, matrix, judge, "dev", base_seed=base_seed
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FreshDevelopmentError("failed to build relevance judging packet") from exc
        packets[judge] = packet
        mappings[judge] = private
    return {
        "kind": "FreshDevelopmentRelevanceJudgePackets",
        "spec_fingerprint": frozen_spec["artifact_fingerprint"],
        "manifest_fingerprint": frozen_manifest["artifact_fingerprint"],
        "candidate_union_fingerprint": _hash(pools),
        "judges": list(ADDENDUM_JUDGES),
        "packets": packets,
        "private_mappings": mappings,
    }


def _validate_content_receipt(value: object, kind: str) -> dict[str, Any]:
    """Validate integrity only; content fingerprints are not signer authentication."""
    try:
        receipt = validate_signed_artifact(value, kind=kind)
    except BakeoffContractError as exc:
        raise FreshDevelopmentError(f"invalid {kind} integrity receipt") from exc
    return receipt


def _derive_rankings_from_retrieval(  # noqa: C901, PLR0912, PLR0915
    retrieval: Mapping[str, Any], *, spec: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, dict[str, list[str]]]:
    """Validate raw channel evidence and deterministically derive both arm rankings."""
    if spec and (
        spec.get("candidate")
        != {
            "candidate_pool": "baseline_pre_output_top20",
            "bm25_weight": 1.5,
            "dense_weight": 1.0,
            "normalization": "per_query_minmax_over_immutable_pool",
            "tie_break": "descending_score_then_baseline_pool_order",
        }
        or spec.get("production", {}).get("rrf")
        != {"k": RRF_K, "union_order": "lexical_then_unseen_dense"}
    ):
        raise FreshDevelopmentError(
            "retrieval derivation is not bound to the frozen production config"
        )
    if not isinstance(retrieval, Mapping) or set(retrieval) != {"cells", "fingerprint"}:
        raise FreshDevelopmentError("retrieval evidence receipt is incomplete")
    cells = retrieval["cells"]
    if not isinstance(cells, Mapping) or retrieval["fingerprint"] != _hash(cells):
        raise FreshDevelopmentError("retrieval evidence fingerprint/query coverage mismatch")
    query_rows = {query["id"]: query for query in manifest["queries"]}
    if set(cells) != set(query_rows):
        raise FreshDevelopmentError("retrieval evidence must cover every fresh query exactly")
    baseline: dict[str, list[str]] = {}
    candidate: dict[str, list[str]] = {}
    for query_id in sorted(query_rows):
        cell = cells[query_id]
        if not isinstance(cell, Mapping) or set(cell) != {"lexical", "dense", "exact_title"}:
            raise FreshDevelopmentError("retrieval evidence cell has unknown fields")
        channels: dict[str, tuple[list[str], list[float]]] = {}
        for channel in ("lexical", "dense"):
            value = cell[channel]
            score_key = "raw_bm25_scores" if channel == "lexical" else "scores"
            if (
                not isinstance(value, Mapping)
                or set(value) != {"ids", score_key}
                or not isinstance(value["ids"], list)
                or not isinstance(value[score_key], list)
                or len(value["ids"]) != len(value[score_key])
                or len(value["ids"]) > TOP_N
                or len(value["ids"]) != len(set(value["ids"]))
                or any(not isinstance(item, str) or not item for item in value["ids"])
                or any(
                    not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not math.isfinite(score)
                    for score in value[score_key]
                )
            ):
                raise FreshDevelopmentError("retrieval channel IDs/scores are malformed")
            channels[channel] = (
                value["ids"],
                value[score_key],
            )
        diagnostics = cell["exact_title"]
        if (
            not isinstance(diagnostics, Mapping)
            or set(diagnostics)
            != {
                "triggered",
                "match_ids",
                "output_id",
                "output_rank",
                "unique_corpus_match",
            }
            or not isinstance(diagnostics["triggered"], bool)
            or not isinstance(diagnostics["match_ids"], list)
            or any(not isinstance(item, str) or not item for item in diagnostics["match_ids"])
            or (
                diagnostics["output_id"] is not None
                and (not isinstance(diagnostics["output_id"], str) or not diagnostics["output_id"])
            )
            or (
                diagnostics["output_rank"] is not None
                and (
                    not isinstance(diagnostics["output_rank"], int)
                    or isinstance(diagnostics["output_rank"], bool)
                    or diagnostics["output_rank"] < 1
                )
            )
            or not isinstance(diagnostics["unique_corpus_match"], bool)
        ):
            raise FreshDevelopmentError("exact-title diagnostics are malformed")
        query = query_rows[query_id]
        if query["category"] == "exact_title":
            if query["subtype"] == "unique_byte_exact_singleton":
                if (
                    diagnostics["triggered"] is not True
                    or len(diagnostics["match_ids"]) != 1
                    or len(query.get("source_entity_ids", [])) != 1
                    or diagnostics["match_ids"][0] != query["source_entity_ids"][0]
                    or diagnostics["output_id"] != diagnostics["match_ids"][0]
                    or diagnostics["output_rank"] != 1
                    or diagnostics["unique_corpus_match"] is not True
                ):
                    raise FreshDevelopmentError(
                        "exact-title singleton must trigger exactly once at rank 1"
                    )
            elif (
                diagnostics["triggered"]
                or diagnostics["match_ids"]
                or diagnostics["output_id"] is not None
                or diagnostics["output_rank"] is not None
                or diagnostics["unique_corpus_match"] is not False
            ):
                raise FreshDevelopmentError(
                    "byte-mismatch controls must have zero exact matches and fall through"
                )
        elif (
            diagnostics["triggered"]
            or diagnostics["match_ids"]
            or diagnostics["output_id"] is not None
            or diagnostics["output_rank"] is not None
            or diagnostics["unique_corpus_match"] is not False
        ):
            raise FreshDevelopmentError(
                "exact-title fast path is only allowed in exact-title cells"
            )
        lexical_ids, lexical_scores = channels["lexical"]
        dense_ids, dense_scores = channels["dense"]
        union = list(dict.fromkeys(lexical_ids + dense_ids))
        if diagnostics["triggered"]:
            output_id = diagnostics["output_id"]
            if output_id != query.get("source_entity_ids", [None])[0]:
                raise FreshDevelopmentError(
                    "exact-title singleton output is not the predeclared source"
                )
            baseline[query_id] = candidate[query_id] = [output_id]
            continue
        if not union:
            raise FreshDevelopmentError("both retrieval channels are empty")
        lexical_rank = {item: index + 1 for index, item in enumerate(lexical_ids)}
        dense_rank = {item: index + 1 for index, item in enumerate(dense_ids)}
        rrf_scores = {
            item: (1 / (RRF_K + lexical_rank[item]) if item in lexical_rank else 0)
            + (1 / (RRF_K + dense_rank[item]) if item in dense_rank else 0)
            for item in union
        }
        rrf = sorted(union, key=lambda item: (-rrf_scores[item], union.index(item)))[:TOP_N]
        lexical_score = {item: -score for item, score in zip(lexical_ids, lexical_scores)}
        dense_score = dict(zip(dense_ids, dense_scores))

        def channel_feature(scores: Mapping[str, float]) -> dict[str, float]:
            if not scores:
                return dict.fromkeys(union, 0.0)
            low, high = min(scores.values()), max(scores.values())
            span = high - low
            floor = low - max(1.0, abs(span)) * 1e-6
            if span == 0:
                return {item: (1.0 if item in scores else 0.0) for item in union}
            return {item: (scores.get(item, floor) - low) / span for item in union}

        def normalize_all(values: Mapping[str, float]) -> dict[str, float]:
            low, high = min(values.values()), max(values.values())
            if high == low:
                return dict.fromkeys(values, 0.0)
            return {item: (value - low) / (high - low) for item, value in values.items()}

        lex_features = channel_feature(lexical_score)
        dense_features = channel_feature(dense_score)
        rrf_pool = rrf
        lex_norm = normalize_all({item: lex_features[item] for item in rrf_pool})
        dense_norm = normalize_all({item: dense_features[item] for item in rrf_pool})
        rerank_scores = {
            item: 1.5 * lex_norm.get(item, 0.0) + dense_norm.get(item, 0.0) for item in rrf
        }
        reranked = sorted(rrf, key=lambda item: (-rerank_scores[item], rrf.index(item)))[:TOP_N]
        baseline[query_id], candidate[query_id] = rrf, reranked
    return {BASELINE_ARM: baseline, CANDIDATE_ARM: candidate}


def _expected_measurement_schedule(
    query_ids: set[str], seed: int
) -> tuple[list[str], list[tuple[str, str]]]:
    rng = random.Random(seed)
    warmups = rng.sample(list(ARMS), len(ARMS))
    ordered_queries = sorted(query_ids)
    rng.shuffle(ordered_queries)
    arm_order = rng.sample(list(ARMS), len(ARMS))
    return warmups, [(query_id, arm) for query_id in ordered_queries for arm in arm_order]


def _validate_timing_trace(  # noqa: C901, PLR0912
    trace: object, *, query_ids: set[str], environment_fingerprint: str, schedule_seed: int = 11
) -> dict[str, Any]:
    if not isinstance(trace, dict):
        raise FreshDevelopmentError("timing trace is missing")
    required = {"warmups", "measurements", "schedule", "environment_fingerprint", "schedule_seed"}
    if set(trace) != required or trace["environment_fingerprint"] != environment_fingerprint:
        raise FreshDevelopmentError("timing trace fields/environment do not match receipt")
    if trace["schedule_seed"] != schedule_seed:
        raise FreshDevelopmentError("timing schedule seed is not the preregistered seed")
    if not isinstance(trace["warmups"], list) or len(trace["warmups"]) != 2:
        raise FreshDevelopmentError("timing trace requires exactly two explicit warmups")
    expected_warmups, _ = _expected_measurement_schedule(query_ids, schedule_seed)
    if [row.get("arm") for row in trace["warmups"] if isinstance(row, dict)] != expected_warmups:
        raise FreshDevelopmentError("warmup order is not the deterministic preregistered order")
    measurements = trace["measurements"]
    schedule = trace["schedule"]
    if not isinstance(measurements, list) or not isinstance(schedule, list):
        raise FreshDevelopmentError("timing trace samples/schedule are malformed")
    if any(not isinstance(row, dict) for row in measurements):
        raise FreshDevelopmentError("timing measurement rows are malformed")
    _, expected = _expected_measurement_schedule(query_ids, schedule_seed)
    if sorted((row.get("query_id"), row.get("arm")) for row in measurements) != sorted(expected):
        raise FreshDevelopmentError(
            "timing trace must contain one measured execution per arm/query"
        )
    if any(not isinstance(item, (list, tuple)) or len(item) != 2 for item in schedule):
        raise FreshDevelopmentError("timing schedule rows are malformed")
    if len(schedule) != len(measurements) or [tuple(item) for item in schedule] != [
        (row["query_id"], row["arm"]) for row in measurements
    ]:
        raise FreshDevelopmentError("timing schedule is not an interleaved execution trace")
    if any(schedule[index][1] == schedule[index - 1][1] for index in range(1, len(schedule))):
        raise FreshDevelopmentError("timing schedule is not interleaved by arm")
    seen: set[tuple[str, str]] = set()
    for row in measurements:
        if (
            not isinstance(row, dict)
            or row.get("arm") not in ARMS
            or row.get("query_id") not in query_ids
        ):
            raise FreshDevelopmentError("timing measurement has an unknown arm/query")
        key = (row["query_id"], row["arm"])
        if key in seen:
            raise FreshDevelopmentError("timing trace replays an arm/query measurement")
        seen.add(key)
        samples = row.get("samples")
        if (
            not isinstance(samples, list)
            or len(samples) < 2
            or any(
                not isinstance(sample, (int, float))
                or isinstance(sample, bool)
                or not math.isfinite(sample)
                or sample < 0
                for sample in samples
            )
        ):
            raise FreshDevelopmentError(
                "timing measurements require >=2 finite non-negative samples"
            )
    for warmup in trace["warmups"]:
        samples = warmup.get("samples") if isinstance(warmup, dict) else None
        if warmup.get("arm") not in ARMS if isinstance(warmup, dict) else True:
            raise FreshDevelopmentError("timing warmup has an unknown arm")
        if (
            not isinstance(samples, list)
            or not samples
            or any(
                not isinstance(sample, (int, float))
                or isinstance(sample, bool)
                or not math.isfinite(sample)
                or sample < 0
                for sample in samples
            )
        ):
            raise FreshDevelopmentError("timing warmups contain invalid samples")
    return trace


def _validate_evidence(  # noqa: C901, PLR0912, PLR0915
    evidence: Mapping[str, Any],
    *,
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    rankings: Mapping[str, Mapping[str, Sequence[str]]],
) -> tuple[dict[str, dict[str, int]], dict[str, Any], dict[str, int]]:
    """Validate and recompute labels/timing from immutable content-addressed evidence."""
    required = {
        "retrieval_evidence",
        "judging_matrix",
        "raw_judgment_artifacts",
        "arbitration_response",
        "merged_labels",
        "adjudication_receipt",
        "timing_trace",
        "production_receipt",
        "execution_receipt",
        "channel_failures",
    }
    if set(evidence) != required:
        raise FreshDevelopmentError("decision evidence is incomplete")
    derived_rankings = _derive_rankings_from_retrieval(
        evidence["retrieval_evidence"], spec=spec, manifest=manifest
    )
    if derived_rankings != rankings:
        raise FreshDevelopmentError(
            "rankings do not deterministically derive from raw retrieval evidence"
        )
    queries = manifest["queries"]
    query_ids = {query["id"] for query in queries}
    matrix = evidence["judging_matrix"]
    if not isinstance(matrix, dict) or set(matrix) != {"pools", "fingerprint"}:
        raise FreshDevelopmentError("judging matrix receipt is incomplete")
    pools = matrix["pools"]
    if matrix["fingerprint"] != _hash(pools) or set(pools) != query_ids:
        raise FreshDevelopmentError("judging matrix fingerprint/query coverage mismatch")
    expected_pools = {}
    for query_id in sorted(query_ids):
        union = list(
            dict.fromkeys(
                list(rankings[BASELINE_ARM][query_id]) + list(rankings[CANDIDATE_ARM][query_id])
            )
        )
        expected_pools[query_id] = union
        if set(pools[query_id]) != set(union) or len(pools[query_id]) != len(union):
            raise FreshDevelopmentError("judging matrix is not the exact union of both arms")
    raw = evidence["raw_judgment_artifacts"]
    if not isinstance(raw, list) or len(raw) != 3:
        raise FreshDevelopmentError("exactly three raw judge artifacts are required")
    merged = merge_all_judgments(
        queries,
        raw,
        {
            "pools": {
                query_id: {candidate_id: {} for candidate_id in ids}
                for query_id, ids in expected_pools.items()
            }
        },
        judges=ADDENDUM_JUDGES,
    )
    arbitration = evidence["arbitration_response"]
    if not isinstance(arbitration, dict):
        raise FreshDevelopmentError("arbitration response is missing")
    arbitration_unsigned = dict(arbitration)
    arbitration_unsigned.pop("fingerprint", None)
    if arbitration.get("fingerprint") != _hash(arbitration_unsigned):
        raise FreshDevelopmentError("arbitration response integrity fingerprint mismatch")
    try:
        merged = apply_arbitration_results(merged, arbitration)
    except (KeyError, TypeError, ValueError) as exc:
        raise FreshDevelopmentError("arbitration response is incomplete or invalid") from exc
    recomputed_merged = merged_artifact(
        merged,
        raw,
        queries,
        judges=ADDENDUM_JUDGES,
        binding={"matrix_fingerprint": matrix["fingerprint"]},
    )
    merged_value = evidence["merged_labels"]
    if not isinstance(merged_value, dict) or merged_value.get("raw_labels_fingerprint") != _hash(
        raw
    ):
        raise FreshDevelopmentError("merged labels do not bind the raw judge artifacts")
    if merged_value.get("kind") != "DevelopmentJudgingAddendumLabels":
        raise FreshDevelopmentError("merged labels lack the addendum provenance kind")
    merged_unsigned = dict(merged_value)
    merged_unsigned.pop("fingerprint", None)
    if merged_value.get("fingerprint") != _hash(merged_unsigned):
        raise FreshDevelopmentError("merged labels integrity fingerprint mismatch")
    labels = merged_value.get("labels")
    expected_labels = [
        {
            "query_id": item.query_id,
            "candidate_id": item.candidate_id,
            "raw_grades": item.raw_grades,
            "median_grade": item.median_grade,
            "escalated": item.escalated,
            "escalation_reason": item.escalation_reason,
            "arbitrated_grade": item.arbitrated_grade,
            "final_grade": item.final_grade,
        }
        for item in merged
    ]
    calibration = merged_value.get("calibration")
    expected_calibration = recomputed_merged.get("calibration")
    calibration_matches = (
        isinstance(calibration, dict)
        and isinstance(expected_calibration, dict)
        and calibration.get("n") == expected_calibration.get("n")
        and (
            calibration.get("accuracy") == expected_calibration.get("accuracy")
            or (
                isinstance(calibration.get("accuracy"), float)
                and isinstance(expected_calibration.get("accuracy"), float)
                and math.isnan(calibration["accuracy"])
                and math.isnan(expected_calibration["accuracy"])
            )
        )
    )
    merged_without_calibration = dict(merged_value)
    expected_without_calibration = dict(recomputed_merged)
    merged_without_calibration.pop("calibration", None)
    expected_without_calibration.pop("calibration", None)
    if (
        merged_without_calibration != expected_without_calibration
        or not calibration_matches
        or labels != expected_labels
        or merged_value.get("judges") != list(ADDENDUM_JUDGES)
        or merged_value.get("adjudicator") != "agent_eval_adjudicator"
    ):
        raise FreshDevelopmentError("merged labels/provenance do not recompute from raw artifacts")
    adjudication = _validate_content_receipt(
        evidence["adjudication_receipt"], "AdjudicationReceipt"
    )
    if (
        adjudication.get("raw_labels_fingerprint") != _hash(raw)
        or adjudication.get("arbitration_response_fingerprint") != _hash(arbitration)
        or adjudication.get("merged_labels_fingerprint") != _hash(merged_value)
    ):
        raise FreshDevelopmentError("adjudication receipt does not bind raw and merged labels")
    production = _validate_content_receipt(
        evidence["production_receipt"], "ProductionConfigReceipt"
    )
    if (
        production.get("production") != spec["production"]
        or production.get("candidate") != spec["candidate"]
    ):
        raise FreshDevelopmentError("production receipt is bound to a different spec")
    environment = production.get("environment_fingerprint")
    _hash_field(environment, "production environment fingerprint")
    _validate_timing_trace(
        evidence["timing_trace"],
        query_ids=query_ids,
        environment_fingerprint=environment,
        schedule_seed=spec["gates"]["end_to_end_latency"]["schedule_seed"],
    )
    execution = _validate_content_receipt(evidence["execution_receipt"], "TwoArmExecutionReceipt")
    expected_seed = spec["gates"]["end_to_end_latency"]["schedule_seed"]
    _, expected_schedule = _expected_measurement_schedule(query_ids, expected_seed)
    expected_schedule_payload = [list(item) for item in expected_schedule]
    if (
        set(execution)
        != {
            "schema_version",
            "kind",
            "artifact_fingerprint",
            "arms",
            "schedule_seed",
            "schedule",
            "schedule_fingerprint",
            "environment_fingerprint",
            "warmup_count",
            "measurement_count",
            "configuration_fingerprint",
            "metric_contract_fingerprint",
            "spec_fingerprint",
            "manifest_fingerprint",
            "production_receipt_fingerprint",
            "rankings_fingerprint",
            "timing_trace_fingerprint",
        }
        or execution.get("spec_fingerprint") != spec["artifact_fingerprint"]
        or execution.get("manifest_fingerprint") != manifest["artifact_fingerprint"]
        or execution.get("production_receipt_fingerprint") != production["artifact_fingerprint"]
        or execution.get("rankings_fingerprint") != _hash(rankings)
        or execution.get("timing_trace_fingerprint") != _hash(evidence["timing_trace"])
        or execution.get("arms") != list(ARMS)
        or execution.get("schedule_seed") != expected_seed
        or execution.get("schedule") != expected_schedule_payload
        or execution.get("schedule_fingerprint") != _hash(expected_schedule_payload)
        or execution.get("schedule_fingerprint") != _hash(evidence["timing_trace"]["schedule"])
        or execution.get("environment_fingerprint") != environment
        or execution.get("warmup_count") != 2
        or execution.get("measurement_count") != len(expected_schedule)
        or execution.get("configuration_fingerprint") != _hash(spec["candidate"])
        or execution.get("metric_contract_fingerprint") != spec["metric_contract_fingerprint"]
    ):
        raise FreshDevelopmentError("execution receipt does not bind the timing schedule")
    failures = evidence["channel_failures"]
    if (
        not isinstance(failures, dict)
        or set(failures) != set(ARMS)
        or any(not isinstance(failures[arm], int) or failures[arm] < 0 for arm in ARMS)
    ):
        raise FreshDevelopmentError("channel failure receipt is incomplete")
    label_map = {query_id: {} for query_id in query_ids}
    for item in labels:
        label_map[item["query_id"]][item["candidate_id"]] = item["final_grade"]
    if any(set(label_map[query_id]) != set(expected_pools[query_id]) for query_id in query_ids):
        raise FreshDevelopmentError("every returned candidate must have a merged label")
    return label_map, evidence["timing_trace"], failures


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise FreshDevelopmentError("latency samples cannot be empty")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))]


def _validate_rankings(
    queries: Sequence[Mapping[str, Any]], rankings: Mapping[str, Mapping[str, Sequence[str]]]
) -> None:
    if set(rankings) != set(ARMS):
        raise FreshDevelopmentError("exactly the two preregistered arms must be evaluated")
    query_ids = {str(query["id"]) for query in queries}
    for arm in ARMS:
        if set(rankings[arm]) != query_ids:
            raise FreshDevelopmentError(f"{arm} rankings do not cover the manifest exactly")
        for query_id, ranking in rankings[arm].items():
            if (
                not isinstance(ranking, Sequence)
                or isinstance(ranking, (str, bytes))
                or len(ranking) > TOP_N
            ):
                raise FreshDevelopmentError(f"{arm} ranking for {query_id} is malformed")
            if len(ranking) != len(set(ranking)) or any(
                not isinstance(item, str) or not item for item in ranking
            ):
                raise FreshDevelopmentError(f"{arm} ranking for {query_id} has invalid IDs")


def _compute_decision(  # noqa: C901, PLR0912, PLR0915
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    rankings: Mapping[str, Mapping[str, Sequence[str]]],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one immutable two-arm result and return a content-addressed decision artifact."""
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    queries = frozen_manifest["queries"]
    _validate_rankings(queries, rankings)
    if not isinstance(evidence, Mapping):
        raise FreshDevelopmentError(
            "raw evidence bundle is required; direct label maps are forbidden"
        )
    evidence_input = evidence
    labels, timing_trace, failures = _validate_evidence(
        evidence, spec=frozen_spec, manifest=frozen_manifest, rankings=rankings
    )
    trace_rows = timing_trace["measurements"]
    latencies_by_arm = {
        arm: [sample for row in trace_rows if row["arm"] == arm for sample in row["samples"]]
        for arm in ARMS
    }
    query_map = {str(query["id"]): query for query in queries}
    family_a: dict[str, list[float]] = defaultdict(list)
    family_b: dict[str, list[float]] = defaultdict(list)
    metrics: dict[str, dict[str, float]] = {}
    category_sets = {"exact_title": [], "exact_sentence": [], "keyword": [], "strict_negative": []}
    source_hits: dict[str, dict[int, list[float]]] = {arm: {1: [], 10: [], 20: []} for arm in ARMS}
    recall_values: dict[str, list[float]] = {arm: [] for arm in ARMS}
    source_top1: dict[str, list[float]] = {arm: [] for arm in ARMS}
    for query_id, query in query_map.items():
        relevance = labels[query_id]
        if any(
            not isinstance(grade, int) or grade not in (0, 1, 2) for grade in relevance.values()
        ):
            raise FreshDevelopmentError(f"labels for {query_id} are not grades 0/1/2")
        category = query.get("category")
        for arm in ARMS:
            ranking = rankings[arm][query_id]
            # Accuracy metrics are defined over positive facets only.  A strict-negative
            # judgment can legitimately contain a grade-2 item (for example, when a judge
            # finds a returned result deceptively answers the negative query); that judgment
            # belongs exclusively to strict-negative safety and must never create an accuracy
            # observation or a topic-family contribution.
            if category in POSITIVE_FACETS:
                ndcg = ndcg_at_10(list(ranking), dict(relevance))
                if ndcg is not None:
                    (family_a if arm == BASELINE_ARM else family_b)[
                        query["topic_family_id"]
                    ].append(ndcg)
                recall = semantic_recall_at_20(list(ranking), dict(relevance))
                # A positive query with no grade-2 item in the judged candidate union is a
                # retrieval miss, not an undefined denominator for the aggregate: every
                # positive query contributes an explicit zero to recall.
                recall_values[arm].append(0.0 if recall is None else recall)
            top1_grade = relevance.get(ranking[0], 0) if ranking else 0
            if category in category_sets:
                category_sets[category].append(
                    (
                        arm,
                        float(top1_grade == 2)
                        if category != "strict_negative"
                        else float(top1_grade < 1),
                    )
                )
            source_ids = list(dict.fromkeys(query.get("source_entity_ids", [])))
            if source_ids:
                source_top1[arm].append(float(top1_grade == 2))
                for source_id in source_ids:
                    for cutoff in (1, 10, 20):
                        source_hits[arm][cutoff].append(float(source_id in ranking[:cutoff]))
    if any(
        len(recall_values[arm]) != sum(FACET_QUOTAS[facet] for facet in POSITIVE_FACETS)
        for arm in ARMS
    ):
        raise FreshDevelopmentError("grade-2 recall denominator is incomplete")
    expected_source_query_count = sum(FACET_QUOTAS[facet] for facet in POSITIVE_FACETS)
    expected_source_pair_count = sum(
        len(dict.fromkeys(query.get("source_entity_ids", [])))
        for query in queries
        if query.get("category") in POSITIVE_FACETS
    )
    if any(len(source_top1[arm]) != expected_source_query_count for arm in ARMS):
        raise FreshDevelopmentError("source top-1 denominator is incomplete")
    if any(
        len(source_hits[arm][cutoff]) != expected_source_pair_count
        for arm in ARMS
        for cutoff in (1, 10, 20)
    ):
        raise FreshDevelopmentError("source-ID hit denominator is incomplete")
    for category, expected_count in FACET_QUOTAS.items():
        if category in category_sets and any(
            sum(current_arm == arm for current_arm, _ in category_sets[category]) != expected_count
            for arm in ARMS
        ):
            raise FreshDevelopmentError(f"{category} safety denominator is incomplete")
    if (
        len(family_a) < frozen_spec["diversity"]["minimum_topic_families"]
        or len(family_b) < frozen_spec["diversity"]["minimum_topic_families"]
    ):
        raise FreshDevelopmentError("paired NDCG lacks the required family clusters")
    for arm in ARMS:
        family_values = family_a if arm == BASELINE_ARM else family_b
        metrics[arm] = {
            "macro_positive_ndcg_at_10": (
                statistics.fmean(statistics.fmean(values) for values in family_values.values())
                if family_values
                else 0.0
            ),
            "grade2_recall_at_20": statistics.fmean(recall_values[arm])
            if recall_values[arm]
            else 0.0,
            "same_specific_fact_grade2_top1": statistics.fmean(source_top1[arm])
            if source_top1[arm]
            else 0.0,
            "exact_safety": statistics.fmean(
                [
                    value
                    for category in ("exact_title", "exact_sentence")
                    for current_arm, value in category_sets[category]
                    if current_arm == arm
                ]
            ),
            "exact_title_safety": statistics.fmean(
                [value for current_arm, value in category_sets["exact_title"] if current_arm == arm]
            ),
            "exact_sentence_safety": statistics.fmean(
                [
                    value
                    for current_arm, value in category_sets["exact_sentence"]
                    if current_arm == arm
                ]
            ),
            "keyword_safety": statistics.fmean(
                [value for current_arm, value in category_sets["keyword"] if current_arm == arm]
            )
            if any(current_arm == arm for current_arm, _ in category_sets["keyword"])
            else 0.0,
            "strict_negative_safety": statistics.fmean(
                [
                    value
                    for current_arm, value in category_sets["strict_negative"]
                    if current_arm == arm
                ]
            )
            if any(current_arm == arm for current_arm, _ in category_sets["strict_negative"])
            else 0.0,
            "channel_failures": failures[arm],
            "latency_p50_ms": _percentile(latencies_by_arm[arm], 0.50),
            "latency_p95_ms": _percentile(latencies_by_arm[arm], 0.95),
        }
        for cutoff in (1, 10, 20):
            values = source_hits[arm][cutoff]
            metrics[arm][f"source_hit_at_{cutoff}"] = statistics.fmean(values) if values else 0.0
        metrics[arm]["source_hit_unit"] = "unique_query_source_pair"
    baseline = metrics[BASELINE_ARM]
    candidate = metrics[CANDIDATE_ARM]
    stat = frozen_spec["gates"]["paired_family_statistic"]
    bootstrap_a = {family: [statistics.fmean(values)] for family, values in family_a.items()}
    bootstrap_b = {family: [statistics.fmean(values)] for family, values in family_b.items()}
    delta, ci_low, ci_high = cluster_bootstrap_delta_ci(
        bootstrap_b, bootstrap_a, n_resamples=stat["resamples"], seed=stat["seed"]
    )
    gates = {
        "zero_channel_failures": all(metrics[arm]["channel_failures"] == 0 for arm in ARMS),
        "exact_safety_non_inferior": candidate["exact_safety"] >= baseline["exact_safety"],
        "keyword_safety_non_inferior": candidate["keyword_safety"] >= baseline["keyword_safety"],
        "strict_negative_safety_non_inferior": candidate["strict_negative_safety"]
        >= baseline["strict_negative_safety"],
        "grade2_recall_at_20_non_inferior": candidate["grade2_recall_at_20"]
        >= baseline["grade2_recall_at_20"],
        "same_specific_fact_top1_non_inferior": candidate["same_specific_fact_grade2_top1"]
        >= baseline["same_specific_fact_grade2_top1"],
        "source_hit_at_1_10_20_non_inferior": all(
            candidate[f"source_hit_at_{cutoff}"] >= baseline[f"source_hit_at_{cutoff}"]
            for cutoff in (1, 10, 20)
        ),
        "paired_family_minimum_effect": ci_low >= frozen_spec["gates"]["minimum_ndcg_effect"],
        "latency_p50": candidate["latency_p50_ms"]
        <= baseline["latency_p50_ms"]
        * (1 + frozen_spec["gates"]["end_to_end_latency"]["p50_relative_increase_at_most"]),
        "latency_p95": candidate["latency_p95_ms"]
        <= baseline["latency_p95_ms"]
        * (1 + frozen_spec["gates"]["end_to_end_latency"]["p95_relative_increase_at_most"]),
    }
    candidate_pass = all(gates.values())
    selected = CANDIDATE_ARM if candidate_pass else BASELINE_ARM
    decision_kind = "DevelopmentWinner" if candidate_pass else "BaselineDecision"
    payload = {
        "experiment_id": frozen_spec["experiment_id"],
        "spec_fingerprint": frozen_spec["artifact_fingerprint"],
        "metric_contract_fingerprint": frozen_spec["metric_contract_fingerprint"],
        "manifest_fingerprint": frozen_manifest["artifact_fingerprint"],
        "selected_arm": selected,
        "candidate_pass": candidate_pass,
        "arms_evaluated_once": list(ARMS),
        "metrics": metrics,
        "paired_ndcg_delta_ci95": {"delta": delta, "low": ci_low, "high": ci_high},
        "gates": gates,
        "failed_gates": sorted(name for name, passed in gates.items() if not passed),
        "evidence": evidence_input,
        "evidence_fingerprints": {
            "retrieval_evidence": evidence_input["retrieval_evidence"]["fingerprint"],
            "judging_matrix": evidence_input["judging_matrix"]["fingerprint"],
            "raw_judgment_artifacts": _hash(evidence_input["raw_judgment_artifacts"]),
            "merged_labels": _hash(evidence_input["merged_labels"]),
            "timing_trace": _hash(evidence_input["timing_trace"]),
            "production_receipt": evidence_input["production_receipt"]["artifact_fingerprint"],
            "execution_receipt": evidence_input["execution_receipt"]["artifact_fingerprint"],
            "metric_contract_fingerprint": frozen_spec["metric_contract_fingerprint"],
        },
    }
    artifact = sign_artifact(decision_kind, payload)
    return artifact


def evaluate_fresh_development(
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    rankings: Mapping[str, Mapping[str, Sequence[str]]],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute and immediately validate one immutable two-arm decision."""
    artifact = _compute_decision(spec, manifest, rankings, evidence)
    return validate_development_decision(artifact, spec, manifest)


def execute_two_arm_development(
    spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    runner: Callable[[str, Mapping[str, Any], str], tuple[Sequence[str], Sequence[float]]],
    evidence: Mapping[str, Any],
    *,
    schedule_seed: int = 11,
) -> dict[str, Any]:
    """Run a deterministic warmup/interleaved schedule, then evaluate bound raw evidence."""
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    if not isinstance(evidence, Mapping):
        raise FreshDevelopmentError("raw evidence template is required")
    if schedule_seed != frozen_spec["gates"]["end_to_end_latency"]["schedule_seed"]:
        raise FreshDevelopmentError("schedule seed must equal the preregistered value 11")
    query_rows = list(frozen_manifest["queries"])
    rng = random.Random(schedule_seed)
    warmups: list[dict[str, Any]] = []
    for arm in rng.sample(list(ARMS), len(ARMS)):
        result = runner(arm, query_rows[0], "warmup")
        if not isinstance(result, tuple) or len(result) != 2:
            raise FreshDevelopmentError("runner warmup must return (ranking, samples)")
        _, samples = result
        if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
            raise FreshDevelopmentError("runner warmup samples are malformed")
        warmups.append({"arm": arm, "samples": list(samples)})
    query_order = [
        query_id
        for query_id, _ in _expected_measurement_schedule(
            {query["id"] for query in query_rows}, schedule_seed
        )[1][::2]
    ]
    arm_order = [
        arm
        for _, arm in _expected_measurement_schedule(
            {query["id"] for query in query_rows}, schedule_seed
        )[1][:2]
    ]
    rankings: dict[str, dict[str, Sequence[str]]] = {arm: {} for arm in ARMS}
    measurements: list[dict[str, Any]] = []
    schedule: list[tuple[str, str]] = []
    query_by_id = {query["id"]: query for query in query_rows}
    for query_id in query_order:
        for arm in arm_order:
            result = runner(arm, query_by_id[query_id], "measure")
            if not isinstance(result, tuple) or len(result) != 2:
                raise FreshDevelopmentError("runner measurement must return (ranking, samples)")
            ranking, samples = result
            if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
                raise FreshDevelopmentError("runner measurement samples are malformed")
            rankings[arm][query_id] = ranking
            row = {"query_id": query_id, "arm": arm, "samples": list(samples)}
            measurements.append(row)
            schedule.append((query_id, arm))
    bound_evidence = dict(evidence)
    bound_evidence["timing_trace"] = {
        "warmups": warmups,
        "measurements": measurements,
        "schedule": schedule,
        "environment_fingerprint": evidence["production_receipt"].get("environment_fingerprint"),
        "schedule_seed": schedule_seed,
    }
    schedule_payload = [list(item) for item in schedule]
    bound_evidence["execution_receipt"] = sign_artifact(
        "TwoArmExecutionReceipt",
        {
            "arms": list(ARMS),
            "schedule_seed": schedule_seed,
            "schedule": schedule_payload,
            "schedule_fingerprint": _hash(schedule_payload),
            "environment_fingerprint": bound_evidence["timing_trace"]["environment_fingerprint"],
            "warmup_count": len(warmups),
            "measurement_count": len(measurements),
            "configuration_fingerprint": _hash(frozen_spec["candidate"]),
            "metric_contract_fingerprint": frozen_spec["metric_contract_fingerprint"],
            "spec_fingerprint": frozen_spec["artifact_fingerprint"],
            "manifest_fingerprint": frozen_manifest["artifact_fingerprint"],
            "production_receipt_fingerprint": evidence["production_receipt"][
                "artifact_fingerprint"
            ],
            "rankings_fingerprint": _hash(rankings),
            "timing_trace_fingerprint": _hash(bound_evidence["timing_trace"]),
        },
    )
    return evaluate_fresh_development(frozen_spec, frozen_manifest, rankings, bound_evidence)


def validate_development_decision(
    artifact: object, spec: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    try:
        value = validate_signed_artifact(artifact)
    except BakeoffContractError as exc:
        raise FreshDevelopmentError(str(exc)) from exc
    if value.get("kind") not in DECISION_KINDS:
        raise FreshDevelopmentError("decision must be DevelopmentWinner or BaselineDecision")
    required = {
        "experiment_id",
        "spec_fingerprint",
        "manifest_fingerprint",
        "selected_arm",
        "candidate_pass",
        "arms_evaluated_once",
        "metric_contract_fingerprint",
        "metrics",
        "paired_ndcg_delta_ci95",
        "gates",
        "failed_gates",
        "evidence",
        "evidence_fingerprints",
    }
    if (
        set(value) - required - {"schema_version", "kind", "artifact_fingerprint"}
        or set(value) - {"schema_version", "kind", "artifact_fingerprint"} != required
    ):
        raise FreshDevelopmentError("decision fields are incomplete")
    if (
        value["experiment_id"] != frozen_spec["experiment_id"]
        or value["spec_fingerprint"] != frozen_spec["artifact_fingerprint"]
        or value["metric_contract_fingerprint"] != frozen_spec["metric_contract_fingerprint"]
        or value["manifest_fingerprint"] != frozen_manifest["artifact_fingerprint"]
    ):
        raise FreshDevelopmentError("decision is bound to a different spec or manifest")
    if value["arms_evaluated_once"] != list(ARMS):
        raise FreshDevelopmentError("decision does not attest exactly-once fixed-arm execution")
    if value["candidate_pass"] != (value["selected_arm"] == CANDIDATE_ARM) or value["kind"] != (
        "DevelopmentWinner" if value["candidate_pass"] else "BaselineDecision"
    ):
        raise FreshDevelopmentError("decision kind/selected arm/pass flag disagree")
    evidence = value["evidence"]
    if not isinstance(evidence, Mapping):
        raise FreshDevelopmentError("decision lacks bound evidence")
    if not isinstance(evidence.get("retrieval_evidence"), Mapping):
        raise FreshDevelopmentError("decision evidence lacks raw retrieval receipt")
    expected_rankings = _derive_rankings_from_retrieval(
        evidence["retrieval_evidence"], spec=frozen_spec, manifest=frozen_manifest
    )
    expected = _compute_decision(frozen_spec, frozen_manifest, expected_rankings, evidence)
    for field in (
        "selected_arm",
        "candidate_pass",
        "arms_evaluated_once",
        "metric_contract_fingerprint",
        "metrics",
        "paired_ndcg_delta_ci95",
        "gates",
        "failed_gates",
        "evidence_fingerprints",
    ):
        if value[field] != expected[field]:
            raise FreshDevelopmentError(f"decision field {field} was not recomputed from evidence")
    return value


def assert_later_transition_readiness(
    spec: Mapping[str, Any], decision: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, str]:
    """Return scientific-readiness metadata; never authorize or unlock a protected transition.

    Human confirmation, signer authentication, nonce/expiry, and one-time custody consumption
    remain external to this module.  A local content hash is explicitly not an authentication
    token.
    """
    frozen_spec = validate_fresh_development_spec(spec)
    frozen_manifest = validate_fresh_query_manifest(manifest, frozen_spec)
    validated = validate_development_decision(decision, frozen_spec, frozen_manifest)
    if validated["kind"] != "DevelopmentWinner":
        raise FreshDevelopmentError("baseline decision is not transition-ready")
    return {
        "experiment_id": frozen_spec["experiment_id"],
        "decision_fingerprint": validated["artifact_fingerprint"],
        "readiness": "external_custody_authorization_required",
    }
