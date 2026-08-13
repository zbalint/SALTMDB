"""Promotion-valid state and custody contracts for the search-accuracy bakeoff.

This module is deliberately dependency-free and does not run retrieval or generation.  It is
the fail-closed boundary around those stages: every transition consumes signed, content-addressed
artifacts, and blind slot material can only be opened with an experiment-specific unlock derived
from the frozen development winner.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-f]{7,64}$")
SCHEMA_VERSION = 1
MAIN_CUSTODIAN = "codex"

MANDATORY_FACETS = frozenset(
    {
        "exact_sentence",
        "keyword",
        "typo",
        "short_memory",
        "long_body",
        "current_vs_superseded",
        "close_sibling",
        "multilingual",
        "strict_negative",
    }
)
REQUIRED_METRICS = (
    "macro_positive_ndcg_at_10",
    "grade2_recall_at_20",
    "same_specific_fact_grade2_top1",
    "exact_safety",
    "keyword_safety",
    "strict_negative_safety",
    "warm_latency_p50_seconds",
    "warm_latency_p95_seconds",
    "benchmark_failures",
)


class BakeoffContractError(ValueError):
    """An artifact or transition violates the frozen experiment contract."""


class BlindAccessError(PermissionError):
    """Blind material was requested without a matching signed unlock."""


class RunState(str, Enum):
    SPEC_FROZEN = "SPEC_FROZEN"
    DEV_INDEXED = "DEV_INDEXED"
    DEV_RETRIEVED = "DEV_RETRIEVED"
    DEV_JUDGED = "DEV_JUDGED"
    DEV_WINNER_SIGNED = "DEV_WINNER_SIGNED"
    BLIND_UNLOCKED = "BLIND_UNLOCKED"
    BLIND_COMPLETE = "BLIND_COMPLETE"
    PROMOTED = "PROMOTED"
    RETAINED = "RETAINED"


ALLOWED_TRANSITIONS = {
    RunState.SPEC_FROZEN: frozenset({RunState.DEV_INDEXED}),
    RunState.DEV_INDEXED: frozenset({RunState.DEV_RETRIEVED}),
    RunState.DEV_RETRIEVED: frozenset({RunState.DEV_JUDGED}),
    RunState.DEV_JUDGED: frozenset({RunState.DEV_WINNER_SIGNED}),
    RunState.DEV_WINNER_SIGNED: frozenset({RunState.BLIND_UNLOCKED}),
    RunState.BLIND_UNLOCKED: frozenset({RunState.BLIND_COMPLETE}),
    RunState.BLIND_COMPLETE: frozenset({RunState.PROMOTED, RunState.RETAINED}),
    RunState.PROMOTED: frozenset(),
    RunState.RETAINED: frozenset(),
}
TRANSITION_EVIDENCE_KINDS = {
    RunState.DEV_INDEXED: "IndexBuildReceipt",
    RunState.DEV_RETRIEVED: "RetrievalRunBundle",
    RunState.DEV_JUDGED: "DevelopmentJudgments",
    RunState.DEV_WINNER_SIGNED: "DevelopmentWinner",
    RunState.BLIND_UNLOCKED: "BlindUnlock",
    RunState.BLIND_COMPLETE: "BlindEvaluation",
    RunState.PROMOTED: "PromotionDecision",
    RunState.RETAINED: "PromotionDecision",
}


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BakeoffContractError(f"{field} must be a non-empty string")
    return value


def _require_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise BakeoffContractError(f"{field} must be a lowercase SHA-256")
    return value


def _require_exact_keys(value: Mapping[str, Any], keys: Sequence[str], label: str) -> None:
    missing = sorted(set(keys) - set(value))
    unknown = sorted(set(value) - set(keys) - {"schema_version", "kind", "artifact_fingerprint"})
    if missing or unknown:
        detail = []
        if missing:
            detail.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown fields: {', '.join(unknown)}")
        raise BakeoffContractError(f"{label} {'; '.join(detail)}")


def sign_artifact(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached, content-addressed artifact with an explicit kind."""
    _require_text(kind, "artifact kind")
    result = {"schema_version": SCHEMA_VERSION, "kind": kind, **dict(payload)}
    result.pop("artifact_fingerprint", None)
    result["artifact_fingerprint"] = fingerprint(result)
    return result


def validate_signed_artifact(artifact: object, *, kind: str | None = None) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise BakeoffContractError("artifact must be an object")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise BakeoffContractError("unsupported artifact schema")
    if kind is not None and artifact.get("kind") != kind:
        raise BakeoffContractError(f"expected {kind} artifact")
    stored = artifact.get("artifact_fingerprint")
    unsigned = dict(artifact)
    unsigned.pop("artifact_fingerprint", None)
    if not isinstance(stored, str) or stored != fingerprint(unsigned):
        raise BakeoffContractError("artifact fingerprint mismatch")
    return artifact


def _validate_spec_collections(value: Mapping[str, Any]) -> None:
    contenders = value["contenders"]
    if (
        not isinstance(contenders, list)
        or len(contenders) < 2
        or len(contenders) != len(set(contenders))
        or not all(isinstance(item, str) and item for item in contenders)
    ):
        raise BakeoffContractError("contenders must be unique non-empty identifiers")
    seeds = value["seeds"]
    if (
        not isinstance(seeds, dict)
        or not seeds
        or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds.values())
    ):
        raise BakeoffContractError("seeds must be a non-empty integer mapping")
    grids = value["hyperparameter_grids"]
    if (
        not isinstance(grids, dict)
        or not grids
        or any(not isinstance(grid, list) or not grid for grid in grids.values())
    ):
        raise BakeoffContractError("hyperparameter grids must be finite predeclared lists")
    versions = value["software_versions"]
    if (
        not isinstance(versions, dict)
        or not versions
        or any(
            not isinstance(name, str) or not name or not isinstance(version, str) or not version
            for name, version in versions.items()
        )
    ):
        raise BakeoffContractError("software versions must be a non-empty string mapping")


def validate_bakeoff_spec(artifact: object) -> dict[str, Any]:
    value = validate_signed_artifact(artifact, kind="BakeoffSpec")
    required = (
        "run_id",
        "commit",
        "corpus_snapshot_hash",
        "query_slots_hash",
        "query_prompt_hash",
        "rubric_hash",
        "configuration_hash",
        "seeds",
        "machine_fingerprint",
        "contenders",
        "hyperparameter_grids",
        "required_metrics",
        "software_versions",
        "holm_comparison_family",
        "split_targets",
        "facet_targets",
    )
    _require_exact_keys(value, required, "BakeoffSpec")
    _require_text(value["run_id"], "run_id")
    for field in (
        "commit",
        "corpus_snapshot_hash",
        "query_slots_hash",
        "query_prompt_hash",
        "rubric_hash",
        "configuration_hash",
        "machine_fingerprint",
    ):
        _require_hash(value[field], field)
    if tuple(value["required_metrics"]) != REQUIRED_METRICS:
        raise BakeoffContractError("required metric vector differs from the frozen contract")
    if value["split_targets"] != {"dev": 400, "blind": 800}:
        raise BakeoffContractError("split targets must be exactly dev=400 and blind=800")
    facets = value["facet_targets"]
    if not isinstance(facets, dict) or set(facets) != {"dev", "blind"}:
        raise BakeoffContractError("facet targets must define dev and blind")
    for split, counts in facets.items():
        if not isinstance(counts, dict) or not MANDATORY_FACETS.issubset(counts):
            raise BakeoffContractError(f"{split} lacks mandatory facets")
        if any(not isinstance(counts[name], int) or counts[name] <= 0 for name in MANDATORY_FACETS):
            raise BakeoffContractError(f"{split} mandatory facet denominators must be positive")
        if set(counts) != MANDATORY_FACETS or sum(counts.values()) != value["split_targets"][split]:
            raise BakeoffContractError(f"{split} facets must be non-overlapping and exhaustive")
    _validate_spec_collections(value)
    if not isinstance(value["holm_comparison_family"], list) or not value["holm_comparison_family"]:
        raise BakeoffContractError("Holm comparison family must be predeclared and non-empty")
    return value


def validate_model_lock(artifact: object) -> dict[str, Any]:
    value = validate_signed_artifact(artifact, kind="ModelLock")
    _require_exact_keys(
        value,
        (
            "source_repository",
            "resolved_revision",
            "files",
            "dimension",
            "normalization",
            "maximum_input_tokens",
            "query_prefix",
            "document_prefix",
        ),
        "ModelLock",
    )
    _require_text(value["source_repository"], "source_repository")
    revision = _require_text(value["resolved_revision"], "resolved_revision")
    if not IMMUTABLE_REVISION_RE.fullmatch(revision):
        raise BakeoffContractError("resolved_revision must be an immutable commit hash")
    files = value["files"]
    if not isinstance(files, list) or not files:
        raise BakeoffContractError("model file inventory must be non-empty")
    paths: set[str] = set()
    for row in files:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}:
            raise BakeoffContractError("model file inventory row is malformed")
        path = _require_text(row["path"], "model file path")
        if path.startswith("/") or ".." in Path(path).parts or path in paths:
            raise BakeoffContractError("model file paths must be unique and relative")
        paths.add(path)
        _require_hash(row["sha256"], "model file sha256")
        if not isinstance(row["size_bytes"], int) or row["size_bytes"] < 0:
            raise BakeoffContractError("model file size must be non-negative")
    if not isinstance(value["dimension"], int) or value["dimension"] <= 0:
        raise BakeoffContractError("model dimension must be positive")
    if value["normalization"] not in {"none", "l2"}:
        raise BakeoffContractError("unsupported normalization")
    if not isinstance(value["maximum_input_tokens"], int) or value["maximum_input_tokens"] <= 0:
        raise BakeoffContractError("maximum_input_tokens must be positive")
    return value


def validate_corpus_manifest(artifact: object) -> dict[str, Any]:
    value = validate_signed_artifact(artifact, kind="CorpusRepresentationManifest")
    _require_exact_keys(
        value,
        ("eligible_ids", "entities", "representation_version", "corpus_root_hash"),
        "CorpusRepresentationManifest",
    )
    ids = value["eligible_ids"]
    entities = value["entities"]
    if not isinstance(ids, list) or not ids or ids != sorted(ids) or len(ids) != len(set(ids)):
        raise BakeoffContractError("eligible IDs must be non-empty, unique, and ordered")
    if not isinstance(entities, list) or [row.get("entity_id") for row in entities] != ids:
        raise BakeoffContractError("entity rows must exactly follow eligible ID order")
    for row in entities:
        if not isinstance(row, dict):
            raise BakeoffContractError("corpus entity row must be an object")
        _require_exact_keys(
            row,
            ("entity_id", "title_hash", "body_hash", "source_hash", "chunk_hashes"),
            "corpus entity",
        )
        for field in ("title_hash", "body_hash", "source_hash"):
            _require_hash(row[field], field)
        chunks = row["chunk_hashes"]
        if not isinstance(chunks, list) or not chunks:
            raise BakeoffContractError("every entity requires at least one deterministic chunk")
        for chunk_hash in chunks:
            _require_hash(chunk_hash, "chunk hash")
    _require_text(value["representation_version"], "representation_version")
    expected_root = fingerprint(
        {
            "eligible_ids": ids,
            "entities": entities,
            "representation_version": value["representation_version"],
        }
    )
    if value["corpus_root_hash"] != expected_root:
        raise BakeoffContractError("corpus root hash mismatch")
    return value


def _assignment_source_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise BakeoffContractError("source_entity_ids must contain non-empty strings")
    return value


def validate_query_slot_assignments(artifact: object) -> dict[str, Any]:
    """Validate sealed dev/blind slot metadata without materializing blind query text.

    Every query has exactly one registered facet.  Topic families and source entities are
    split-disjoint, which prevents a near-duplicate memory family from leaking into both stages.
    """
    value = validate_signed_artifact(artifact, kind="QuerySlotAssignments")
    _require_exact_keys(value, ("assignments", "generation_prompt_hash"), "QuerySlotAssignments")
    _require_hash(value["generation_prompt_hash"], "generation_prompt_hash")
    rows = value["assignments"]
    if not isinstance(rows, list) or len(rows) != 1200:
        raise BakeoffContractError("slot assignments must contain exactly 1200 rows")
    ids: set[str] = set()
    slots: set[str] = set()
    family_split: dict[str, str] = {}
    source_split: dict[str, str] = {}
    counts = {"dev": 0, "blind": 0}
    facet_counts = {split: {facet: 0 for facet in MANDATORY_FACETS} for split in counts}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "query_id",
            "slot_id",
            "split",
            "topic_family_id",
            "source_entity_ids",
            "facet",
        }:
            raise BakeoffContractError("slot assignment row is malformed")
        query_id = _require_text(row["query_id"], "query_id")
        slot_id = _require_text(row["slot_id"], "slot_id")
        family = _require_text(row["topic_family_id"], "topic_family_id")
        split = row["split"]
        facet = row["facet"]
        if query_id in ids or slot_id in slots:
            raise BakeoffContractError("query and slot IDs must be unique")
        if split not in counts or facet not in MANDATORY_FACETS:
            raise BakeoffContractError("slot split or facet is not registered")
        previous_family_split = family_split.setdefault(family, split)
        if previous_family_split != split:
            raise BakeoffContractError("topic family occurs in both dev and blind")
        for source_id in _assignment_source_ids(row["source_entity_ids"]):
            previous_source_split = source_split.setdefault(source_id, split)
            if previous_source_split != split:
                raise BakeoffContractError("source entity occurs in both dev and blind")
        ids.add(query_id)
        slots.add(slot_id)
        counts[split] += 1
        facet_counts[split][facet] += 1
    if counts != {"dev": 400, "blind": 800}:
        raise BakeoffContractError("slot assignments must preserve the 400/800 split")
    for split in counts:
        if any(facet_counts[split][facet] == 0 for facet in MANDATORY_FACETS):
            raise BakeoffContractError(f"{split} has an empty mandatory facet")
    return value


def validate_metric_vector(metrics: object) -> dict[str, Any]:
    if not isinstance(metrics, dict) or set(metrics) != set(REQUIRED_METRICS):
        raise BakeoffContractError("metrics must contain the complete frozen metric vector")
    for name in REQUIRED_METRICS[:-1]:
        value = metrics[name]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise BakeoffContractError(f"metric {name} must be finite numeric")
    for name in REQUIRED_METRICS[:6]:
        if not 0.0 <= float(metrics[name]) <= 1.0:
            raise BakeoffContractError(f"metric {name} must be within [0, 1]")
    for name in ("warm_latency_p50_seconds", "warm_latency_p95_seconds"):
        if float(metrics[name]) < 0:
            raise BakeoffContractError(f"metric {name} cannot be negative")
    if metrics["warm_latency_p50_seconds"] > metrics["warm_latency_p95_seconds"]:
        raise BakeoffContractError("warm latency p50 cannot exceed p95")
    if not isinstance(metrics["benchmark_failures"], int) or metrics["benchmark_failures"] < 0:
        raise BakeoffContractError("benchmark_failures must be a non-negative integer")
    return metrics


def validate_development_winner(artifact: object, spec: Mapping[str, Any]) -> dict[str, Any]:
    value = validate_signed_artifact(artifact, kind="DevelopmentWinner")
    validate_bakeoff_spec(spec)
    _require_exact_keys(
        value,
        (
            "run_id",
            "spec_fingerprint",
            "pipeline",
            "development_metrics",
            "upstream_fingerprints",
            "development_query_ids",
        ),
        "DevelopmentWinner",
    )
    if (
        value["run_id"] != spec["run_id"]
        or value["spec_fingerprint"] != spec["artifact_fingerprint"]
    ):
        raise BakeoffContractError("development winner is bound to a different experiment")
    validate_metric_vector(value["development_metrics"])
    if not isinstance(value["pipeline"], dict) or not value["pipeline"]:
        raise BakeoffContractError("winner pipeline must be complete")
    contender_id = value["pipeline"].get("contender_id")
    if contender_id not in spec["contenders"]:
        raise BakeoffContractError("winner pipeline is not a predeclared contender")
    if value["development_metrics"]["benchmark_failures"] != 0:
        raise BakeoffContractError("a development winner cannot contain benchmark failures")
    if value["development_metrics"]["warm_latency_p95_seconds"] > 5.0:
        raise BakeoffContractError("development winner exceeds the latency ceiling")
    upstream = value["upstream_fingerprints"]
    if not isinstance(upstream, dict) or not upstream:
        raise BakeoffContractError("winner requires upstream artifact fingerprints")
    for name, digest in upstream.items():
        _require_text(name, "upstream artifact name")
        _require_hash(digest, f"upstream fingerprint {name}")
    query_ids = value["development_query_ids"]
    if not isinstance(query_ids, list) or len(query_ids) != 400 or len(set(query_ids)) != 400:
        raise BakeoffContractError("winner requires exactly 400 unique development query IDs")
    return value


def build_blind_unlock(
    spec: Mapping[str, Any],
    winner: Mapping[str, Any],
    *,
    user_confirmation: str | None = None,
    custodian: str = MAIN_CUSTODIAN,
) -> dict[str, Any]:
    validate_bakeoff_spec(spec)
    validate_development_winner(winner, spec)
    if custodian != MAIN_CUSTODIAN:
        raise BlindAccessError("only the main custodian may issue a blind unlock")
    if not isinstance(user_confirmation, str) or not user_confirmation.strip():
        raise BlindAccessError(
            "an explicit user confirmation is required immediately before blind unlock"
        )
    return sign_artifact(
        "BlindUnlock",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "development_winner_fingerprint": winner["artifact_fingerprint"],
            "query_prompt_hash": spec["query_prompt_hash"],
            "blind_slots_hash": spec["query_slots_hash"],
            "custodian": custodian,
            "user_confirmation": user_confirmation.strip(),
        },
    )


def validate_blind_unlock(
    artifact: object, spec: Mapping[str, Any], winner: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        value = validate_signed_artifact(artifact, kind="BlindUnlock")
        validate_development_winner(winner, spec)
    except BakeoffContractError as exc:
        raise BlindAccessError("blind unlock is missing or malformed") from exc
    expected = {
        "run_id": spec["run_id"],
        "spec_fingerprint": spec["artifact_fingerprint"],
        "development_winner_fingerprint": winner["artifact_fingerprint"],
        "query_prompt_hash": spec["query_prompt_hash"],
        "blind_slots_hash": spec["query_slots_hash"],
        "custodian": MAIN_CUSTODIAN,
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise BlindAccessError("blind unlock does not match the frozen experiment winner")
    if (
        not isinstance(value.get("user_confirmation"), str)
        or not value["user_confirmation"].strip()
    ):
        raise BlindAccessError("blind unlock lacks the required explicit user confirmation")
    return value


def build_blind_manifest_receipt(
    spec: Mapping[str, Any],
    winner: Mapping[str, Any],
    unlock: Mapping[str, Any],
    file_hash: str,
    *,
    custodian: str = MAIN_CUSTODIAN,
) -> dict[str, Any]:
    """Bind post-unlock blind query bytes to the frozen winner and generation prompt."""
    validate_blind_unlock(unlock, spec, winner)
    if custodian != MAIN_CUSTODIAN:
        raise BlindAccessError("only the main custodian may sign a blind manifest receipt")
    _require_hash(file_hash, "blind manifest file hash")
    return sign_artifact(
        "BlindQueryManifestReceipt",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "development_winner_fingerprint": winner["artifact_fingerprint"],
            "blind_unlock_fingerprint": unlock["artifact_fingerprint"],
            "query_prompt_hash": spec["query_prompt_hash"],
            "file_sha256": file_hash,
            "custodian": custodian,
        },
    )


def validate_blind_manifest_receipt(
    receipt: object,
    spec: Mapping[str, Any],
    winner: Mapping[str, Any],
    unlock: Mapping[str, Any],
) -> dict[str, Any]:
    value = validate_signed_artifact(receipt, kind="BlindQueryManifestReceipt")
    expected = build_blind_manifest_receipt(
        spec, winner, unlock, value.get("file_sha256", ""), custodian=MAIN_CUSTODIAN
    )
    if value != expected:
        raise BlindAccessError("blind manifest receipt does not match the frozen experiment")
    return value


def validate_promotion_decision(  # noqa: C901, PLR0912
    artifact: object, spec: Mapping[str, Any]
) -> dict[str, Any]:
    value = validate_signed_artifact(artifact, kind="PromotionDecision")
    _require_exact_keys(
        value,
        (
            "run_id",
            "spec_fingerprint",
            "blind_metrics",
            "accuracy_deltas",
            "confidence_intervals",
            "holm_results",
            "safety_deltas",
            "failures",
            "latency",
            "promotion",
            "evidence_fingerprints",
        ),
        "PromotionDecision",
    )
    if (
        value["run_id"] != spec["run_id"]
        or value["spec_fingerprint"] != spec["artifact_fingerprint"]
    ):
        raise BakeoffContractError("promotion decision is bound to a different experiment")
    validate_metric_vector(value["blind_metrics"])
    evidence = value["evidence_fingerprints"]
    required_evidence = {
        "development_winner",
        "blind_evaluation",
        "blind_unlock",
        "blind_manifest_receipt",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_evidence:
        raise BakeoffContractError("promotion decision lacks its complete custody evidence binding")
    for name, digest in evidence.items():
        _require_hash(digest, f"promotion evidence {name}")
    if not isinstance(value["promotion"], bool):
        raise BakeoffContractError("promotion must be boolean")
    deltas = value["accuracy_deltas"]
    if not isinstance(deltas, dict) or set(deltas) != {
        "ndcg_at_10",
        "grade2_recall_at_20",
        "same_specific_fact_grade2_top1",
    }:
        raise BakeoffContractError("promotion decision lacks the frozen accuracy deltas")
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in deltas.values()
    ):
        raise BakeoffContractError("accuracy deltas must be finite")
    if not -1.0 <= float(deltas["ndcg_at_10"]) <= 1.0:
        raise BakeoffContractError("NDCG delta must be within [-1, 1]")
    intervals = value["confidence_intervals"]
    ndcg_ci = intervals.get("ndcg_delta_ci95") if isinstance(intervals, dict) else None
    if (
        not isinstance(ndcg_ci, list)
        or len(ndcg_ci) != 2
        or any(
            not isinstance(item, (int, float)) or not math.isfinite(float(item)) for item in ndcg_ci
        )
        or ndcg_ci[0] > ndcg_ci[1]
    ):
        raise BakeoffContractError("NDCG confidence interval is malformed")
    holm = value["holm_results"]
    comparison_name = "winner_vs_baseline_same_specific_fact"
    comparison = (
        next(
            (
                item
                for item in holm
                if isinstance(item, dict) and item.get("comparison") == comparison_name
            ),
            None,
        )
        if isinstance(holm, list)
        else None
    )
    if comparison is None or not isinstance(comparison.get("adjusted_p"), (int, float)):
        raise BakeoffContractError("Holm results lack the same-specific-fact comparison")
    adjusted_p = float(comparison["adjusted_p"])
    if not math.isfinite(adjusted_p) or not 0.0 <= adjusted_p <= 1.0:
        raise BakeoffContractError("Holm adjusted p-value must be finite and within [0, 1]")
    declared_comparisons = set(spec["holm_comparison_family"])
    observed_comparisons = {item.get("comparison") for item in holm if isinstance(item, dict)}
    if observed_comparisons != declared_comparisons:
        raise BakeoffContractError("Holm results do not cover the predeclared comparison family")
    safety = value["safety_deltas"]
    if not isinstance(safety, dict) or set(safety) != {"exact", "keyword", "strict_negative"}:
        raise BakeoffContractError("safety deltas are incomplete")
    if any(
        not isinstance(item, (int, float)) or not math.isfinite(float(item))
        for item in safety.values()
    ):
        raise BakeoffContractError("safety deltas must be finite")
    failures = value["failures"]
    latency = value["latency"]
    if not isinstance(failures, list) or not isinstance(latency, dict):
        raise BakeoffContractError("failure or latency evidence is malformed")
    p95 = latency.get("candidate_warm_p95_seconds")
    if not isinstance(p95, (int, float)) or not math.isfinite(float(p95)) or p95 < 0:
        raise BakeoffContractError("candidate warm p95 is malformed")
    gates_pass = (
        float(ndcg_ci[0]) > 0.0
        and float(deltas["grade2_recall_at_20"]) >= 0.03
        and float(deltas["same_specific_fact_grade2_top1"]) > 0.0
        and adjusted_p < 0.05
        and all(float(item) <= 0.01 for item in safety.values())
        and not failures
        and value["blind_metrics"]["benchmark_failures"] == 0
        and float(p95) <= 5.0
    )
    if value["promotion"] is not gates_pass:
        raise BakeoffContractError("promotion boolean contradicts the locked gates")
    return value


def build_promotion_decision(
    spec: Mapping[str, Any],
    blind_evaluation: Mapping[str, Any],
    winner: Mapping[str, Any],
    unlock: Mapping[str, Any],
    manifest_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the only terminal decision from fully signed blind evidence."""
    validate_blind_evaluation(blind_evaluation, spec)
    validate_development_winner(winner, spec)
    validate_blind_unlock(unlock, spec, winner)
    validate_blind_manifest_receipt(manifest_receipt, spec, winner, unlock)
    decision = sign_artifact(
        "PromotionDecision",
        {
            "run_id": spec["run_id"],
            "spec_fingerprint": spec["artifact_fingerprint"],
            "blind_metrics": blind_evaluation["candidate_metrics"],
            "accuracy_deltas": blind_evaluation["accuracy_deltas"],
            "confidence_intervals": blind_evaluation["confidence_intervals"],
            "holm_results": blind_evaluation["holm_results"],
            "safety_deltas": blind_evaluation["safety_deltas"],
            "failures": (
                []
                if blind_evaluation["candidate_metrics"]["benchmark_failures"] == 0
                else ["candidate_benchmark_failures"]
            ),
            "latency": {
                "candidate_warm_p95_seconds": blind_evaluation["candidate_metrics"][
                    "warm_latency_p95_seconds"
                ]
            },
            "promotion": False,
            "evidence_fingerprints": {
                "development_winner": winner["artifact_fingerprint"],
                "blind_evaluation": blind_evaluation["artifact_fingerprint"],
                "blind_unlock": unlock["artifact_fingerprint"],
                "blind_manifest_receipt": manifest_receipt["artifact_fingerprint"],
            },
        },
    )
    # Re-sign with the deterministically derived truth value.  Validation owns every locked gate.
    ndcg_low = decision["confidence_intervals"]["ndcg_delta_ci95"][0]
    same = next(
        item
        for item in decision["holm_results"]
        if item["comparison"] == "winner_vs_baseline_same_specific_fact"
    )["adjusted_p"]
    promotion = (
        ndcg_low > 0
        and decision["accuracy_deltas"]["grade2_recall_at_20"] >= 0.03
        and decision["accuracy_deltas"]["same_specific_fact_grade2_top1"] > 0
        and same < 0.05
        and all(x <= 0.01 for x in decision["safety_deltas"].values())
        and not decision["failures"]
        and decision["latency"]["candidate_warm_p95_seconds"] <= 5
    )
    decision = sign_artifact("PromotionDecision", {**decision, "promotion": promotion})
    validate_promotion_decision(decision, spec)
    return decision


def validate_blind_evaluation(  # noqa: C901, PLR0912
    artifact: object, spec: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the complete, privacy-preserving Gate-D blind evaluation receipt."""
    value = validate_signed_artifact(artifact, kind="BlindEvaluation")
    _require_exact_keys(
        value,
        (
            "run_id",
            "spec_fingerprint",
            "candidate_contender_id",
            "baseline_contender_id",
            "complete_query_count",
            "blind_query_ids",
            "candidate_metrics",
            "baseline_metrics",
            "accuracy_deltas",
            "safety_deltas",
            "confidence_intervals",
            "holm_results",
            "configuration_mutated_after_first_result",
            "fixed_configuration_attestation",
            "unresolved_disagreements",
            "evidence_fingerprints",
        ),
        "BlindEvaluation",
    )
    if (
        value["run_id"] != spec["run_id"]
        or value["spec_fingerprint"] != spec["artifact_fingerprint"]
    ):
        raise BakeoffContractError("blind evaluation is bound to a different experiment")
    if (
        value["candidate_contender_id"] not in spec["contenders"]
        or value["baseline_contender_id"] != "lexical:bm25"
    ):
        raise BakeoffContractError(
            "blind evaluation must compare the selected contender with lexical:bm25"
        )
    ids = value["blind_query_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) != 800
        or len(set(ids)) != 800
        or not all(isinstance(x, str) and x for x in ids)
    ):
        raise BakeoffContractError("blind evaluation requires exactly 800 unique blind query IDs")
    if (
        value["complete_query_count"] != 800
        or value["configuration_mutated_after_first_result"] is not False
    ):
        raise BakeoffContractError("blind evaluation is incomplete or mutated")
    if value["unresolved_disagreements"] != 0:
        raise BakeoffContractError("blind evaluation has unresolved adjudications")
    attestation = value["fixed_configuration_attestation"]
    if not isinstance(attestation, dict) or attestation.get("frozen") is not True:
        raise BakeoffContractError("blind evaluation lacks a fixed-configuration attestation")
    validate_metric_vector(value["candidate_metrics"])
    validate_metric_vector(value["baseline_metrics"])
    expected_accuracy = {"ndcg_at_10", "grade2_recall_at_20", "same_specific_fact_grade2_top1"}
    if (
        not isinstance(value["accuracy_deltas"], dict)
        or set(value["accuracy_deltas"]) != expected_accuracy
    ):
        raise BakeoffContractError("blind evaluation lacks complete paired accuracy deltas")
    if not isinstance(value["safety_deltas"], dict) or set(value["safety_deltas"]) != {
        "exact",
        "keyword",
        "strict_negative",
    }:
        raise BakeoffContractError("blind evaluation lacks complete safety regressions")
    for group in (value["accuracy_deltas"], value["safety_deltas"]):
        if any(
            not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(float(x))
            for x in group.values()
        ):
            raise BakeoffContractError("blind paired outcomes must be finite")
    intervals = value["confidence_intervals"]
    ci = intervals.get("ndcg_delta_ci95") if isinstance(intervals, dict) else None
    if (
        not isinstance(ci, list)
        or len(ci) != 2
        or any(not isinstance(x, (int, float)) or not math.isfinite(float(x)) for x in ci)
        or ci[0] > ci[1]
    ):
        raise BakeoffContractError("blind NDCG confidence interval is malformed")
    holm = value["holm_results"]
    if not isinstance(holm, list) or {
        item.get("comparison") for item in holm if isinstance(item, dict)
    } != set(spec["holm_comparison_family"]):
        raise BakeoffContractError(
            "blind Holm results do not cover the predeclared comparison family"
        )
    for item in holm:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("adjusted_p"), (int, float))
            or not 0 <= float(item["adjusted_p"]) <= 1
        ):
            raise BakeoffContractError("blind Holm result is malformed")
    required_fingerprints = {
        "development_winner",
        "blind_unlock",
        "blind_manifest_receipt",
        "judging_matrix",
        "winner_bundle",
        "baseline_bundle",
        "raw_judgments",
        "merged_labels",
        "metrics_artifact",
        "blind_query_ids",
    }
    evidence = value["evidence_fingerprints"]
    if not isinstance(evidence, dict) or set(evidence) != required_fingerprints:
        raise BakeoffContractError("blind evaluation lacks complete evidence fingerprints")
    for name, digest in evidence.items():
        _require_hash(digest, f"blind evidence {name}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class BakeoffStateMachine:
    """Atomic, resumable checkpoint ledger for one immutable run."""

    run_dir: Path

    @property
    def checkpoint_path(self) -> Path:
        return self.run_dir / "state_checkpoint.json"

    @property
    def spec_path(self) -> Path:
        return self.run_dir / "bakeoff_spec.json"

    @property
    def history_dir(self) -> Path:
        return self.run_dir / "checkpoint_history"

    @property
    def evidence_dir(self) -> Path:
        return self.run_dir / "evidence"

    def _write_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        sequence = len(list(self.history_dir.glob("*.json")))
        history_path = self.history_dir / f"{sequence:03d}-{checkpoint['state']}.json"
        if history_path.exists():
            raise BakeoffContractError("checkpoint history collision")
        _atomic_write_json(history_path, checkpoint)
        _atomic_write_json(self.checkpoint_path, checkpoint)

    def initialize(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        validate_bakeoff_spec(spec)
        if self.checkpoint_path.exists():
            raise BakeoffContractError("run already has a checkpoint")
        checkpoint = sign_artifact(
            "BakeoffCheckpoint",
            {
                "run_id": spec["run_id"],
                "state": RunState.SPEC_FROZEN.value,
                "spec_fingerprint": spec["artifact_fingerprint"],
                "previous_checkpoint_fingerprint": None,
                "evidence_fingerprints": {"spec": spec["artifact_fingerprint"]},
                "terminal": False,
            },
        )
        _atomic_write_json(self.spec_path, spec)
        self._write_checkpoint(checkpoint)
        return checkpoint

    def load(self) -> dict[str, Any]:
        if not self.checkpoint_path.is_file():
            raise BakeoffContractError("run checkpoint does not exist")
        value = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        validate_signed_artifact(value, kind="BakeoffCheckpoint")
        history = sorted(self.history_dir.glob("*.json"))
        if not history:
            raise BakeoffContractError("checkpoint history is missing")
        latest = json.loads(history[-1].read_text(encoding="utf-8"))
        validate_signed_artifact(latest, kind="BakeoffCheckpoint")
        if latest["artifact_fingerprint"] != value["artifact_fingerprint"]:
            raise BakeoffContractError("current checkpoint diverges from immutable history")
        previous = None
        for path in history:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            validate_signed_artifact(receipt, kind="BakeoffCheckpoint")
            if receipt.get("previous_checkpoint_fingerprint") != previous:
                raise BakeoffContractError("checkpoint history chain is broken")
            previous = receipt["artifact_fingerprint"]
        return value

    def _validate_transition_artifact(  # noqa: C901, PLR0912
        self,
        target: RunState,
        artifact: Mapping[str, Any],
        spec: Mapping[str, Any],
        evidence: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if (
            artifact.get("run_id") != spec["run_id"]
            or artifact.get("spec_fingerprint") != spec["artifact_fingerprint"]
        ):
            raise BakeoffContractError("transition evidence is bound to a different run/spec")
        if target is RunState.DEV_INDEXED:
            if artifact.get("coverage_complete") is not True or artifact.get("failures") != []:
                raise BakeoffContractError("development index receipt is incomplete")
        elif target is RunState.DEV_RETRIEVED:
            if artifact.get("complete_query_count") != 400 or artifact.get("failures") != []:
                raise BakeoffContractError("development retrieval bundle is incomplete")
        elif target is RunState.DEV_JUDGED:
            if (
                artifact.get("judge_artifact_count") != 3
                or artifact.get("unresolved_disagreements") != 0
            ):
                raise BakeoffContractError("development judgments are incomplete")
        elif target is RunState.DEV_WINNER_SIGNED:
            validate_development_winner(artifact, spec)
        elif target is RunState.BLIND_UNLOCKED:
            winner_artifact = next(
                (item for item in evidence.values() if item.get("kind") == "DevelopmentWinner"),
                None,
            )
            if winner_artifact is None:
                raise BakeoffContractError(
                    "blind unlock transition requires its development winner"
                )
            validate_blind_unlock(artifact, spec, winner_artifact)
        elif target is RunState.BLIND_COMPLETE:
            validate_blind_evaluation(artifact, spec)
        elif target in {RunState.PROMOTED, RunState.RETAINED}:
            validate_promotion_decision(artifact, spec)
            blind = next(
                (item for item in evidence.values() if item.get("kind") == "BlindEvaluation"), None
            )
            winner = next(
                (item for item in evidence.values() if item.get("kind") == "DevelopmentWinner"),
                None,
            )
            unlock = next(
                (item for item in evidence.values() if item.get("kind") == "BlindUnlock"), None
            )
            receipt = next(
                (
                    item
                    for item in evidence.values()
                    if item.get("kind") == "BlindQueryManifestReceipt"
                ),
                None,
            )
            if any(item is None for item in (blind, winner, unlock, receipt)):
                raise BakeoffContractError(
                    "terminal decision requires the complete custody evidence chain"
                )
            expected = {
                "development_winner": winner["artifact_fingerprint"],
                "blind_evaluation": blind["artifact_fingerprint"],
                "blind_unlock": unlock["artifact_fingerprint"],
                "blind_manifest_receipt": receipt["artifact_fingerprint"],
            }
            if artifact["evidence_fingerprints"] != expected:
                raise BakeoffContractError(
                    "promotion decision evidence does not bind the supplied custody chain"
                )

    def transition(
        self,
        target: RunState,
        evidence: Mapping[str, Mapping[str, Any]],
        *,
        expected_spec_fingerprint: str,
    ) -> dict[str, Any]:
        current = self.load()
        spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        validate_bakeoff_spec(spec)
        if current.get("terminal") is True:
            raise BakeoffContractError("terminal runs cannot be restarted under the same run ID")
        if current.get("spec_fingerprint") != expected_spec_fingerprint:
            raise BakeoffContractError("checkpoint spec fingerprint is stale")
        source = RunState(current["state"])
        if target not in ALLOWED_TRANSITIONS[source]:
            raise BakeoffContractError(f"invalid transition {source.value} -> {target.value}")
        if not evidence:
            raise BakeoffContractError("every transition requires signed evidence")
        fingerprints: dict[str, str] = {}
        kinds: set[str] = set()
        for name, artifact in evidence.items():
            validate_signed_artifact(artifact)
            fingerprints[_require_text(name, "evidence name")] = artifact["artifact_fingerprint"]
            kinds.add(str(artifact["kind"]))
        required_kind = TRANSITION_EVIDENCE_KINDS[target]
        if required_kind not in kinds:
            raise BakeoffContractError(
                f"transition to {target.value} requires {required_kind} evidence"
            )
        required_artifact = next(
            item for item in evidence.values() if item["kind"] == required_kind
        )
        self._validate_transition_artifact(target, required_artifact, spec, evidence)
        if target in {RunState.PROMOTED, RunState.RETAINED}:
            decision = next(item for item in evidence.values() if item["kind"] == required_kind)
            expected_promotion = target is RunState.PROMOTED
            if decision.get("promotion") is not expected_promotion:
                raise BakeoffContractError("terminal state contradicts PromotionDecision")
        checkpoint = sign_artifact(
            "BakeoffCheckpoint",
            {
                "run_id": current["run_id"],
                "state": target.value,
                "spec_fingerprint": expected_spec_fingerprint,
                "previous_checkpoint_fingerprint": current["artifact_fingerprint"],
                "evidence_fingerprints": fingerprints,
                "terminal": target in {RunState.PROMOTED, RunState.RETAINED},
            },
        )
        for artifact in evidence.values():
            evidence_path = self.evidence_dir / f"{artifact['artifact_fingerprint']}.json"
            if not evidence_path.exists():
                _atomic_write_json(evidence_path, artifact)
        self._write_checkpoint(checkpoint)
        return checkpoint


@dataclass(frozen=True)
class BlindVault:
    """Custodian-only storage whose read path validates a winner-specific unlock first."""

    vault_dir: Path

    @property
    def slots_path(self) -> Path:
        return self.vault_dir / "blind_slots.sealed.json"

    def seal_slots(
        self, slots: Sequence[Mapping[str, Any]], spec: Mapping[str, Any], *, custodian: str
    ) -> None:
        validate_bakeoff_spec(spec)
        if custodian != MAIN_CUSTODIAN:
            raise BlindAccessError("only the main custodian may seal blind slots")
        if not slots:
            raise BakeoffContractError("blind slot vault cannot be empty")
        payload = {"run_id": spec["run_id"], "slots": list(slots)}
        if fingerprint(payload["slots"]) != spec["query_slots_hash"]:
            raise BakeoffContractError("blind slots do not match the frozen spec")
        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.vault_dir, 0o700)
        _atomic_write_json(self.slots_path, sign_artifact("SealedBlindSlots", payload))

    def open_slots(
        self,
        unlock: object,
        spec: Mapping[str, Any],
        winner: Mapping[str, Any],
        *,
        custodian: str,
    ) -> list[dict[str, Any]]:
        if custodian != MAIN_CUSTODIAN:
            raise BlindAccessError("only the main custodian may open blind slots")
        # Validate authorization before the first filesystem read.  Tests patch Path.read_text
        # to prove pre-unlock calls never touch the sealed file.
        validate_blind_unlock(unlock, spec, winner)
        value = json.loads(self.slots_path.read_text(encoding="utf-8"))
        validate_signed_artifact(value, kind="SealedBlindSlots")
        if (
            value.get("run_id") != spec["run_id"]
            or fingerprint(value.get("slots")) != spec["query_slots_hash"]
        ):
            raise BlindAccessError("sealed blind slots do not match the experiment")
        return list(value["slots"])


def authorize_blind_file(
    blind_path: Path,
    vault_dir: Path,
    spec_path: Path,
    winner_path: Path,
    unlock_path: Path,
    manifest_receipt_path: Path,
    *,
    custodian: str = MAIN_CUSTODIAN,
) -> bytes:
    """Validate authorization before a caller opens a private blind artifact.

    The authorization artifacts are separate from the blind file.  After validating them, this
    function performs the only target read, verifies its frozen hash, and returns those bytes.
    """
    if custodian != MAIN_CUSTODIAN:
        raise BlindAccessError("only the main custodian may authorize blind file access")
    resolved_vault = vault_dir.resolve()
    resolved_blind = blind_path.resolve()
    if resolved_blind == resolved_vault or resolved_vault not in resolved_blind.parents:
        raise BlindAccessError("blind artifact must be stored inside the run-private vault")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    winner = json.loads(winner_path.read_text(encoding="utf-8"))
    unlock = json.loads(unlock_path.read_text(encoding="utf-8"))
    validate_blind_unlock(unlock, spec, winner)
    receipt = json.loads(manifest_receipt_path.read_text(encoding="utf-8"))
    validate_blind_manifest_receipt(receipt, spec, winner, unlock)
    payload = blind_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt["file_sha256"]:
        raise BlindAccessError("blind artifact bytes do not match the frozen manifest hash")
    return payload
