"""Build provenance-free evaluation judging packets and validate returned labels.

The public artifacts deliberately contain no ranking/configuration or ground-truth metadata.
The private mapping is retained locally for merge/arbitration only.
"""

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

VALID_GRADES = {0, 1, 2}
# The baseline protocol is deliberately Luna-only.  These are stable domain role IDs rather
# than provider/model names: a packet may be executed by any approved Luna worker, while the
# role identity remains auditable and cannot silently regress to the legacy judge set.
JUDGES = ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
ARBITRATOR = "agent_eval_adjudicator"


def artifact_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "fingerprint") -> None:
    """Reject an artifact whose signed content no longer matches its stored fingerprint."""
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


def _seed(base_seed: int, split: str, judge: str, query_id: str) -> int:
    value = f"{base_seed}:{split}:{judge}:{query_id}".encode()
    return int(hashlib.sha256(value).hexdigest()[:16], 16)


def build_judge_packets(
    queries: list[dict], matrix: dict, judge: str, split: str, base_seed: int = 0
) -> tuple[dict, dict]:
    """Return (external_packet, private_mapping).  The packet is safe to send to a judge."""
    if judge not in JUDGES:
        raise ValueError(f"unknown judge {judge!r}")
    if split not in {"dev", "blind"}:
        raise ValueError("split must be dev or blind")
    query_by_id = {q["id"]: q for q in queries}
    if len(query_by_id) != len(queries):
        raise ValueError("duplicate query IDs")
    pools = matrix.get("pools")
    if not isinstance(pools, dict) or set(pools) != set(query_by_id):
        raise ValueError("matrix pools must cover exactly the supplied queries")
    tasks, mapping = [], {}
    for task_index, (query_id, candidates) in enumerate(pools.items(), start=1):
        query = query_by_id[query_id]
        if query.get("split") is not None and query["split"] != split:
            raise ValueError("query split does not match packet split")
        if not isinstance(candidates, dict) or not candidates:
            raise ValueError(f"query {query_id!r} has no pooled candidates")
        rendered_candidates = [
            (candidate_id, {"title": item.get("title") or "", "snippet": item.get("snippet") or ""})
            for candidate_id, item in candidates.items()
        ]
        if len({candidate_id for candidate_id, _ in rendered_candidates}) != len(rendered_candidates):
            raise ValueError(f"query {query_id!r} has duplicate candidate IDs")
        random.Random(_seed(base_seed, split, judge, query_id)).shuffle(rendered_candidates)
        task_id = f"task-{task_index:04d}"
        rendered = [
            {"candidate_id": f"candidate-{candidate_index:03d}", **candidate}
            for candidate_index, (_, candidate) in enumerate(rendered_candidates, start=1)
        ]
        tasks.append({"task_id": task_id, "query": query["query"], "candidates": rendered})
        mapping[task_id] = {
            "query_id": query_id,
            "source_entity_ids": query.get("source_entity_ids", []),
            "candidate_ids": [c["candidate_id"] for c in rendered],
            "candidate_entity_ids": {
                rendered[index]["candidate_id"]: candidate_id
                for index, (candidate_id, _) in enumerate(rendered_candidates)
            },
        }
    packet = {
        "schema_version": 1,
        "judge": judge,
        "rubric": {
            "0": "Irrelevant or non-answer to the query.",
            "1": "Related context or partial relevance, but not a direct answer to the query.",
            "2": "Directly answers the query.",
        },
        "tasks": tasks,
    }
    private = {
        "schema_version": 1,
        "judge": judge,
        "split": split,
        "tasks": mapping,
        "queries_fingerprint": artifact_fingerprint(queries),
        "matrix_pools_fingerprint": artifact_fingerprint(pools),
    }
    packet["fingerprint"] = artifact_fingerprint(packet)
    private["packet_fingerprint"] = packet["fingerprint"]
    private["coverage_fingerprint"] = artifact_fingerprint(
        sorted((task_id, sorted(item["candidate_ids"])) for task_id, item in mapping.items())
    )
    private["fingerprint"] = artifact_fingerprint(private)
    return packet, private


def validate_labels(response: dict, private_mapping: dict, judge: str) -> list[dict]:
    """Validate a complete judge response and normalize it for merge_judgments."""
    verify_artifact_fingerprint(private_mapping)
    if private_mapping.get("split") not in {"dev", "blind"}:
        raise ValueError("private mapping has invalid split")
    if private_mapping.get("judge") not in JUDGES:
        raise ValueError("private mapping is not for a configured Luna judge")
    if "shard" in private_mapping or private_mapping.get("is_shard"):
        raise ValueError("sharded judge mappings are not accepted")
    if judge != private_mapping.get("judge"):
        raise ValueError("judge does not match private mapping")
    expected = private_mapping["tasks"]
    if not isinstance(expected, dict) or not expected:
        raise ValueError("private mapping has no complete task coverage")
    seen: set[tuple[str, str]] = set()
    normalized = []
    for label in response.get("labels", []):
        task_id, candidate_id, grade = (
            label.get("task_id"),
            label.get("candidate_id"),
            label.get("grade"),
        )
        if (
            task_id not in expected
            or candidate_id not in expected[task_id]["candidate_ids"]
            or grade not in VALID_GRADES
        ):
            raise ValueError("unknown task/candidate or invalid grade in judge response")
        key = (task_id, candidate_id)
        if key in seen:
            raise ValueError("duplicate judge label")
        seen.add(key)
        normalized.append(
            {
                "judge": judge,
                "query_id": expected[task_id]["query_id"],
                "candidate_id": expected[task_id]["candidate_entity_ids"][candidate_id],
                "grade": grade,
            }
        )
    required = {
        (task_id, cid) for task_id, item in expected.items() for cid in item["candidate_ids"]
    }
    if seen != required:
        raise ValueError("judge response is incomplete")
    return sorted(normalized, key=lambda item: (item["query_id"], item["candidate_id"]))


def write_packets(
    queries_path: Path,
    matrix_path: Path,
    out_dir: Path,
    mapping_dir: Path,
    judge: str,
    split: str,
    base_seed: int = 0,
) -> None:
    queries = json.loads(queries_path.read_text())
    if isinstance(queries, dict):
        queries = queries["queries"]
    packet, private = build_judge_packets(
        queries, json.loads(matrix_path.read_text()), judge, split, base_seed
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"judge_packet_{judge}.json").write_text(
        json.dumps(packet, indent=2, ensure_ascii=False)
    )
    (mapping_dir / f"judge_mapping_{judge}_{split}.json").write_text(
        json.dumps(private, indent=2, ensure_ascii=False)
    )


def ingest_labels(response_path: Path, mapping_path: Path, out_path: Path, judge: str) -> dict:
    """Validate one complete response and atomically retain the normalized raw-label artifact."""
    mapping = json.loads(mapping_path.read_text())
    normalized = validate_labels(json.loads(response_path.read_text()), mapping, judge)
    artifact = {
        "schema_version": 1,
        "judge": judge,
        "mapping_fingerprint": mapping.get("fingerprint"),
        "coverage_fingerprint": mapping.get("coverage_fingerprint"),
        "label_count": len(normalized),
        "labels": normalized,
    }
    artifact["fingerprint"] = artifact_fingerprint(artifact)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp = out_path.with_suffix(out_path.suffix + ".tmp")
    temp.write_text(json.dumps(artifact, indent=2))
    temp.replace(out_path)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    packet = actions.add_parser("packets")
    packet.add_argument("--queries", type=Path, required=True)
    packet.add_argument("--matrix", type=Path, required=True)
    packet.add_argument("--out-dir", type=Path, required=True)
    packet.add_argument(
        "--mapping-dir",
        type=Path,
        required=True,
        help="Private local directory for ground-truth and provenance mappings.",
    )
    packet.add_argument("--judge", choices=JUDGES, required=True)
    packet.add_argument("--split", choices=("dev", "blind"), required=True)
    packet.add_argument("--seed", type=int, default=0)
    ingest = actions.add_parser("ingest")
    ingest.add_argument("--response", type=Path, required=True)
    ingest.add_argument("--mapping", type=Path, required=True)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.add_argument("--judge", choices=JUDGES, required=True)
    args = parser.parse_args()
    if args.action == "packets":
        write_packets(
            args.queries,
            args.matrix,
            args.out_dir,
            args.mapping_dir,
            args.judge,
            args.split,
            args.seed,
        )
    else:
        ingest_labels(args.response, args.mapping, args.out, args.judge)


if __name__ == "__main__":
    main()
