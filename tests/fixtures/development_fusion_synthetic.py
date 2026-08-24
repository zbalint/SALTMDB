"""Small, deterministic Gate-D custody artifacts that do not depend on ignored outputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from bakeoff_state import sign_artifact
from judge_pool import judge_version_fingerprint


RAW_ROLES = (
    "agent_eval_judge_raw_a",
    "agent_eval_judge_raw_b",
    "agent_eval_judge_raw_c",
)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def build_lexical_snapshot_receipt(db_path: Path, *, corpus_root_hash: str) -> dict[str, Any]:
    """Build a signed receipt over a tiny test DB, matching the production fields."""
    return sign_artifact(
        "LexicalSnapshotReceipt",
        {
            "corpus_root_hash": corpus_root_hash,
            "db_path": str(db_path),
            "entity_count": 0,
            "relation_count": 0,
            "sentinel_timestamp": "2026-08-24T00:00:00+00:00",
            "owner_id": "synthetic-test",
            "db_sha256_informational": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        },
    )


def build_addendum_contract() -> dict[str, Any]:
    """Build a complete 172-query/241-pair addendum and all three raw roles."""
    query_ids = [f"q{index:03d}" for index in range(172)]
    parent_pools = {
        query_id: {f"old-{index:03d}": {"title": "Old", "full_content": "Old"}}
        for index, query_id in enumerate(query_ids)
    }
    parent = sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": "a" * 64,
            "query_manifest_fingerprint": "b" * 64,
            "query_count": 172,
            "pools": parent_pools,
        },
    )
    pools: dict[str, dict[str, dict[str, str]]] = {}
    for index, query_id in enumerate(query_ids):
        candidate_count = 2 if index < 69 else 1
        pools[query_id] = {
            f"new-{index:03d}-{candidate_index}": {
                "title": "New",
                "full_content": "New",
            }
            for candidate_index in range(candidate_count)
        }
    addendum = sign_artifact(
        "JudgingMatrix",
        {
            "spec_fingerprint": parent["spec_fingerprint"],
            "query_manifest_fingerprint": parent["query_manifest_fingerprint"],
            "query_count": 172,
            "missing_pair_count": 241,
            "pools": pools,
            "parent_judging_matrix_fingerprint": parent["artifact_fingerprint"],
            "query_manifest_file_sha256": "c" * 64,
            "query_split": "dev",
            "corpus_root_hash": "d" * 64,
            "corpus_manifest_fingerprint": "e" * 64,
            "corpus_export_file_sha256": "f" * 64,
            "raw_bundle_fingerprint": "1" * 64,
            "worklist_fingerprint": "2" * 64,
        },
    )
    binding = {
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
    }
    pairs = [
        (query_id, candidate_id)
        for query_id, candidates in pools.items()
        for candidate_id in candidates
    ]
    raw_artifacts = []
    for role in RAW_ROLES:
        raw = {
            "schema_version": 1,
            "kind": "RawJudgmentLabels",
            "judge": role,
            "judge_version": "synthetic-v1",
            "judge_version_fingerprint": judge_version_fingerprint(RAW_ROLES),
            "judge_role_set": "raw_addendum",
            "matrix_binding": binding,
            "label_count": len(pairs),
            "labels": [
                {"query_id": query_id, "candidate_id": candidate_id, "grade": 0}
                for query_id, candidate_id in pairs
            ],
        }
        raw["fingerprint"] = _fingerprint(raw)
        raw_artifacts.append(raw)
    merged = {
        "schema_version": 1,
        "kind": "DevelopmentJudgingAddendumLabels",
        "judges": list(RAW_ROLES),
        "adjudicator": "agent_eval_adjudicator",
        "judge_version": "synthetic-v1",
        "agreement": [
            {
                "judges": [left, right],
                "n": 241,
                "exact_agreement_rate": 1.0,
                "cohens_kappa": 1.0,
                "confusion": {"0:0": 241},
            }
            for index, left in enumerate(RAW_ROLES)
            for right in RAW_ROLES[index + 1 :]
        ],
        "escalation": {"count": 0, "total": 241, "rate": 0.0},
        "addendum_binding": binding,
        "labels": [
            {
                "query_id": query_id,
                "candidate_id": candidate_id,
                "raw_grades": dict.fromkeys(RAW_ROLES, 0),
                "median_grade": 0,
                "escalated": False,
                "escalation_reason": None,
                "arbitrated_grade": None,
                "final_grade": 0,
            }
            for query_id, candidate_id in pairs
        ],
    }
    merged["raw_labels_fingerprint"] = _fingerprint(raw_artifacts)
    merged["fingerprint"] = _fingerprint(merged)
    return {
        "parent": parent,
        "addendum": addendum,
        "merged": merged,
        "raw_artifacts": raw_artifacts,
        "query_ids": set(query_ids),
        "old_pairs": {(query_id, f"old-{index:03d}") for index, query_id in enumerate(query_ids)},
    }
