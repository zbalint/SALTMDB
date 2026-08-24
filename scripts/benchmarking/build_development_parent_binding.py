"""Create a signed compatibility proof for the historical devrun2 judging parent."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bakeoff_state import sign_artifact, validate_bakeoff_spec, validate_signed_artifact  # noqa: E402
from build_evaluation_queries import artifact_fingerprint, load_manifest  # noqa: E402
from judge_pool import validate_judge_packet, verify_artifact_fingerprint  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_query_manifest(path: Path, *, corpus_root_hash: str) -> tuple[dict, bytes]:
    """Read, validate, and retain one immutable query-manifest byte snapshot."""
    payload = path.read_bytes()
    return (
        load_manifest(
            path,
            raw_bytes=payload,
            expected_split="dev",
            expected_corpus_fingerprint=corpus_root_hash,
            require_provenance=True,
        ),
        payload,
    )


def build_parent_binding(  # noqa: C901, PLR0912, PLR0915
    # The custody proof intentionally validates several independent signed
    # records in one fail-closed transaction.
    *,
    spec_path: Path,
    queries_path: Path,
    matrix_path: Path,
    labels_path: Path,
    mapping_paths: list[Path],
    packet_paths: list[Path],
    ingested_label_paths: list[Path],
) -> dict[str, Any]:
    spec = validate_bakeoff_spec(json.loads(spec_path.read_text()))
    query_manifest, query_payload = _load_query_manifest(
        queries_path, corpus_root_hash=spec["corpus_snapshot_hash"]
    )
    matrix = validate_signed_artifact(json.loads(matrix_path.read_text()), kind="JudgingMatrix")
    labels = json.loads(labels_path.read_text())
    stored = labels.get("fingerprint")
    unsigned = dict(labels)
    unsigned.pop("fingerprint", None)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError("historical merged labels fingerprint mismatch")
    queries = query_manifest["queries"]
    query_ids = {query["id"] for query in queries}
    pools = matrix.get("pools")
    if matrix.get("spec_fingerprint") != spec["artifact_fingerprint"]:
        raise ValueError("historical matrix spec binding mismatch")
    if not isinstance(pools, dict) or set(pools) != query_ids:
        raise ValueError("historical matrix does not cover exact dev queries")
    expected_pairs = {
        (query_id, candidate_id)
        for query_id, candidates in pools.items()
        for candidate_id in candidates
    }
    actual_pairs = {
        (label.get("query_id"), label.get("candidate_id")) for label in labels.get("labels", [])
    }
    if actual_pairs != expected_pairs:
        raise ValueError("historical labels do not exactly cover historical matrix pools")
    if not (len(mapping_paths) == len(packet_paths) == len(ingested_label_paths) == 3):
        raise ValueError(
            "historical proof requires exactly three mappings, packets, and ingested labels"
        )
    expected_roles = ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
    canonical_pool_fp = artifact_fingerprint(pools)
    mapping_records = []
    packet_records = []
    raw_records = []
    raw_by_role = {}
    for role, mapping_path, packet_path, raw_path in zip(
        expected_roles, mapping_paths, packet_paths, ingested_label_paths, strict=True
    ):
        mapping = json.loads(mapping_path.read_text())
        verify_artifact_fingerprint(mapping)
        if mapping.get("judge") != role or mapping.get("split") != "dev":
            raise ValueError(f"historical mapping role/split mismatch for {role}")
        if (
            mapping.get("queries_fingerprint") != artifact_fingerprint(queries)
            or mapping.get("matrix_pools_fingerprint") != canonical_pool_fp
        ):
            raise ValueError(f"historical mapping binding mismatch for {role}")
        if (
            mapping_records
            and mapping.get("coverage_fingerprint") != mapping_records[0]["coverage_fingerprint"]
        ):
            raise ValueError(f"historical mapping coverage mismatch for {role}")
        packet = json.loads(packet_path.read_text())
        validate_judge_packet(packet)
        if packet.get("judge") != role or packet.get("fingerprint") != mapping.get(
            "packet_fingerprint"
        ):
            raise ValueError(f"historical packet binding mismatch for {role}")
        raw = json.loads(raw_path.read_text())
        verify_artifact_fingerprint(raw)
        if (
            raw.get("judge") != role
            or raw.get("mapping_fingerprint") != mapping.get("fingerprint")
            or raw.get("coverage_fingerprint") != mapping.get("coverage_fingerprint")
            or raw.get("label_count") != len(raw.get("labels", []))
        ):
            raise ValueError(f"historical ingested-label binding mismatch for {role}")
        raw_pairs = {(x.get("query_id"), x.get("candidate_id")) for x in raw.get("labels", [])}
        if raw_pairs != expected_pairs or any(
            x.get("grade") not in (0, 1, 2) for x in raw["labels"]
        ):
            raise ValueError(f"historical ingested labels are incomplete for {role}")
        mapping_records.append(
            {
                "judge": role,
                "file_sha256": _sha(mapping_path),
                "fingerprint": mapping["fingerprint"],
                "queries_fingerprint": mapping["queries_fingerprint"],
                "matrix_pools_fingerprint": mapping["matrix_pools_fingerprint"],
                "packet_fingerprint": mapping["packet_fingerprint"],
                "coverage_fingerprint": mapping["coverage_fingerprint"],
            }
        )
        packet_records.append(
            {"judge": role, "file_sha256": _sha(packet_path), "fingerprint": packet["fingerprint"]}
        )
        raw_records.append(
            {
                "judge": role,
                "file_sha256": _sha(raw_path),
                "fingerprint": raw["fingerprint"],
                "mapping_fingerprint": raw["mapping_fingerprint"],
                "coverage_fingerprint": raw["coverage_fingerprint"],
            }
        )
        raw_by_role[role] = raw
    canonical_raw_fp = artifact_fingerprint([raw_by_role[role] for role in expected_roles])
    if labels.get("raw_labels_fingerprint") != canonical_raw_fp:
        raise ValueError("historical merged labels raw_labels_fingerprint is not canonical")
    binding = {
        "parent_matrix_fingerprint": matrix["artifact_fingerprint"],
        "spec_fingerprint": spec["artifact_fingerprint"],
        "query_manifest_fingerprint": query_manifest["manifest_fingerprint"],
        "query_manifest_file_sha256": hashlib.sha256(query_payload).hexdigest(),
        "query_split": "dev",
        "historical_labels_fingerprint": stored,
        "historical_labels_file_sha256": _sha(labels_path),
        "corpus_root_hash": spec["corpus_snapshot_hash"],
        "label_pair_count": len(expected_pairs),
        "canonical_queries_fingerprint": artifact_fingerprint(queries),
        "canonical_matrix_pools_fingerprint": canonical_pool_fp,
        "canonical_raw_labels_fingerprint": canonical_raw_fp,
        "mapping_records": mapping_records,
        "packet_records": packet_records,
        "ingested_label_records": raw_records,
        "judge_roles": list(expected_roles),
    }
    return sign_artifact(
        "DevelopmentParentBinding",
        {
            "development_only": True,
            "evidence_tier": "legacy_parent_compatibility_proof",
            "binding": binding,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--queries-dev", type=Path, required=True)
    parser.add_argument("--judging-matrix", type=Path, required=True)
    parser.add_argument("--historical-labels", type=Path, required=True)
    parser.add_argument("--mappings", type=Path, nargs=3, required=True)
    parser.add_argument("--packets", type=Path, nargs=3, required=True)
    parser.add_argument("--ingested-labels", type=Path, nargs=3, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_parent_binding(
        spec_path=args.spec,
        queries_path=args.queries_dev,
        matrix_path=args.judging_matrix,
        labels_path=args.historical_labels,
        mapping_paths=list(args.mappings),
        packet_paths=list(args.packets),
        ingested_label_paths=list(args.ingested_labels),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
