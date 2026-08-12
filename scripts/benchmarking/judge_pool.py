"""Build provenance-free evaluation judging packets and validate returned labels.

The public artifacts deliberately contain no ranking/configuration or ground-truth metadata.
The private mapping is retained locally for merge/arbitration only.
"""

import argparse
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bakeoff_state import authorize_blind_file  # noqa: E402
from analyze_evaluation_matrix import validate_frozen_shortlist  # noqa: E402

VALID_GRADES = {0, 1, 2}
PACKET_SCHEMA_VERSION = 2
EXCERPT_ALGORITHM_VERSION = "query-centered-redacted-v1"
EXCERPT_MAX_CHARS = 600
# The baseline protocol is deliberately Luna-only.  These are stable domain role IDs rather
# than provider/model names: a packet may be executed by any approved Luna worker, while the
# role identity remains auditable and cannot silently regress to the legacy judge set.
JUDGES = ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
ARBITRATOR = "agent_eval_adjudicator"
# Bump only when the rubric/packet schema or judge-role contract changes.  The value is included
# in every Stage-1 provenance envelope so labels from an older rubric cannot be merged silently.
JUDGE_VERSION = "stage1-grades-0-1-2-gains-0-1-3-v1"


def judging_protocol_metadata() -> dict:
    """Return the signed three-judge/adjudication contract embedded in each packet."""
    return {
        "judge_count": len(JUDGES),
        "judges": list(JUDGES),
        "adjudicator": ARBITRATOR,
        "required_grades_per_candidate": len(JUDGES),
        "aggregation": "median_of_three",
        # Keep ground-truth terminology out of the public packet.  The private merge stage
        # still records its precise ``ground_truth_conflict`` reason.
        "escalation": ["full_disagreement", "known_source_conflict"],
        "adjudication": "fourth_pass_only_for_escalated_items",
    }


def validate_judging_protocol_metadata(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("missing judging protocol metadata")
    expected = judging_protocol_metadata()
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"judging protocol metadata mismatch for {key}")


def _canonical_source_text(item: dict) -> str:
    """Build the only source representation allowed to enter a public excerpt."""
    title = item.get("title") or ""
    content = item.get("full_content") or item.get("source_text") or item.get("snippet") or ""
    if not isinstance(title, str) or not isinstance(content, str):
        raise ValueError("candidate title/content must be strings")
    return "\n\n".join(part.strip() for part in (title, content) if part.strip())


_ID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{24,}\b"
)
_PRIVATE_FIELD_RE = re.compile(
    r"(?i)\b(?:source[_ -]?entity[_ -]?ids?|config(?:uration)?|ground[_ -]?truth|rank(?:ing)?)\s*[:=]\s*[^\s,;]+"
)
_SPACE_RE = re.compile(r"\s+")


def redact_excerpt_text(value: str, *, extra_tokens: tuple[str, ...] = ()) -> str:
    """Redact identifiers and evaluator bookkeeping from judge-visible text."""
    value = _ID_RE.sub("[redacted-id]", value)
    value = _PRIVATE_FIELD_RE.sub("[redacted-metadata]", value)
    for token in sorted(
        (item for item in extra_tokens if isinstance(item, str) and item),
        key=len,
        reverse=True,
    ):
        value = re.sub(rf"(?i)(?<![\w-]){re.escape(token)}(?![\w-])", "[redacted-id]", value)
    return _SPACE_RE.sub(" ", value).strip()


def _query_tokens(query: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[\wÀ-ÖØ-öø-ÿ]{2,}", query, flags=re.UNICODE)}


def build_query_centered_excerpt(
    query: str,
    source_text: str,
    *,
    max_chars: int = EXCERPT_MAX_CHARS,
    redact_tokens: tuple[str, ...] = (),
) -> str:
    """Produce a deterministic, redacted excerpt centered on query-token matches.

    The source string is never sent directly to a judge when it exceeds the cap.  Sentence
    scoring uses only query-token overlap and original order, making the excerpt reproducible
    without an embedding/model call.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if not isinstance(source_text, str):
        raise ValueError("source_text must be a string")
    if max_chars < 80:
        raise ValueError("max_chars must be at least 80")
    redacted = redact_excerpt_text(source_text, extra_tokens=redact_tokens)
    if not redacted:
        return "[no source excerpt]"
    if len(redacted) <= max_chars:
        return redacted
    query_tokens = _query_tokens(query)
    sentences = list(re.finditer(r"[^.!?]+(?:[.!?]+|$)", redacted))
    scored: list[tuple[int, int, int, int]] = []
    for index, match in enumerate(sentences):
        sentence_tokens = set(re.findall(r"[\wÀ-ÖØ-öø-ÿ]{2,}", match.group(), flags=re.UNICODE))
        overlap = len(query_tokens & {token.casefold() for token in sentence_tokens})
        scored.append((-overlap, match.start(), match.end(), index))
    if scored:
        _, start, end, _ = min(scored)
        center = (start + end) // 2
    else:
        center = 0
    half = max_chars // 2
    start = max(0, min(center - half, len(redacted) - max_chars))
    excerpt = redacted[start : start + max_chars].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if start + max_chars < len(redacted):
        excerpt += "…"
    return excerpt


def source_sha256(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


def excerpt_sha256(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def judge_version_fingerprint() -> str:
    return artifact_fingerprint(
        {
            "version": JUDGE_VERSION,
            "judges": JUDGES,
            "arbitrator": ARBITRATOR,
            "grades": [0, 1, 2],
            "ndcg_gains": [0, 1, 3],
        }
    )


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


def _validate_packet_header(packet: dict) -> None:
    if packet.get("packet_schema_version") != PACKET_SCHEMA_VERSION:
        raise ValueError("unsupported judge packet schema")
    if packet.get("excerpt_algorithm_version") != EXCERPT_ALGORITHM_VERSION:
        raise ValueError("judge packet uses a stale excerpt algorithm")
    validate_judging_protocol_metadata(packet.get("judging_protocol"))
    if not isinstance(packet.get("tasks"), list) or not packet["tasks"]:
        raise ValueError("judge packet has no tasks")


def _validate_packet_candidate(candidate: object) -> None:
    if not isinstance(candidate, dict):
        raise ValueError("judge packet candidate is malformed")
    private_keys = ("entity_id", "source_entity_id", "config", "rank", "score", "ground_truth")
    if any(key in candidate for key in private_keys):
        raise ValueError("judge packet candidate exposes private metadata")
    excerpt = candidate.get("excerpt")
    if not isinstance(excerpt, str):
        raise ValueError("judge packet candidate lacks excerpt")
    if candidate.get("excerpt_sha256") != excerpt_sha256(excerpt):
        raise ValueError("judge packet excerpt fingerprint mismatch")
    if not isinstance(candidate.get("source_sha256"), str):
        raise ValueError("judge packet candidate lacks source fingerprint")


def _validate_packet_task(task: object) -> None:
    if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
        raise ValueError("judge packet task is malformed")
    private_keys = ("query_id", "source_entity_ids", "config", "ranking")
    if any(key in task for key in private_keys):
        raise ValueError("judge packet exposes private evaluator metadata")
    for candidate in task.get("candidates", []):
        _validate_packet_candidate(candidate)


def validate_judge_packet(packet: object) -> None:
    """Reject a mutable/tampered packet or one that exposes evaluator bookkeeping."""
    verify_artifact_fingerprint(packet)
    if not isinstance(packet, dict):
        raise ValueError("unsupported judge packet schema")
    _validate_packet_header(packet)
    for task in packet["tasks"]:
        _validate_packet_task(task)


def require_frozen_dev_shortlist(path: Path | None) -> None:
    """Refuse blind packets until the signed development shortlist is available."""
    if path is None or not path.exists():
        raise RuntimeError("blind packets require a signed --dev-shortlist")
    validate_frozen_shortlist(json.loads(path.read_text()))


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
        rendered_candidates = []
        candidate_hashes = {}
        for candidate_id, item in candidates.items():
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError(f"query {query_id!r} has an invalid candidate ID")
            if not isinstance(item, dict):
                raise ValueError(f"query {query_id!r} candidate is not an object")
            source_text = _canonical_source_text(item)
            excerpt = build_query_centered_excerpt(
                query["query"], source_text, redact_tokens=(candidate_id,)
            )
            rendered_item = {
                "excerpt": excerpt,
                "excerpt_sha256": excerpt_sha256(excerpt),
                "source_sha256": source_sha256(source_text),
                "excerpt_algorithm_version": EXCERPT_ALGORITHM_VERSION,
            }
            rendered_candidates.append((candidate_id, rendered_item))
            candidate_hashes[candidate_id] = rendered_item["source_sha256"]
        if len({candidate_id for candidate_id, _ in rendered_candidates}) != len(
            rendered_candidates
        ):
            raise ValueError(f"query {query_id!r} has duplicate candidate IDs")
        random.Random(_seed(base_seed, split, judge, query_id)).shuffle(rendered_candidates)
        task_id = f"task-{task_index:04d}"
        rendered = [
            {"candidate_id": f"candidate-{candidate_index:03d}", **candidate}
            for candidate_index, (_, candidate) in enumerate(rendered_candidates, start=1)
        ]
        tasks.append(
            {
                "task_id": task_id,
                "query": query["query"],
                "query_sha256": hashlib.sha256(query["query"].encode("utf-8")).hexdigest(),
                "candidates": rendered,
            }
        )
        mapping[task_id] = {
            "query_id": query_id,
            "source_entity_ids": query.get("source_entity_ids", []),
            "candidate_ids": [c["candidate_id"] for c in rendered],
            "candidate_entity_ids": {
                rendered[index]["candidate_id"]: candidate_id
                for index, (candidate_id, _) in enumerate(rendered_candidates)
            },
            "candidate_source_sha256": candidate_hashes,
        }
    packet = {
        "schema_version": 1,
        "packet_schema_version": PACKET_SCHEMA_VERSION,
        "judge": judge,
        "judge_version": JUDGE_VERSION,
        "excerpt_algorithm_version": EXCERPT_ALGORITHM_VERSION,
        "immutable": True,
        "rank_and_config_blind": True,
        "judging_protocol": judging_protocol_metadata(),
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
        "judge_version": JUDGE_VERSION,
        "judge_version_fingerprint": judge_version_fingerprint(),
        "random_seed": base_seed,
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
    validate_judge_packet(packet)
    return packet, private


def validate_labels(response: dict, private_mapping: dict, judge: str) -> list[dict]:
    """Validate a complete judge response and normalize it for merge_judgments."""
    verify_artifact_fingerprint(private_mapping)
    if private_mapping.get("split") not in {"dev", "blind"}:
        raise ValueError("private mapping has invalid split")
    if private_mapping.get("judge_version") != JUDGE_VERSION:
        raise ValueError("private mapping uses a stale judge rubric version")
    if private_mapping.get("judge_version_fingerprint") != judge_version_fingerprint():
        raise ValueError("private mapping judge-version fingerprint mismatch")
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


def agreement_report(labels: list[dict], *, judges: tuple[str, ...] = JUDGES) -> dict:
    """Return lightweight three-judge agreement metadata for an ingested label set.

    The full adjudication merge remains in ``merge_judgments.py``; this hook is intentionally
    usable at packet-ingest time, before an arbitration response exists.
    """
    if len(judges) != 3 or set(judges) != set(JUDGES):
        raise ValueError("agreement report requires the configured three judges")
    by_item: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for label in labels:
        if not isinstance(label, dict) or label.get("judge") not in judges:
            raise ValueError("agreement labels contain an unknown judge")
        grade = label.get("grade")
        if grade not in VALID_GRADES:
            raise ValueError("agreement labels contain an invalid grade")
        query_id, candidate_id, judge = (
            label.get("query_id"),
            label.get("candidate_id"),
            label.get("judge"),
        )
        if not all(isinstance(value, str) and value for value in (query_id, candidate_id, judge)):
            raise ValueError("agreement labels require query, candidate, and judge IDs")
        assert isinstance(query_id, str) and isinstance(candidate_id, str)
        assert isinstance(judge, str) and isinstance(grade, int)
        by_item[(query_id, candidate_id)][judge] = grade
    pairwise = {}
    for index, left in enumerate(judges):
        for right in judges[index + 1 :]:
            common = [grades for grades in by_item.values() if left in grades and right in grades]
            confusion = {f"{a},{b}": 0 for a in sorted(VALID_GRADES) for b in sorted(VALID_GRADES)}
            for grades in common:
                confusion[f"{grades[left]},{grades[right]}"] += 1
            exact = (
                sum(count for key, count in confusion.items() if key[0] == key[2]) / len(common)
                if common
                else float("nan")
            )
            pairwise[f"{left}:{right}"] = {
                "n": len(common),
                "exact_agreement_rate": exact,
                "confusion": confusion,
            }
    complete = sum(1 for grades in by_item.values() if set(grades) == set(judges))
    return {
        "protocol": judging_protocol_metadata(),
        "items": len(by_item),
        "complete_three_way_items": complete,
        "pairwise": pairwise,
        "adjudication": {
            "adjudicator": ARBITRATOR,
            "escalated_items": None,
            "status": "pending_merge",
        },
    }


build_agreement_report = agreement_report


def write_packets(
    queries_path: Path,
    matrix_path: Path,
    out_dir: Path,
    mapping_dir: Path,
    judge: str,
    split: str,
    base_seed: int = 0,
    authorized_query_bytes: bytes | None = None,
) -> None:
    if split == "blind" and authorized_query_bytes is None:
        raise RuntimeError("direct blind packet generation requires authorized query bytes")
    queries = json.loads(
        authorized_query_bytes.decode("utf-8")
        if authorized_query_bytes is not None
        else queries_path.read_text()
    )
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
        "judge_version": JUDGE_VERSION,
        "judge_version_fingerprint": judge_version_fingerprint(),
        "random_seed": mapping.get("random_seed"),
        "mapping_fingerprint": mapping.get("fingerprint"),
        "coverage_fingerprint": mapping.get("coverage_fingerprint"),
        "label_count": len(normalized),
        "labels": normalized,
        "judging_protocol": judging_protocol_metadata(),
        "agreement": agreement_report(normalized),
        "adjudication": {
            "adjudicator": ARBITRATOR,
            "status": "pending_merge",
        },
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
    packet.add_argument(
        "--dev-shortlist",
        type=Path,
        help="Signed development shortlist, required for blind packet generation.",
    )
    packet.add_argument("--blind-vault-dir", type=Path)
    packet.add_argument("--bakeoff-spec", type=Path)
    packet.add_argument("--development-winner", type=Path)
    packet.add_argument("--blind-unlock", type=Path)
    packet.add_argument("--blind-manifest-receipt", type=Path)
    packet.add_argument("--seed", type=int, default=0)
    ingest = actions.add_parser("ingest")
    ingest.add_argument("--response", type=Path, required=True)
    ingest.add_argument("--mapping", type=Path, required=True)
    ingest.add_argument("--out", type=Path, required=True)
    ingest.add_argument("--judge", choices=JUDGES, required=True)
    args = parser.parse_args()
    if args.action == "packets":
        authorized_query_bytes = None
        if args.split == "blind":
            require_frozen_dev_shortlist(args.dev_shortlist)
            authorization_paths = (
                args.blind_vault_dir,
                args.bakeoff_spec,
                args.development_winner,
                args.blind_unlock,
                args.blind_manifest_receipt,
            )
            if any(path is None for path in authorization_paths):
                raise RuntimeError("blind packets require vault and matching BlindUnlock paths")
            authorized_query_bytes = authorize_blind_file(
                args.queries,
                args.blind_vault_dir,
                args.bakeoff_spec,
                args.development_winner,
                args.blind_unlock,
                args.blind_manifest_receipt,
            )
        write_packets(
            args.queries,
            args.matrix,
            args.out_dir,
            args.mapping_dir,
            args.judge,
            args.split,
            args.seed,
            authorized_query_bytes,
        )
    else:
        ingest_labels(args.response, args.mapping, args.out, args.judge)


if __name__ == "__main__":
    main()
