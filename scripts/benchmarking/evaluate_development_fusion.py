"""Leakage-safe development-only fusion comparison for the Gate-D devrun2 artifacts.

This evaluator deliberately consumes only the signed development query manifest, judging matrix,
merged development labels, and the lexical/BGE-small entity retrieval bundles.  It never accepts
blind/vault/source-slot inputs and does not import the live search service.  The baseline is the
deployed FTS5 + BGE-small equal-weight rank-RRF hybrid; contenders are fixed score-normalized
fusion grids and a grouped-family cross-validation pairwise linear ranker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from eval_stats import ndcg_at_10, semantic_recall_at_20  # noqa: E402
from bakeoff_state import (  # noqa: E402
    validate_bakeoff_spec,
    validate_corpus_manifest,
    validate_signed_artifact,
)
from build_evaluation_queries import load_manifest, validate_queries  # noqa: E402
from build_judging_matrix import load_frozen_entity_text  # noqa: E402
from judge_pool import judge_version_fingerprint as _judge_version_fingerprint  # noqa: E402
from merge_judgments import (  # noqa: E402
    RawJudgment,
    compute_pairwise_agreement,
    merge_query_judgments,
)
from ranking_architecture import (  # noqa: E402
    PairwiseExample,
    RankedCandidate,
    grouped_family_folds,
    normalized_linear_fusion,
    score_linear,
    train_pairwise_linear_ranker,
    validate_feature_schema,
)


RUN_ID = "accuracy-bakeoff-20260812-devrun2"
BGE_SMALL_MODEL_ID = "BAAI/bge-small-en-v1.5"
BGE_SMALL_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"
TOP_N = 20
RRF_K = 60
PAIRWISE_FEATURES = (
    "bm25_score",
    "bm25_rank",
    "dense_entity_score",
    "dense_entity_rank",
    "channel_agreement",
)
PAIRWISE_FOLD_COUNT = 5
PAIRWISE_REGULARIZATION = 0.01
PAIRWISE_LEARNING_RATE = 0.05
PAIRWISE_EPOCHS = 100
DETERMINISTIC_SEED = 7
SCORE_FUSION_GRID = (
    (0.5, 1.0),
    (1.0, 1.0),
    (1.5, 1.0),
    (2.0, 1.0),
    (1.0, 1.5),
    (1.0, 2.0),
)
PROTECTED_PATH_PARTS = (
    "blind",
    "vault",
    "unlock",
    "source_slots",
    "retrieval-audit",
    "historical_audit",
)


class DevelopmentFusionError(ValueError):
    """Raised for malformed, mismatched, or protected evaluation inputs."""


def _sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_protected_path(path: Path) -> None:
    lowered = str(path).casefold().replace("\\", "/")
    if any(part in lowered for part in PROTECTED_PATH_PARTS):
        raise DevelopmentFusionError(f"protected/non-development input is forbidden: {path}")


def _read_json(path: Path) -> dict[str, Any]:
    _reject_protected_path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevelopmentFusionError(f"cannot read JSON artifact {path}") from exc
    if not isinstance(value, dict):
        raise DevelopmentFusionError(f"artifact must be a JSON object: {path}")
    return value


def _artifact_fingerprint(value: Mapping[str, Any], field: str) -> str:
    fingerprint = value.get(field)
    if not isinstance(fingerprint, str) or not fingerprint:
        raise DevelopmentFusionError(f"artifact lacks {field}")
    unsigned = dict(value)
    unsigned.pop(field, None)
    actual = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if actual != fingerprint:
        raise DevelopmentFusionError(f"{field} mismatch")
    return fingerprint


def _finite(value: Any, *, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _nearest_rank(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else 0.0


def _load_queries(
    path: Path, spec: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], str, str]:
    _reject_protected_path(path)
    try:
        query_bytes = path.read_bytes()
    except OSError as exc:
        raise DevelopmentFusionError(
            f"cannot read signed development query manifest: {path}"
        ) from exc
    try:
        artifact = load_manifest(
            path,
            raw_bytes=query_bytes,
            expected_split="dev",
            expected_corpus_fingerprint=spec["corpus_snapshot_hash"],
            require_provenance=True,
        )
        validate_queries(artifact["queries"])
    except (OSError, ValueError, KeyError) as exc:
        raise DevelopmentFusionError(f"invalid signed development query manifest: {path}") from exc
    query_fingerprint = artifact.get("manifest_fingerprint")
    if not isinstance(query_fingerprint, str):
        raise DevelopmentFusionError("queries-dev artifact lacks manifest_fingerprint")
    queries = artifact.get("queries")
    if not isinstance(queries, list) or len(queries) != 400:
        raise DevelopmentFusionError("queries-dev must contain exactly 400 queries")
    indexed: dict[str, dict[str, Any]] = {}
    for query in queries:
        if (
            not isinstance(query, dict)
            or query.get("split") != "dev"
            or not isinstance(query.get("id"), str)
            or query["id"] in indexed
            or not isinstance(query.get("query"), str)
            or not isinstance(query.get("topic_family_id"), str)
        ):
            raise DevelopmentFusionError("queries-dev contains malformed/non-dev rows")
        indexed[query["id"]] = query
    return indexed, query_fingerprint, hashlib.sha256(query_bytes).hexdigest()


def _validate_parent_binding(  # noqa: C901
    # The compatibility proof cross-checks every historical custody edge.
    path: Path,
    *,
    spec: Mapping[str, Any],
    matrix: Mapping[str, Any],
    labels: Mapping[str, Any],
    queries_path: Path,
    labels_path: Path,
    query_fingerprint: str,
    canonical_query_fingerprint: str,
    query_file_sha256: str | None = None,
) -> None:
    try:
        envelope = validate_signed_artifact(_read_json(path), kind="DevelopmentParentBinding")
    except ValueError as exc:
        raise DevelopmentFusionError("invalid signed historical parent binding") from exc
    binding = envelope.get("binding")
    expected = {
        "parent_matrix_fingerprint": matrix.get("artifact_fingerprint"),
        "spec_fingerprint": spec.get("artifact_fingerprint"),
        "query_manifest_fingerprint": query_fingerprint,
        "query_manifest_file_sha256": query_file_sha256 or _sha256_bytes(queries_path),
        "query_split": "dev",
        "historical_labels_fingerprint": labels.get("fingerprint"),
        "historical_labels_file_sha256": _sha256_bytes(labels_path),
        "corpus_root_hash": spec.get("corpus_snapshot_hash"),
        "label_pair_count": len(
            {
                (label.get("query_id"), label.get("candidate_id"))
                for label in labels.get("labels", [])
            }
        ),
    }
    if envelope.get("evidence_tier") != "legacy_parent_compatibility_proof":
        raise DevelopmentFusionError("historical parent binding does not match exact dev inputs")
    if any(binding.get(key) != value for key, value in expected.items()):
        raise DevelopmentFusionError("historical parent binding does not match exact dev inputs")
    required = (
        "canonical_queries_fingerprint",
        "canonical_matrix_pools_fingerprint",
        "canonical_raw_labels_fingerprint",
        "mapping_records",
        "packet_records",
        "ingested_label_records",
        "judge_roles",
    )
    if any(key not in binding for key in required):
        raise DevelopmentFusionError("historical parent binding lacks packet/mapping custody")
    if binding["canonical_queries_fingerprint"] != canonical_query_fingerprint:
        raise DevelopmentFusionError("historical mapping query fingerprint mismatch")
    pools = matrix.get("pools")
    canonical_pool_fp = hashlib.sha256(
        json.dumps(pools, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if binding["canonical_matrix_pools_fingerprint"] != canonical_pool_fp:
        raise DevelopmentFusionError("historical mapping pool fingerprint mismatch")
    if binding["canonical_raw_labels_fingerprint"] != labels.get("raw_labels_fingerprint"):
        raise DevelopmentFusionError("historical raw-label aggregate mismatch")
    roles = ["agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c"]
    if binding["judge_roles"] != roles:
        raise DevelopmentFusionError("historical parent judge-role coverage mismatch")
    mappings = binding["mapping_records"]
    packets = binding["packet_records"]
    raws = binding["ingested_label_records"]
    if (
        [row.get("judge") for row in mappings] != roles
        or [row.get("judge") for row in packets] != roles
        or [row.get("judge") for row in raws] != roles
    ):
        raise DevelopmentFusionError("historical parent role records are incomplete")
    if any(
        row.get("queries_fingerprint") != canonical_query_fingerprint
        or row.get("matrix_pools_fingerprint") != canonical_pool_fp
        or not isinstance(row.get("packet_fingerprint"), str)
        or len(row["packet_fingerprint"]) != 64
        for row in mappings
    ):
        raise DevelopmentFusionError("historical mapping packet/query/pool binding mismatch")
    if any(
        packet.get("fingerprint") != mapping.get("packet_fingerprint")
        for packet, mapping in zip(packets, mappings, strict=True)
    ):
        raise DevelopmentFusionError("historical packet fingerprint mismatch")
    if any(
        not isinstance(row.get("mapping_fingerprint"), str)
        or row["mapping_fingerprint"] != mapping.get("fingerprint")
        or row.get("coverage_fingerprint") != mapping.get("coverage_fingerprint")
        for row, mapping in zip(raws, mappings, strict=True)
    ):
        raise DevelopmentFusionError("historical ingested-label mapping binding mismatch")


def _load_relevance(
    matrix: Mapping[str, Any],
    labels_artifact: Mapping[str, Any],
    query_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, int]], str]:
    if matrix.get("query_count") != 400 or set(matrix.get("pools", {})) != query_ids:
        raise DevelopmentFusionError("judging matrix does not cover exactly development queries")
    pools = matrix.get("pools")
    if not isinstance(pools, dict):
        raise DevelopmentFusionError("judging matrix pools must be an object")
    merged_fingerprint = _artifact_fingerprint(labels_artifact, "fingerprint")
    labels = labels_artifact.get("labels")
    if not isinstance(labels, list):
        raise DevelopmentFusionError("merged development labels must contain labels")
    relevance: dict[str, dict[str, int]] = defaultdict(dict)
    for label in labels:
        if not isinstance(label, dict):
            raise DevelopmentFusionError("malformed development label")
        query_id, candidate_id, grade = (
            label.get("query_id"),
            label.get("candidate_id"),
            label.get("final_grade"),
        )
        if (
            not isinstance(query_id, str)
            or query_id not in query_ids
            or not isinstance(candidate_id, str)
            or grade not in (0, 1, 2)
            or candidate_id in relevance[query_id]
        ):
            raise DevelopmentFusionError("malformed or duplicate development label")
        relevance[query_id][candidate_id] = grade
    if any(set(relevance[qid]) != set(pools[qid]) for qid in query_ids):
        raise DevelopmentFusionError("development labels do not exactly cover judging pools")
    return pools, dict(relevance), merged_fingerprint


ADDENDUM_JUDGES = (
    "agent_eval_judge_raw_a",
    "agent_eval_judge_raw_b",
    "agent_eval_judge_raw_c",
)


def _load_addendum_relevance(  # noqa: C901, PLR0912, PLR0915
    addendum_matrix: Mapping[str, Any],
    labels_artifact: Mapping[str, Any],
    *,
    parent_matrix: Mapping[str, Any],
    query_ids: set[str],
    old_pairs: set[tuple[str, str]],
    raw_artifacts: list[Mapping[str, Any]],
    query_source_ids: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, int]], str]:
    """Validate and load only a complete, provenance-rich external addendum merge."""
    try:
        validate_signed_artifact(addendum_matrix, kind="JudgingMatrix")
    except ValueError as exc:
        raise DevelopmentFusionError("invalid signed raw lexical judging addendum") from exc
    if addendum_matrix.get("spec_fingerprint") != parent_matrix.get("spec_fingerprint"):
        raise DevelopmentFusionError("addendum matrix spec binding differs from parent matrix")
    if addendum_matrix.get("parent_judging_matrix_fingerprint") != parent_matrix.get(
        "artifact_fingerprint"
    ):
        raise DevelopmentFusionError("addendum matrix parent binding is stale")
    parent_query_binding = parent_matrix.get("query_manifest_fingerprint")
    if (
        parent_query_binding is not None
        and addendum_matrix.get("query_manifest_fingerprint") != parent_query_binding
    ):
        raise DevelopmentFusionError("addendum matrix query binding differs from parent matrix")
    pools = addendum_matrix.get("pools")
    if (
        addendum_matrix.get("query_count") != 172
        or addendum_matrix.get("missing_pair_count") != 241
        or not isinstance(pools, dict)
        or set(pools) != set(query_ids).intersection(pools)
        or len(pools) != 172
    ):
        raise DevelopmentFusionError("addendum matrix does not cover exactly 172 affected queries")
    expected_pairs = {
        (query_id, candidate_id)
        for query_id, candidates in pools.items()
        for candidate_id in candidates
    }
    if len(expected_pairs) != 241 or expected_pairs & old_pairs:
        raise DevelopmentFusionError("addendum matrix pairs are not exactly the missing raw pairs")
    fingerprint_field = (
        "fingerprint" if "fingerprint" in labels_artifact else "artifact_fingerprint"
    )
    merged_fingerprint = _artifact_fingerprint(labels_artifact, fingerprint_field)
    if labels_artifact.get("kind") not in {None, "DevelopmentJudgingAddendumLabels"}:
        raise DevelopmentFusionError("unexpected addendum-label artifact kind")
    if labels_artifact.get("judges") != list(ADDENDUM_JUDGES):
        raise DevelopmentFusionError("addendum labels lack the three raw lexical judge roles")
    if labels_artifact.get("adjudicator") != "agent_eval_adjudicator":
        raise DevelopmentFusionError("addendum labels lack the configured adjudicator")
    if not isinstance(labels_artifact.get("judge_version"), str):
        raise DevelopmentFusionError("addendum labels lack judge-version provenance")
    if not isinstance(labels_artifact.get("raw_labels_fingerprint"), str):
        raise DevelopmentFusionError("addendum labels lack raw-label provenance")
    expected_binding = {
        key: addendum_matrix.get(key)
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
    }
    if labels_artifact.get("addendum_binding") != expected_binding:
        raise DevelopmentFusionError("addendum labels lack exact frozen-input binding")
    if len(raw_artifacts) != 3:
        raise DevelopmentFusionError("addendum merge requires exactly three raw label artifacts")
    raw_roles = ADDENDUM_JUDGES
    raw_by_role = {}
    raw_labels_by_role: dict[str, dict[tuple[str, str], int]] = {}
    for artifact, role in zip(raw_artifacts, raw_roles, strict=True):
        try:
            # Raw labels are emitted by judge_pool.ingest_labels, whose signed
            # contract uses ``fingerprint`` (the bakeoff bundles use the
            # separate ``artifact_fingerprint`` field).  Do not silently
            # accept the latter here: accepting a different artifact family
            # would weaken the three-independent-judge custody proof.
            _artifact_fingerprint(artifact, "fingerprint")
        except DevelopmentFusionError as exc:
            raise DevelopmentFusionError("raw addendum label artifact is not signed") from exc
        if (
            artifact.get("judge") != role
            or artifact.get("judge_role_set") != "raw_addendum"
            or artifact.get("judge_version_fingerprint")
            != _judge_version_fingerprint(tuple(raw_roles))
            or artifact.get("matrix_binding") != expected_binding
            or artifact.get("label_count") != 241
            or not isinstance(artifact.get("labels"), list)
        ):
            raise DevelopmentFusionError("raw addendum label role or matrix binding mismatch")
        pairs = {(item.get("query_id"), item.get("candidate_id")) for item in artifact["labels"]}
        if pairs != expected_pairs or any(
            item.get("grade") not in (0, 1, 2) for item in artifact["labels"]
        ):
            raise DevelopmentFusionError("raw addendum label artifact coverage is incomplete")
        raw_by_role[role] = artifact
        raw_labels_by_role[role] = {
            (item["query_id"], item["candidate_id"]): item["grade"] for item in artifact["labels"]
        }
    if any(
        raw_by_role[role].get("judge_version") != labels_artifact.get("judge_version")
        for role in raw_roles
    ):
        raise DevelopmentFusionError(
            "merged addendum judge-version provenance differs from raw roles"
        )
    raw_judgments = [
        RawJudgment(role, item["query_id"], item["candidate_id"], item["grade"])
        for role in raw_roles
        for item in raw_by_role[role]["labels"]
    ]
    canonical_raw_fingerprint = hashlib.sha256(
        json.dumps(
            [raw_by_role[role] for role in raw_roles],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if labels_artifact.get("raw_labels_fingerprint") != canonical_raw_fingerprint:
        raise DevelopmentFusionError("merged addendum raw-label aggregate does not match artifacts")
    expected_agreement = []
    for index, left in enumerate(raw_roles):
        for right in raw_roles[index + 1 :]:
            agreement = compute_pairwise_agreement(raw_judgments, left, right)
            expected_agreement.append(
                {
                    "judges": [left, right],
                    "n": agreement.n,
                    "exact_agreement_rate": agreement.exact_agreement_rate,
                    "cohens_kappa": agreement.cohens_kappa,
                    "confusion": {
                        f"{a}:{b}": count for (a, b), count in agreement.confusion.items()
                    },
                }
            )
    if (
        not isinstance(labels_artifact.get("agreement"), list)
        or labels_artifact["agreement"] != expected_agreement
    ):
        raise DevelopmentFusionError("addendum labels agreement does not match raw artifacts")
    escalation = labels_artifact.get("escalation")
    if not isinstance(escalation, dict) or not isinstance(escalation.get("count"), int):
        raise DevelopmentFusionError("addendum labels lack escalation/adjudication provenance")
    labels = labels_artifact.get("labels")
    if not isinstance(labels, list) or len(labels) != 241:
        raise DevelopmentFusionError("addendum labels must contain exactly 241 labels")
    relevance: dict[str, dict[str, int]] = defaultdict(dict)
    expected_escalations = 0
    for label in labels:
        if not isinstance(label, dict):
            raise DevelopmentFusionError("malformed addendum label")
        query_id, candidate_id = label.get("query_id"), label.get("candidate_id")
        final_grade = label.get("final_grade")
        raw_grades = label.get("raw_grades")
        if (
            not isinstance(query_id, str)
            or not isinstance(candidate_id, str)
            or (query_id, candidate_id) not in expected_pairs
            or candidate_id in relevance.get(query_id, {})
            or final_grade not in (0, 1, 2)
            or not isinstance(raw_grades, dict)
            or set(raw_grades) != set(ADDENDUM_JUDGES)
            or any(grade not in (0, 1, 2) for grade in raw_grades.values())
            or label.get("median_grade") not in (0, 1, 2)
            or not isinstance(label.get("escalated"), bool)
        ):
            raise DevelopmentFusionError("malformed or incomplete addendum label provenance")
        if label["escalated"] and label.get("arbitrated_grade") not in (0, 1, 2):
            raise DevelopmentFusionError("escalated addendum label lacks adjudicated grade")
        if not label["escalated"] and label.get("arbitrated_grade") is not None:
            raise DevelopmentFusionError("non-escalated addendum label has adjudicated grade")
        pair_raw = [
            RawJudgment(
                role,
                query_id,
                candidate_id,
                raw_labels_by_role[role][(query_id, candidate_id)],
            )
            for role in raw_roles
        ]
        merged = merge_query_judgments(
            pair_raw,
            list((query_source_ids or {}).get(query_id, ())),
            judges=tuple(raw_roles),
        )[0]
        if (
            label.get("raw_grades") != merged.raw_grades
            or label.get("median_grade") != merged.median_grade
            or label.get("escalated") != merged.escalated
            or label.get("escalation_reason") != merged.escalation_reason
        ):
            raise DevelopmentFusionError("merged addendum label disagrees with raw grades")
        if merged.escalated:
            expected_escalations += 1
            if label.get("arbitrated_grade") not in (0, 1, 2):
                raise DevelopmentFusionError("escalated addendum label lacks adjudicated grade")
        elif label.get("arbitrated_grade") is not None:
            raise DevelopmentFusionError("non-escalated addendum label has adjudicated grade")
        expected_final = (
            label["arbitrated_grade"]
            if label.get("arbitrated_grade") is not None
            else merged.median_grade
        )
        if label.get("final_grade") != expected_final:
            raise DevelopmentFusionError("merged addendum final grade disagrees with raw grades")
        relevance.setdefault(query_id, {})[candidate_id] = final_grade
    if labels_artifact["escalation"].get("count") != expected_escalations:
        raise DevelopmentFusionError("addendum escalation count does not match raw artifacts")
    if labels_artifact["escalation"].get("total") != len(labels) or labels_artifact[
        "escalation"
    ].get("rate") != (expected_escalations / len(labels) if labels else None):
        raise DevelopmentFusionError("addendum escalation totals do not match raw artifacts")
    if {
        (query_id, candidate_id) for query_id, rows in relevance.items() for candidate_id in rows
    } != expected_pairs:
        raise DevelopmentFusionError("addendum labels do not exactly cover addendum pools")
    return dict(pools), dict(relevance), merged_fingerprint


def _load_corpus_identity(
    manifest_path: Path, export_path: Path, spec: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    try:
        manifest = validate_corpus_manifest(_read_json(manifest_path))
        if manifest.get("corpus_root_hash") != spec.get("corpus_snapshot_hash"):
            raise DevelopmentFusionError("corpus manifest is bound to a different BakeoffSpec")
        export = _read_json(export_path)
        entity_text = load_frozen_entity_text(export, manifest)
    except (KeyError, ValueError) as exc:
        raise DevelopmentFusionError("invalid frozen corpus identity inputs") from exc
    return (
        {entity_id: row["title"] for entity_id, row in entity_text.items()},
        {
            "corpus_manifest": manifest["artifact_fingerprint"],
            "corpus_export_sha256": _sha256_bytes(export_path),
            "corpus_manifest_file_sha256": _sha256_bytes(manifest_path),
        },
    )


def _validate_bundle(
    bundle: Mapping[str, Any],
    expected_kind: str,
    query_ids: set[str],
    pools: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if bundle.get("run_id") != RUN_ID or bundle.get("complete_query_count") != 400:
        raise DevelopmentFusionError(f"{expected_kind} bundle is not bound to {RUN_ID}")
    if bundle.get("failures") != []:
        raise DevelopmentFusionError(f"{expected_kind} bundle contains failures")
    cell = bundle.get("cell")
    if not isinstance(cell, dict) or cell.get("kind") != expected_kind:
        observed_kind = cell.get("kind") if isinstance(cell, dict) else None
        raise DevelopmentFusionError(f"expected {expected_kind} bundle cell, got {observed_kind}")
    result_rows = bundle.get("results")
    if not isinstance(result_rows, list) or len(result_rows) != 400:
        raise DevelopmentFusionError(f"{expected_kind} bundle must contain 400 results")
    indexed: dict[str, dict[str, Any]] = {}
    for row in result_rows:
        if not isinstance(row, dict) or not isinstance(row.get("query_id"), str):
            raise DevelopmentFusionError(f"malformed {expected_kind} result")
        query_id = row["query_id"]
        if query_id in indexed or query_id not in query_ids or row.get("failure") is not None:
            raise DevelopmentFusionError(f"invalid {expected_kind} query row {query_id}")
        hits = row.get("top20")
        if not isinstance(hits, list) or len(hits) > TOP_N:
            raise DevelopmentFusionError(f"invalid {expected_kind} top20")
        ids = [hit.get("entity_id") for hit in hits if isinstance(hit, dict)]
        if len(ids) != len(hits) or len(ids) != len(set(ids)):
            raise DevelopmentFusionError(f"duplicate/malformed {expected_kind} IDs")
        if not set(ids) <= set(pools[query_id]):
            raise DevelopmentFusionError(f"{expected_kind} result escapes judging pool")
        indexed[query_id] = row
    if set(indexed) != query_ids:
        raise DevelopmentFusionError(f"{expected_kind} result coverage mismatch")
    return indexed


def _validate_signed_bundle(
    bundle: Mapping[str, Any],
    expected_kind: str,
    expected_spec_fingerprint: str,
    spec_corpus_fingerprint: str,
    query_ids: set[str],
    pools: Mapping[str, Mapping[str, Any]],
    query_binding: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    try:
        signed = validate_signed_artifact(bundle, kind="RetrievalRunBundle")
    except ValueError as exc:
        raise DevelopmentFusionError(f"invalid signed {expected_kind} RetrievalRunBundle") from exc
    if signed.get("spec_fingerprint") != expected_spec_fingerprint:
        raise DevelopmentFusionError(f"{expected_kind} bundle is bound to a different BakeoffSpec")
    for field, expected_value in query_binding.items():
        if signed.get(field) != expected_value:
            raise DevelopmentFusionError(f"{expected_kind} bundle has stale/missing {field}")
    cell = signed.get("cell")
    if not isinstance(cell, dict) or cell.get("representation_root") != spec_corpus_fingerprint:
        raise DevelopmentFusionError(
            f"{expected_kind} bundle representation root is not the frozen corpus snapshot"
        )
    if expected_kind == "dense" and (
        not isinstance(cell, dict)
        or cell.get("kind") != "dense"
        or cell.get("model_id") != BGE_SMALL_MODEL_ID
        or cell.get("channel") != "entity"
        or cell.get("revision") != BGE_SMALL_REVISION
    ):
        raise DevelopmentFusionError(
            "dense bundle is not the pinned BAAI/bge-small-en-v1.5 entity revision"
        )
    if expected_kind == "dense":
        receipt = signed.get("index_receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("kind") != "dense"
            or receipt.get("ready") != 1
            or receipt.get("representation_root") != spec_corpus_fingerprint
            or receipt.get("compatibility_key") != cell.get("compatibility_key")
            or not isinstance(receipt.get("receipt_fingerprint"), str)
            or len(receipt["receipt_fingerprint"]) != 64
        ):
            raise DevelopmentFusionError("dense bundle lacks a valid corpus-bound index receipt")
    if expected_kind == "lexical" and (
        not isinstance(cell, dict)
        or cell.get("kind") != "lexical"
        or cell.get("channel") != "bm25_raw_production"
        or cell.get("lexical_policy") != "raw_production"
        or cell.get("production_faithful") is not True
    ):
        raise DevelopmentFusionError("lexical bundle is not the pinned raw production FTS channel")
    return _validate_bundle(signed, expected_kind, query_ids, pools)


def load_inputs(  # noqa: C901, PLR0912, PLR0915
    queries_path: Path,
    matrix_path: Path,
    labels_path: Path,
    lexical_path: Path,
    dense_path: Path,
    spec_path: Path,
    corpus_manifest_path: Path,
    corpus_export_path: Path,
    addendum_matrix_path: Path | None = None,
    addendum_labels_path: Path | None = None,
    parent_binding_path: Path | None = None,
    addendum_raw_labels_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Load and validate only development artifacts, returning immutable-ish plain data."""
    try:
        spec = validate_bakeoff_spec(_read_json(spec_path))
    except DevelopmentFusionError:
        raise
    except ValueError as exc:
        raise DevelopmentFusionError("invalid signed BakeoffSpec") from exc
    if spec.get("run_id") != RUN_ID:
        raise DevelopmentFusionError("BakeoffSpec run_id does not match the development run")
    corpus_titles, corpus_fingerprints = _load_corpus_identity(
        corpus_manifest_path, corpus_export_path, spec
    )
    queries, query_fingerprint, query_file_sha256 = _load_queries(queries_path, spec)
    query_binding = {
        "query_manifest_fingerprint": query_fingerprint,
        "query_manifest_file_sha256": query_file_sha256,
        "query_split": "dev",
    }
    try:
        matrix = validate_signed_artifact(_read_json(matrix_path), kind="JudgingMatrix")
    except ValueError as exc:
        raise DevelopmentFusionError("invalid signed JudgingMatrix") from exc
    if matrix.get("spec_fingerprint") != spec["artifact_fingerprint"]:
        raise DevelopmentFusionError("JudgingMatrix is bound to a different BakeoffSpec")
    labels_artifact = _read_json(labels_path)
    if matrix.get("query_manifest_fingerprint") is None:
        if parent_binding_path is None:
            raise DevelopmentFusionError("JudgingMatrix has stale/missing query binding")
        _validate_parent_binding(
            parent_binding_path,
            spec=spec,
            matrix=matrix,
            labels=labels_artifact,
            queries_path=queries_path,
            labels_path=labels_path,
            query_fingerprint=query_fingerprint,
            canonical_query_fingerprint=hashlib.sha256(
                json.dumps(
                    list(queries.values()),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            query_file_sha256=query_file_sha256,
        )
    else:
        for field, expected_value in query_binding.items():
            if matrix.get(field) != expected_value:
                raise DevelopmentFusionError(f"JudgingMatrix has stale/missing {field}")
    lexical = _read_json(lexical_path)
    dense = _read_json(dense_path)
    addendum_matrix_for_binding = (
        _read_json(addendum_matrix_path) if addendum_matrix_path is not None else None
    )
    if addendum_matrix_for_binding is not None:
        if lexical.get("artifact_fingerprint") != addendum_matrix_for_binding.get(
            "raw_bundle_fingerprint"
        ):
            raise DevelopmentFusionError("lexical bundle is not the addendum raw bundle")
        if lexical.get("cell") != addendum_matrix_for_binding.get("raw_cell"):
            raise DevelopmentFusionError("lexical bundle cell differs from addendum raw cell")
        cell = lexical.get("cell")
        if (
            not isinstance(cell, dict)
            or cell.get("representation_root") != spec["corpus_snapshot_hash"]
            or not isinstance(cell.get("lexical_snapshot_receipt_fingerprint"), str)
            or len(cell["lexical_snapshot_receipt_fingerprint"]) != 64
            or not isinstance(cell.get("lexical_snapshot_db_sha256"), str)
            or len(cell["lexical_snapshot_db_sha256"]) != 64
        ):
            raise DevelopmentFusionError("lexical bundle lacks valid snapshot provenance")
    pools, relevance, merged_fingerprint = _load_relevance(matrix, labels_artifact, set(queries))
    addendum_fingerprints: dict[str, str] = {}
    if (addendum_matrix_path is None) != (addendum_labels_path is None):
        raise DevelopmentFusionError(
            "--addendum-matrix and --addendum-labels must be supplied together"
        )
    if addendum_matrix_path is not None and addendum_labels_path is not None:
        if addendum_raw_labels_paths is None or len(addendum_raw_labels_paths) != 3:
            raise DevelopmentFusionError(
                "addendum merge requires exactly three raw label artifact paths"
            )
        addendum_matrix = addendum_matrix_for_binding
        addendum_labels = _read_json(addendum_labels_path)
        raw_addendum_artifacts = [_read_json(path) for path in addendum_raw_labels_paths]
        old_pairs = {
            (query_id, candidate_id)
            for query_id, candidates in pools.items()
            for candidate_id in candidates
        }
        add_pools, add_relevance, add_fingerprint = _load_addendum_relevance(
            addendum_matrix,
            addendum_labels,
            parent_matrix=matrix,
            query_ids=set(queries),
            old_pairs=old_pairs,
            raw_artifacts=raw_addendum_artifacts,
            query_source_ids={
                query_id: query.get("source_entity_ids", []) for query_id, query in queries.items()
            },
        )
        for query_id, candidates in add_pools.items():
            if query_id in pools:
                overlap = set(pools[query_id]) & set(candidates)
                if overlap:
                    raise DevelopmentFusionError("addendum labels would overwrite old labels")
                pools[query_id].update(candidates)
                relevance[query_id].update(add_relevance[query_id])
        addendum_fingerprints = {
            "judging_addendum_matrix": addendum_matrix.get("artifact_fingerprint"),
            "judging_addendum_matrix_file_sha256": _sha256_bytes(addendum_matrix_path),
            "merged_addendum_labels": add_fingerprint,
            "merged_addendum_labels_file_sha256": _sha256_bytes(addendum_labels_path),
            "merged_addendum_raw_labels": [
                {
                    "judge": artifact.get("judge"),
                    "fingerprint": artifact.get("fingerprint"),
                    "file_sha256": _sha256_bytes(path),
                }
                for artifact, path in zip(
                    raw_addendum_artifacts, addendum_raw_labels_paths, strict=True
                )
            ],
        }
    return {
        "queries": queries,
        "pools": pools,
        "relevance": relevance,
        "lexical": _validate_signed_bundle(
            lexical,
            "lexical",
            spec["artifact_fingerprint"],
            spec["corpus_snapshot_hash"],
            set(queries),
            pools,
            query_binding,
        ),
        "dense": _validate_signed_bundle(
            dense,
            "dense",
            spec["artifact_fingerprint"],
            spec["corpus_snapshot_hash"],
            set(queries),
            pools,
            query_binding,
        ),
        "spec": spec,
        "corpus_titles": corpus_titles,
        "fingerprints": {
            "queries_dev": query_fingerprint,
            "judging_matrix": matrix.get("artifact_fingerprint"),
            "merged_dev_labels": merged_fingerprint,
            "lexical_bundle": lexical.get("artifact_fingerprint"),
            "dense_bundle": dense.get("artifact_fingerprint"),
            "bakeoff_spec": spec["artifact_fingerprint"],
            "queries_file_sha256": _sha256_bytes(queries_path),
            "judging_matrix_file_sha256": _sha256_bytes(matrix_path),
            "merged_dev_labels_file_sha256": _sha256_bytes(labels_path),
            "lexical_bundle_file_sha256": _sha256_bytes(lexical_path),
            "dense_bundle_file_sha256": _sha256_bytes(dense_path),
            "bakeoff_spec_file_sha256": _sha256_bytes(spec_path),
            **query_binding,
            **corpus_fingerprints,
            **addendum_fingerprints,
        },
    }


def _channel_maps(
    lexical_row: Mapping[str, Any], dense_row: Mapping[str, Any]
) -> tuple[list[str], dict[str, int], dict[str, int], dict[str, float], dict[str, float], float]:
    lexical_hits = lexical_row["top20"]
    dense_hits = dense_row["top20"]
    lexical_ids = [hit["entity_id"] for hit in lexical_hits]
    dense_ids = [hit["entity_id"] for hit in dense_hits]
    if len(lexical_ids) != len(set(lexical_ids)) or len(dense_ids) != len(set(dense_ids)):
        raise DevelopmentFusionError("channel contains duplicate IDs")
    union = list(dict.fromkeys([*lexical_ids, *dense_ids]))
    lexical_ranks = {candidate_id: index + 1 for index, candidate_id in enumerate(lexical_ids)}
    dense_ranks = {candidate_id: index + 1 for index, candidate_id in enumerate(dense_ids)}
    lexical_scores = {
        hit["entity_id"]: transformed
        for hit in lexical_hits
        if (transformed := _finite(hit.get("raw_bm25_score"))) is not None
    }
    # SQLite BM25 is lower-is-better; negation freezes the higher-is-better transform used below.
    lexical_scores = {candidate_id: -score for candidate_id, score in lexical_scores.items()}
    dense_scores = {
        hit["entity_id"]: score
        for hit in dense_hits
        if (score := _finite(hit.get("score"))) is not None
    }
    latencies = [
        _finite(lexical_row.get("latency_ms"), default=0.0),
        _finite(dense_row.get("latency_ms"), default=0.0),
    ]
    return union, lexical_ranks, dense_ranks, lexical_scores, dense_scores, sum(latencies)


def _channel_feature(values: Mapping[str, float], union: Sequence[str]) -> dict[str, float]:
    if not values:
        return {candidate_id: 0.0 for candidate_id in union}
    low, high = min(values.values()), max(values.values())
    span = high - low
    floor = low - max(1.0, abs(span)) * 1e-6
    if span == 0:
        return {candidate_id: (1.0 if candidate_id in values else 0.0) for candidate_id in union}
    return {candidate_id: (values.get(candidate_id, floor) - low) / span for candidate_id in union}


def _exact_title_parity(
    ranking: Sequence[str], query_text: str, corpus_titles: Mapping[str, str]
) -> tuple[list[str], dict[str, Any]]:
    matches = [candidate_id for candidate_id, title in corpus_titles.items() if title == query_text]
    # Production identity is byte-exact and corpus-wide: a unique visible title is a singleton
    # result even when it was absent from the retrieval candidate union.  Collisions fall back to
    # ordinary ranking.  This policy is applied identically to every arm.
    if len(matches) == 1:
        candidate_id = matches[0]
        return [candidate_id], {
            "matched": True,
            "candidate_id": candidate_id,
            "unique_corpus_match": True,
            "injected_out_of_union": candidate_id not in ranking,
            "mode": "production_exact_singleton",
        }
    return list(ranking), {
        "matched": False,
        "candidate_id": None,
        "unique_corpus_match": len(matches) == 1,
        "collision_count": len(matches),
        "mode": "retrieval_ranking",
    }


def _features(
    union: Sequence[str],
    lexical_ranks: Mapping[str, int],
    dense_ranks: Mapping[str, int],
    lexical_scores: Mapping[str, float],
    dense_scores: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    lexical_norm = _channel_feature(lexical_scores, union)
    dense_norm = _channel_feature(dense_scores, union)
    return {
        candidate_id: {
            "bm25_score": lexical_norm[candidate_id],
            "bm25_rank": 1.0 / lexical_ranks[candidate_id]
            if candidate_id in lexical_ranks
            else 0.0,
            "dense_entity_score": dense_norm[candidate_id],
            "dense_entity_rank": 1.0 / dense_ranks[candidate_id]
            if candidate_id in dense_ranks
            else 0.0,
            "channel_agreement": float(
                candidate_id in lexical_ranks and candidate_id in dense_ranks
            ),
        }
        for candidate_id in union
    }


def _rrf_ranking(
    union: Sequence[str], lexical_ranks: Mapping[str, int], dense_ranks: Mapping[str, int]
) -> list[str]:
    scored = {
        candidate_id: (
            1.0 / (RRF_K + lexical_ranks[candidate_id]) if candidate_id in lexical_ranks else 0.0
        )
        + (1.0 / (RRF_K + dense_ranks[candidate_id]) if candidate_id in dense_ranks else 0.0)
        for candidate_id in union
    }
    return [
        candidate_id
        for candidate_id, _ in sorted(
            scored.items(), key=lambda item: (-item[1], union.index(item[0]))
        )
    ][:TOP_N]


def _score_fusion_ranking(
    features: Mapping[str, Mapping[str, float]],
    lexical_weight: float,
    dense_weight: float,
    candidate_ids: Sequence[str] | None = None,
) -> list[str]:
    ordered_ids = candidate_ids if candidate_ids is not None else list(features)
    candidates = [
        RankedCandidate(candidate_id, features[candidate_id]) for candidate_id in ordered_ids
    ]
    ranked = normalized_linear_fusion(
        candidates,
        {"bm25_score": lexical_weight, "dense_entity_score": dense_weight},
    )
    return [candidate.candidate_id for candidate in ranked[:TOP_N]]


def _pairwise_examples(
    query_ids: Iterable[str],
    features_by_query: Mapping[str, Mapping[str, Mapping[str, float]]],
    relevance: Mapping[str, Mapping[str, int]],
) -> list[PairwiseExample]:
    examples: list[PairwiseExample] = []
    for query_id in sorted(query_ids):
        features = features_by_query[query_id]
        grades = relevance[query_id]
        candidates = {
            candidate_id: RankedCandidate(candidate_id, values)
            for candidate_id, values in features.items()
        }
        ids = sorted(candidates)
        for left in ids:
            for right in ids:
                if grades.get(left, 0) > grades.get(right, 0):
                    examples.append(
                        PairwiseExample(
                            query_id,
                            candidates[left],
                            candidates[right],
                        )
                    )
    return examples


def _pairwise_cv_rankings(
    query_ids: Sequence[str],
    family_ids: Mapping[str, str],
    features_by_query: Mapping[str, Mapping[str, Mapping[str, float]]],
    relevance: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]], dict[str, Any]]:
    validate_feature_schema(PAIRWISE_FEATURES)
    families = [family_ids[query_id] for query_id in query_ids]
    folds = grouped_family_folds(families, PAIRWISE_FOLD_COUNT)
    output: dict[str, list[str]] = {}
    models: list[dict[str, Any]] = []
    for fold_index, heldout_families in enumerate(folds):
        heldout = [query_id for query_id in query_ids if family_ids[query_id] in heldout_families]
        training = [query_id for query_id in query_ids if query_id not in heldout]
        examples = _pairwise_examples(training, features_by_query, relevance)
        weights = train_pairwise_linear_ranker(
            examples,
            PAIRWISE_FEATURES,
            regularization=PAIRWISE_REGULARIZATION,
            learning_rate=PAIRWISE_LEARNING_RATE,
            epochs=PAIRWISE_EPOCHS,
        )
        models.append(
            {
                "fold": fold_index,
                "heldout_family_ids": sorted(heldout_families),
                "training_family_ids": sorted({family_ids[query_id] for query_id in training}),
                "training_query_count": len(training),
                "pair_count": len(examples),
                "weights": weights,
            }
        )
        for query_id in heldout:
            ranked = sorted(
                features_by_query[query_id],
                key=lambda candidate_id: (
                    -score_linear(
                        RankedCandidate(candidate_id, features_by_query[query_id][candidate_id]),
                        weights,
                    ),
                    list(features_by_query[query_id]).index(candidate_id),
                ),
            )
            output[query_id] = ranked[:TOP_N]
    if set(output) != set(query_ids):
        raise DevelopmentFusionError("pairwise grouped-family CV did not cover every query")
    final_training = list(query_ids)
    final_examples = _pairwise_examples(final_training, features_by_query, relevance)
    final_weights = train_pairwise_linear_ranker(
        final_examples,
        PAIRWISE_FEATURES,
        regularization=PAIRWISE_REGULARIZATION,
        learning_rate=PAIRWISE_LEARNING_RATE,
        epochs=PAIRWISE_EPOCHS,
    )
    final_model = {
        "training_scope": "all_development_queries_after_grouped_family_cv",
        "training_query_count": len(final_training),
        "pair_count": len(final_examples),
        "feature_order": list(PAIRWISE_FEATURES),
        "regularization": PAIRWISE_REGULARIZATION,
        "learning_rate": PAIRWISE_LEARNING_RATE,
        "epochs": PAIRWISE_EPOCHS,
        "seed": DETERMINISTIC_SEED,
        "weights": final_weights,
    }
    final_model["model_fingerprint"] = hashlib.sha256(
        json.dumps(final_model, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return output, models, final_model


def _metric_vector(
    queries: Mapping[str, Mapping[str, Any]],
    relevance: Mapping[str, Mapping[str, int]],
    rankings: Mapping[str, Sequence[str]],
    latencies: Mapping[str, float],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    family_values: dict[str, list[float]] = defaultdict(list)
    recall_values: list[float] = []
    source_top1: list[float] = []
    source_ids: list[str] = []
    source_hits: dict[int, int] = {1: 0, 10: 0, 20: 0}
    exact: list[float] = []
    keyword: list[float] = []
    negative: list[float] = []
    by_category: dict[str, list[float]] = defaultdict(list)
    for query_id, query in queries.items():
        ranking = list(rankings[query_id])
        rel = relevance[query_id]
        ndcg = ndcg_at_10(ranking, rel)
        if ndcg is not None:
            family_values[query["topic_family_id"]].append(ndcg)
        recall = semantic_recall_at_20(ranking, rel)
        if recall is not None:
            recall_values.append(recall)
        grade = rel.get(ranking[0], 0) if ranking else 0
        if query.get("source_entity_ids"):
            source_top1.append(float(grade == 2))
            for source_id in dict.fromkeys(query["source_entity_ids"]):
                source_ids.append(source_id)
                for cutoff in source_hits:
                    if source_id in ranking[:cutoff]:
                        source_hits[cutoff] += 1
        category = query.get("category", "unknown")
        category_values = by_category[category]
        category_values.append(
            float(grade == 2) if category != "strict_negative" else float(grade < 1)
        )
        if category == "exact_sentence":
            exact.append(float(grade == 2))
        elif category == "keyword":
            keyword.append(float(grade == 2))
        elif category == "strict_negative":
            negative.append(float(grade < 1))
    per_category = {
        category: {"count": len(values), "rate": _mean(values)}
        for category, values in sorted(by_category.items())
    }
    source_metrics = {
        f"hit_at_{cutoff}": {
            "hits": source_hits[cutoff],
            "denominator": len(source_ids),
            "rate": (source_hits[cutoff] / len(source_ids)) if source_ids else 0.0,
        }
        for cutoff in (1, 10, 20)
    }
    metrics = {
        "query_count": len(queries),
        "macro_positive_ndcg_at_10": _mean([_mean(values) for values in family_values.values()]),
        "grade2_recall_at_20": _mean(recall_values),
        "same_specific_fact_grade2_top1": _mean(source_top1),
        "source_id_metrics": {
            "unit": "source_entity_id occurrence; each unique query/source pair is one denominator",
            **source_metrics,
        },
        "exact_safety": _mean(exact),
        "keyword_safety": _mean(keyword),
        "strict_negative_safety": _mean(negative),
        "shared_retrieval_latency_ms": {
            "mean": _mean(list(latencies.values())),
            "p50": _nearest_rank(list(latencies.values()), 0.50),
            "p95": _nearest_rank(list(latencies.values()), 0.95),
            "max": max(latencies.values()) if latencies else 0.0,
        },
    }
    return metrics, per_category


def _paired_ndcg_outcome(
    queries: Mapping[str, Mapping[str, Any]],
    relevance: Mapping[str, Mapping[str, int]],
    baseline: Mapping[str, Sequence[str]],
    contender: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    wins = losses = ties = 0
    source_wlt: dict[str, dict[str, int]] = {}
    for cutoff in (1, 10, 20):
        source_wins = source_losses = source_ties = 0
        for query_id in sorted(queries):
            source_ids = list(dict.fromkeys(queries[query_id].get("source_entity_ids", [])))
            if not source_ids:
                continue
            base_count = sum(source_id in baseline[query_id][:cutoff] for source_id in source_ids)
            contender_count = sum(
                source_id in contender[query_id][:cutoff] for source_id in source_ids
            )
            if contender_count > base_count:
                source_wins += 1
            elif contender_count < base_count:
                source_losses += 1
            else:
                source_ties += 1
        source_wlt[f"hit_at_{cutoff}"] = {
            "wins": source_wins,
            "losses": source_losses,
            "ties": source_ties,
        }
    for query_id in sorted(queries):
        base_value = ndcg_at_10(baseline[query_id], relevance[query_id]) or 0.0
        contender_value = ndcg_at_10(contender[query_id], relevance[query_id]) or 0.0
        if contender_value > base_value:
            wins += 1
        elif contender_value < base_value:
            losses += 1
        else:
            ties += 1
    return {
        "metric": "per_query_ndcg_at_10",
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "source_id_hit_outcomes": {
            "unit": "per query, comparing exact source-ID counts at each cutoff",
            **source_wlt,
        },
    }


def _comparison_deltas(metrics: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "macro_positive_ndcg_at_10",
        "grade2_recall_at_20",
        "same_specific_fact_grade2_top1",
        "exact_safety",
        "keyword_safety",
        "strict_negative_safety",
    )
    result = {field: float(metrics[field]) - float(baseline[field]) for field in fields}
    result["source_id_metrics"] = {
        key: float(metrics["source_id_metrics"][key]["rate"])
        - float(baseline["source_id_metrics"][key]["rate"])
        for key in ("hit_at_1", "hit_at_10", "hit_at_20")
    }
    return result


def _selection_recommendation(comparisons: Mapping[str, Any]) -> dict[str, Any]:
    gate_results: dict[str, dict[str, Any]] = {}
    for name, comparison in comparisons.items():
        if name == "deployed_hybrid_rrf":
            gate_results[name] = {"eligible": True, "reasons": ["baseline fallback"]}
            continue
        deltas = comparison["delta_vs_deployed_rrf"]
        reasons: list[str] = []
        gates = {
            "positive_ndcg_gain": deltas["macro_positive_ndcg_at_10"] > 0.0,
            "exact_non_inferiority": deltas["exact_safety"] >= 0.0,
            "keyword_non_inferiority": deltas["keyword_safety"] >= 0.0,
            "strict_negative_non_inferiority": deltas["strict_negative_safety"] >= 0.0,
            "grade2_recall_non_inferiority": deltas["grade2_recall_at_20"] >= 0.0,
            "same_specific_fact_non_inferiority": deltas["same_specific_fact_grade2_top1"] >= 0.0,
            "source_hit_at_1_non_inferiority": deltas["source_id_metrics"]["hit_at_1"] >= 0.0,
            "source_hit_at_10_non_inferiority": deltas["source_id_metrics"]["hit_at_10"] >= 0.0,
            "source_hit_at_20_non_inferiority": deltas["source_id_metrics"]["hit_at_20"] >= 0.0,
            "zero_channel_failures": comparison["metrics"].get("channel_failures", 0) == 0,
        }
        reasons.extend(key for key, passed in gates.items() if not passed)
        gate_results[name] = {
            "eligible": not reasons,
            "gates": gates,
            "failed_gates": reasons,
        }
    eligible = [name for name, result in gate_results.items() if result["eligible"]]
    selected = sorted(
        eligible,
        key=lambda name: (
            -float(comparisons[name]["metrics"]["macro_positive_ndcg_at_10"]),
            name,
        ),
    )[0]
    return {
        "selected_arm": selected,
        "eligible_arms": eligible,
        "gate_results": gate_results,
        "selection_metric": "macro_positive_ndcg_at_10, after strict safety/source/failure gates",
        "promotion_valid": False,
        "next_step": "pre-register this gate policy and rerun on fresh development evidence before any blind or production decision",
    }


def evaluate_development(inputs: Mapping[str, Any]) -> dict[str, Any]:
    queries = inputs["queries"]
    relevance = inputs["relevance"]
    query_ids = sorted(queries)
    baseline: dict[str, list[str]] = {}
    fusion_rankings: dict[str, dict[str, list[str]]] = {
        f"score_fusion_fts_{fts:g}_dense_{dense:g}": {} for fts, dense in SCORE_FUSION_GRID
    }
    rerank_rankings: dict[str, dict[str, list[str]]] = {
        f"score_rerank_rrf_pool_fts_{fts:g}_dense_{dense:g}": {} for fts, dense in SCORE_FUSION_GRID
    }
    features_by_query: dict[str, dict[str, dict[str, float]]] = {}
    latencies: dict[str, float] = {}
    exact_title_diagnostics: dict[str, dict[str, Any]] = {}
    for query_id in query_ids:
        exact_matches = [
            candidate_id
            for candidate_id, title in inputs["corpus_titles"].items()
            if title == queries[query_id]["query"]
        ]
        if len(exact_matches) == 1 and exact_matches[0] not in relevance[query_id]:
            raise DevelopmentFusionError(
                f"unique exact-title candidate {exact_matches[0]} lacks judged relevance for {query_id}"
            )
        union, lexical_ranks, dense_ranks, lexical_scores, dense_scores, latency = _channel_maps(
            inputs["lexical"][query_id], inputs["dense"][query_id]
        )
        features = _features(union, lexical_ranks, dense_ranks, lexical_scores, dense_scores)
        features_by_query[query_id] = features
        latencies[query_id] = latency
        # Keep the deployed RRF membership separate from final output parity.
        # A unique byte-exact title may live outside the retrieval union and
        # is injected only after each arm has scored its valid candidate pool.
        rrf_pool = _rrf_ranking(union, lexical_ranks, dense_ranks)
        base, parity = _exact_title_parity(
            rrf_pool, queries[query_id]["query"], inputs["corpus_titles"]
        )
        baseline[query_id] = base
        exact_title_diagnostics[query_id] = parity
        for (fts_weight, dense_weight), name in zip(SCORE_FUSION_GRID, fusion_rankings):
            ranked = _score_fusion_ranking(features, fts_weight, dense_weight)
            fusion_rankings[name][query_id], _ = _exact_title_parity(
                ranked, queries[query_id]["query"], inputs["corpus_titles"]
            )
            rerank_name = f"score_rerank_rrf_pool_fts_{fts_weight:g}_dense_{dense_weight:g}"
            reranked = _score_fusion_ranking(
                features,
                fts_weight,
                dense_weight,
                candidate_ids=rrf_pool,
            )
            rerank_rankings[rerank_name][query_id], _ = _exact_title_parity(
                reranked, queries[query_id]["query"], inputs["corpus_titles"]
            )
    pairwise_rankings, pairwise_models, pairwise_final_model = _pairwise_cv_rankings(
        query_ids,
        {query_id: queries[query_id]["topic_family_id"] for query_id in query_ids},
        features_by_query,
        relevance,
    )
    pairwise_rankings = {
        query_id: _exact_title_parity(ranked, queries[query_id]["query"], inputs["corpus_titles"])[
            0
        ]
        for query_id, ranked in pairwise_rankings.items()
    }

    arms: dict[str, dict[str, Any]] = {"deployed_hybrid_rrf": {"rankings": baseline}}
    arms.update({name: {"rankings": rankings} for name, rankings in fusion_rankings.items()})
    arms.update({name: {"rankings": rankings} for name, rankings in rerank_rankings.items()})
    arms["pairwise_linear_grouped_family_cv"] = {"rankings": pairwise_rankings}
    comparisons: dict[str, Any] = {}
    for name, arm in arms.items():
        metrics, per_category = _metric_vector(queries, relevance, arm["rankings"], latencies)
        metrics["channel_failures"] = 0
        comparisons[name] = {"metrics": metrics, "by_category": per_category}
    baseline_metrics = comparisons["deployed_hybrid_rrf"]["metrics"]
    for name, comparison in comparisons.items():
        comparison["delta_vs_deployed_rrf"] = _comparison_deltas(
            comparison["metrics"], baseline_metrics
        )
        comparison["paired_vs_deployed_rrf"] = _paired_ndcg_outcome(
            queries,
            relevance,
            baseline,
            arms[name]["rankings"],
        )
    baseline_recall = comparisons["deployed_hybrid_rrf"]["metrics"]["grade2_recall_at_20"]
    baseline_source_hit20 = comparisons["deployed_hybrid_rrf"]["metrics"]["source_id_metrics"][
        "hit_at_20"
    ]
    for name in rerank_rankings:
        metrics = comparisons[name]["metrics"]
        if metrics["grade2_recall_at_20"] != baseline_recall:
            raise DevelopmentFusionError(f"{name} changed fixed-pool grade2 recall@20")
        if metrics["source_id_metrics"]["hit_at_20"] != baseline_source_hit20:
            raise DevelopmentFusionError(f"{name} changed fixed-pool source-ID hit@20")
    selection_recommendation = _selection_recommendation(comparisons)

    config = {
        "baseline": {
            "name": "deployed_hybrid_rrf",
            "channels": ["lexical:bm25", "dense:BAAI/bge-small-en-v1.5:entity"],
            "lexical_cell_channel": "bm25_raw_production",
            "lexical_policy": "raw_production",
            "dense_model_revision": BGE_SMALL_REVISION,
            "channel_top_n": TOP_N,
            "candidate_union_order": "lexical top20 order, then unseen dense top20 order",
            "rrf_k": RRF_K,
            "final_top_n": TOP_N,
            "latency_evidence": "summed sequential lexical then dense channel latency; fusion/LTR compute overhead is unmeasured",
        },
        "score_aware_fusion": {
            "grid": [
                {"bm25_weight": fts, "dense_weight": dense} for fts, dense in SCORE_FUSION_GRID
            ],
            "normalization": "per-query min-max over the lexical+dense candidate union",
            "bm25_transform": "higher_is_better = -raw_bm25_score; SQLite BM25 raw score is lower-is-better",
            "dense_transform": "higher_is_better = frozen bundle score",
            "missing_channel": "floor = observed_min - max(1, observed_range)*1e-6 before min-max",
            "tie_break": "descending fused score, then frozen candidate union order",
            "model": "none; fixed weights only",
        },
        "score_rerank_rrf_pool": {
            "grid": [
                {"bm25_weight": fts, "dense_weight": dense} for fts, dense in SCORE_FUSION_GRID
            ],
            "candidate_membership": "immutable deployed_hybrid_rrf top20 after identical exact-title parity",
            "normalization": "per-query min-max over the immutable RRF top20 only",
            "transform_and_tie_break": "identical to score_aware_fusion",
            "invariants": [
                "grade2_recall_at_20 equals deployed_hybrid_rrf",
                "source-ID hit@20 equals deployed_hybrid_rrf",
            ],
        },
        "pairwise_linear": {
            "feature_names": list(PAIRWISE_FEATURES),
            "feature_source": "same transformed per-query normalized channel evidence as score fusion",
            "allowlist_validation": "ranking_architecture.validate_feature_schema",
            "family_split": "grouped_family_folds(sorted unique family IDs, modulo assignment)",
            "fold_count": PAIRWISE_FOLD_COUNT,
            "regularization": PAIRWISE_REGULARIZATION,
            "learning_rate": PAIRWISE_LEARNING_RATE,
            "epochs": PAIRWISE_EPOCHS,
            "seed": DETERMINISTIC_SEED,
            "pair_rule": "final_grade higher is preferred; equal grades omitted",
            "tie_break": "descending linear score, then frozen candidate union order",
            "model": "deterministic regularized pairwise logistic ranker",
            "heldout_metrics_only": "pairwise CV metrics use held-out-family predictions; no resubstitution performance is reported",
            "final_model": "after CV, train one frozen all-development model for future runtime/blind use; not used to report CV metrics",
        },
        "evidence_tier": {
            "label": "exploratory_development_screening",
            "preregistered": False,
            "promotion_valid": False,
            "reason": "This reuses an already-observed historical devrun2 and is not fresh preregistered evidence.",
        },
        "latency_evidence": {
            "channel_latency": "summed sequential lexical then dense retrieval latency retained",
            "fusion_compute_overhead": "unmeasured",
            "promotion_valid": False,
        },
        "selection_gates": {
            "accuracy": "strictly positive macro NDCG@10 delta versus deployed RRF",
            "gate_d_accuracy": "grade2_recall_at_20 and same_specific_fact_grade2_top1 non-inferior to deployed RRF",
            "exact_keyword_negative": "non-inferior to deployed RRF (delta >= 0.0 each)",
            "source_ids": "hit@1, hit@10, and hit@20 non-inferior to deployed RRF",
            "channel_failures": "zero lexical/dense channel failures",
            "tie_break": "highest macro NDCG@10, then lexicographically smallest arm name",
            "fallback": "deployed_hybrid_rrf when no contender passes every gate",
        },
        "exact_title_parity": {
            "applied_to": "baseline and every contender identically",
            "rule": "unique byte-identical title match across the full eligible frozen corpus returns that singleton, including when absent from the channel union; collisions/no-match preserve ranking",
            "scope": "full frozen corpus identity using signed representation manifest and verified corpus export",
        },
    }
    code_fingerprint = _sha256_bytes(Path(__file__).resolve())
    artifact = {
        "schema_version": 1,
        "kind": "DevelopmentFusionEvaluation",
        "run_id": RUN_ID,
        "development_only": True,
        "generated_by": str(Path(__file__).resolve()),
        "input_fingerprints": inputs["fingerprints"],
        "code_sha256": code_fingerprint,
        "configuration": config,
        "comparisons": comparisons,
        "pairwise_models": pairwise_models,
        "pairwise_final_model": pairwise_final_model,
        "selection_recommendation": selection_recommendation,
        "exact_title_diagnostics": exact_title_diagnostics,
        "query_rankings": {
            name: {query_id: list(ranking) for query_id, ranking in arm["rankings"].items()}
            for name, arm in arms.items()
        },
    }
    artifact["artifact_fingerprint"] = hashlib.sha256(
        json.dumps(artifact, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return artifact


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries-dev", type=Path, required=True)
    parser.add_argument("--judging-matrix", type=Path, required=True)
    parser.add_argument("--merged-dev-labels", type=Path, required=True)
    parser.add_argument(
        "--judging-addendum-matrix",
        type=Path,
        help="Optional signed raw-production development judging addendum matrix.",
    )
    parser.add_argument(
        "--merged-addendum-labels",
        type=Path,
        help="Optional complete three-judge/adjudication addendum labels.",
    )
    parser.add_argument(
        "--addendum-raw-labels",
        type=Path,
        nargs=3,
        help="Exactly three signed raw addendum label artifacts, in raw_a/raw_b/raw_c order.",
    )
    parser.add_argument("--lexical-bundle", type=Path, required=True)
    parser.add_argument("--dense-bundle", type=Path, required=True)
    parser.add_argument("--bakeoff-spec", type=Path, required=True)
    parser.add_argument("--corpus-manifest", type=Path, required=True)
    parser.add_argument("--corpus-export", type=Path, required=True)
    parser.add_argument(
        "--parent-binding",
        type=Path,
        help="Signed compatibility proof required for a legacy matrix without query binding.",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        artifact = evaluate_development(
            load_inputs(
                args.queries_dev,
                args.judging_matrix,
                args.merged_dev_labels,
                args.lexical_bundle,
                args.dense_bundle,
                args.bakeoff_spec,
                args.corpus_manifest,
                args.corpus_export,
                args.judging_addendum_matrix,
                args.merged_addendum_labels,
                args.parent_binding,
                args.addendum_raw_labels,
            )
        )
        _atomic_write(args.out, artifact)
        print(
            json.dumps({"artifact": str(args.out), "fingerprint": artifact["artifact_fingerprint"]})
        )
        return 0
    except (DevelopmentFusionError, ValueError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
