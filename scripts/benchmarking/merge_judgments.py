"""Merges 3 judges' raw per-(query,candidate) grades into final labels, per plan §4/§0b items
11-12/17 (`scratch/plans/precision_first_search_evaluation.md`). Pure computation -- no external
provider/DB dependency -- so it's fully unit-testable against synthetic judge output now, before any
real judging batch is dispatched. `judge_pool.py` (prompt construction + response parsing for
each judge) feeds this module's `merge_query_judgments` its raw grades once real judging runs.

## Escalation rule (§0b item 12/17)
A (query, candidate) item is escalated to a 4th, non-blind Claude ARBITRATION pass (not a 4th
independent judgment, per Codex round-2's explicit distinction) iff either:
  (a) the 3 raw grades have zero pairwise agreement (all three of {0,1,2} present), OR
  (b) the candidate is one of the query's predeclared `source_entity_ids` (positive queries only)
      AND the median-of-3 grade is < 2 (a potential ground-truth conflict).
This module detects escalation triggers and computes the plain median; the actual arbitration
call (an LLM invocation) happens elsewhere -- `apply_arbitration_override` accepts the result
back in.
"""

import argparse
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

JUDGES = ("agent_eval_judge_a", "agent_eval_judge_b", "agent_eval_judge_c")
ARBITRATOR = "agent_eval_adjudicator"
JUDGE_VERSION = "stage1-grades-0-1-2-gains-0-1-3-v1"
VALID_GRADES = {0, 1, 2}


def artifact_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def verify_artifact_fingerprint(value: object, *, field: str = "fingerprint") -> None:
    if not isinstance(value, dict) or not isinstance(value.get(field), str):
        raise ValueError(f"artifact lacks {field}")
    unsigned = dict(value)
    stored = unsigned.pop(field)
    if stored != artifact_fingerprint(unsigned):
        raise ValueError(f"artifact {field} mismatch")


@dataclass
class RawJudgment:
    """One judge's grade for one (query, candidate) pooled item."""

    judge: str  # one of the three agent_eval_judge_* Luna roles
    query_id: str
    candidate_id: str
    grade: int  # 0, 1, or 2


@dataclass
class MergedJudgment:
    query_id: str
    candidate_id: str
    raw_grades: dict[str, int]  # judge -> grade, exactly 3 entries
    median_grade: int
    escalated: bool
    escalation_reason: str | None = None  # "full_disagreement" | "ground_truth_conflict" | both
    arbitrated_grade: int | None = None  # set later by apply_arbitration_override

    @property
    def final_grade(self) -> int:
        """The grade downstream metrics should use: the arbitrated grade if one was applied,
        else the plain median. §0b item 11: "the adjudicated grade replaces the plain median for
        that item [only]." """
        return self.arbitrated_grade if self.arbitrated_grade is not None else self.median_grade


def _median_int(grades: list[int]) -> int:
    """Median of exactly 3 integers is always one of the 3 values -- no float/interpolation case
    (plan §4: "ties in a 3-way median don't occur"). statistics.median on an odd-length list
    already returns the middle element directly, but this wrapper asserts the int-typed
    invariant explicitly rather than silently accepting statistics.median's float return type
    for even-length input (which should never happen here -- always exactly 3 judges)."""
    if len(grades) != 3:
        raise ValueError(f"expected exactly 3 grades for a plain median, got {len(grades)}")
    result = statistics.median(grades)
    return int(result)


def merge_query_judgments(
    raw_judgments: list[RawJudgment],
    source_entity_ids: list[str] | None = None,
) -> list[MergedJudgment]:
    """Groups raw_judgments (already filtered to ONE query_id, all candidates for that query) by
    candidate_id, computes the median + escalation trigger for each. source_entity_ids is that
    query's predeclared ground truth (empty/None for LLM-paraphrase-only positive queries and all
    negative queries) -- used only for the ground-truth-conflict escalation trigger."""
    if not raw_judgments:
        return []
    query_ids = {item.query_id for item in raw_judgments}
    if len(query_ids) != 1:
        raise ValueError("merge_query_judgments accepts one query at a time")
    by_candidate: dict[str, dict[str, int]] = {}
    for rj in raw_judgments:
        if rj.judge not in JUDGES or rj.grade not in VALID_GRADES:
            raise ValueError("unknown judge role or invalid grade")
        if rj.judge in by_candidate.setdefault(rj.candidate_id, {}):
            raise ValueError("duplicate judge judgment")
        by_candidate[rj.candidate_id][rj.judge] = rj.grade

    source_set = set(source_entity_ids or [])
    results = []
    for candidate_id, judge_grades in by_candidate.items():
        if set(judge_grades) != set(JUDGES):
            raise ValueError("candidate lacks complete three-way Luna coverage")
        grades = list(judge_grades.values())
        median = _median_int(grades)
        full_disagreement = len(set(grades)) == 3
        ground_truth_conflict = candidate_id in source_set and median < 2

        reasons = []
        if full_disagreement:
            reasons.append("full_disagreement")
        if ground_truth_conflict:
            reasons.append("ground_truth_conflict")

        results.append(
            MergedJudgment(
                query_id=raw_judgments[0].query_id if raw_judgments else "",
                candidate_id=candidate_id,
                raw_grades=dict(judge_grades),
                median_grade=median,
                escalated=bool(reasons),
                escalation_reason="+".join(reasons) if reasons else None,
            )
        )
    return results


def apply_arbitration_override(merged: MergedJudgment, arbitrated_grade: int) -> MergedJudgment:
    """Records a 4th-pass Claude arbitration result. Only valid on an already-escalated item
    (arbitration is triggered BY escalation, not applied speculatively)."""
    if not merged.escalated or arbitrated_grade not in VALID_GRADES:
        raise ValueError(
            f"attempted to apply arbitration to a non-escalated item "
            f"(query={merged.query_id}, candidate={merged.candidate_id})"
        )
    merged.arbitrated_grade = arbitrated_grade
    return merged


def _raw_from_artifacts(label_artifacts: list[dict]) -> list[RawJudgment]:  # noqa: C901
    """Accept exactly three complete, non-overlapping judge artifacts."""
    by_judge: dict[str, list[dict]] = {}
    for artifact in label_artifacts:
        verify_artifact_fingerprint(artifact)
        judge = artifact.get("judge")
        if judge not in JUDGES or judge in by_judge:
            raise ValueError("need one raw-label artifact for each configured judge")
        if artifact.get("judge_version") != JUDGE_VERSION:
            raise ValueError("raw labels use a stale judge rubric version")
        labels = artifact.get("labels")
        if not isinstance(labels, list):
            raise ValueError("raw-label artifact lacks labels")
        if artifact.get("label_count") != len(labels):
            raise ValueError("raw-label artifact label_count mismatch")
        by_judge[judge] = labels
    if set(by_judge) != set(JUDGES):
        raise ValueError("need exactly the three configured judge label sets")
    expected: set[tuple[str, str]] | None = None
    result = []
    for judge, labels in by_judge.items():
        pairs = set()
        for item in labels:
            pair = (item.get("query_id"), item.get("candidate_id"))
            if (
                not all(isinstance(x, str) and x for x in pair)
                or item.get("grade") not in VALID_GRADES
            ):
                raise ValueError("invalid raw judgment")
            if pair in pairs:
                raise ValueError("duplicate raw judgment")
            pairs.add(pair)
            result.append(RawJudgment(judge, pair[0], pair[1], item["grade"]))
        if expected is None:
            expected = pairs
        elif pairs != expected:
            raise ValueError("judge label coverage differs")
    return result


def merge_all_judgments(
    queries: list[dict], label_artifacts: list[dict], matrix: dict | None = None
) -> list[MergedJudgment]:
    """Merge only after complete three-way coverage is proven for every pooled item."""
    query_sources = {q["id"]: q.get("source_entity_ids", []) for q in queries}
    raw = _raw_from_artifacts(label_artifacts)
    if matrix is not None:
        pools = matrix.get("pools")
        if not isinstance(pools, dict) or set(pools) != set(query_sources):
            raise ValueError("matrix pools do not cover exactly the supplied queries")
        expected_pairs = {
            (query_id, candidate_id)
            for query_id, candidates in pools.items()
            for candidate_id in candidates
        }
        actual_pairs = {(item.query_id, item.candidate_id) for item in raw}
        if actual_pairs != expected_pairs:
            raise ValueError("raw labels are incomplete or sharded relative to the matrix pool")
    grouped: dict[str, list[RawJudgment]] = {}
    for item in raw:
        if item.query_id not in query_sources:
            raise ValueError(f"label references unknown query {item.query_id}")
        grouped.setdefault(item.query_id, []).append(item)
    if set(grouped) != set(query_sources):
        raise ValueError("raw labels do not cover every query")
    merged = []
    for query_id in sorted(grouped):
        merged.extend(merge_query_judgments(grouped[query_id], query_sources[query_id]))
    return sorted(merged, key=lambda item: (item.query_id, item.candidate_id))


def build_arbitration_packet(
    merged: list[MergedJudgment], queries: list[dict], matrix: dict
) -> dict:
    """Create a non-blind packet: raw grades/context, never a ground-truth marker."""
    query_by_id = {q["id"]: q for q in queries}
    tasks = []
    for item in merged:
        if not item.escalated:
            continue
        candidate = matrix.get("pools", {}).get(item.query_id, {}).get(item.candidate_id)
        if candidate is None:
            raise ValueError("escalated candidate absent from local candidate pool")
        tasks.append(
            {
                "task_id": f"arbitration:{item.query_id}:{item.candidate_id}",
                "query": query_by_id[item.query_id]["query"],
                "candidate": {
                    "candidate_id": item.candidate_id,
                    "title": candidate.get("title", ""),
                    "snippet": candidate.get("snippet", ""),
                },
                "raw_grades": item.raw_grades,
                "reason": item.escalation_reason,
            }
        )
    packet = {"schema_version": 1, "adjudicator": ARBITRATOR, "tasks": tasks}
    packet["fingerprint"] = artifact_fingerprint(packet)
    return packet


def apply_arbitration_results(merged: list[MergedJudgment], response: dict) -> list[MergedJudgment]:
    if response.get("adjudicator") not in {None, ARBITRATOR}:
        raise ValueError("arbitration response has an invalid Luna adjudicator")
    by_task = {f"arbitration:{m.query_id}:{m.candidate_id}": m for m in merged if m.escalated}
    submitted = response.get("labels", [])
    if len(submitted) != len(by_task):
        raise ValueError("arbitration response is incomplete")
    seen = set()
    for item in submitted:
        task_id, grade = item.get("task_id"), item.get("grade")
        if task_id not in by_task or task_id in seen or grade not in VALID_GRADES:
            raise ValueError("invalid arbitration result")
        seen.add(task_id)
        apply_arbitration_override(by_task[task_id], grade)
    if seen != set(by_task):
        raise ValueError("arbitration response is incomplete")
    return merged


def merged_artifact(
    merged: list[MergedJudgment], raw_artifacts: list[dict], queries: list[dict]
) -> dict:
    source_map = {
        q["id"]: q.get("source_entity_ids", [])
        for q in queries
        if q.get("provenance") == "squad-ground-truth"
    }
    raw = _raw_from_artifacts(raw_artifacts)
    agreements = []
    for i, judge_a in enumerate(JUDGES):
        for judge_b in JUDGES[i + 1 :]:
            agreement = compute_pairwise_agreement(raw, judge_a, judge_b)
            agreements.append(
                {
                    "judges": [judge_a, judge_b],
                    "n": agreement.n,
                    "exact_agreement_rate": agreement.exact_agreement_rate,
                    "cohens_kappa": agreement.cohens_kappa,
                    "confusion": {
                        f"{a}:{b}": count for (a, b), count in agreement.confusion.items()
                    },
                }
            )
    labels = [
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
    result = {
        "schema_version": 1,
        "judges": list(JUDGES),
        "adjudicator": ARBITRATOR,
        "judge_version": JUDGE_VERSION,
        "judgment_grades": [0, 1, 2],
        "ndcg_gains": [0, 1, 3],
        "labels": labels,
        "raw_labels_fingerprint": artifact_fingerprint(raw_artifacts),
        "calibration": calibration_accuracy(merged, source_map),
        "agreement": agreements,
        "escalation": {
            "count": sum(item.escalated for item in merged),
            "total": len(merged),
            "rate": (sum(item.escalated for item in merged) / len(merged)) if merged else None,
            "median_grade_counts": {
                str(grade): sum(item.median_grade == grade for item in merged)
                for grade in sorted(VALID_GRADES)
            },
            "final_grade_counts": {
                str(grade): sum(item.final_grade == grade for item in merged)
                for grade in sorted(VALID_GRADES)
            },
        },
    }
    result["fingerprint"] = artifact_fingerprint(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument(
        "--labels",
        type=Path,
        nargs=3,
        required=True,
        help="Validated raw label artifacts, one per judge.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arbitration-packet", type=Path)
    parser.add_argument("--arbitration-response", type=Path)
    args = parser.parse_args()
    query_value = json.loads(args.queries.read_text())
    queries = (
        query_value.get("queries", query_value) if isinstance(query_value, dict) else query_value
    )
    raw_artifacts = [json.loads(path.read_text()) for path in args.labels]
    matrix = json.loads(args.matrix.read_text())
    merged = merge_all_judgments(queries, raw_artifacts, matrix)
    packet = build_arbitration_packet(merged, queries, matrix)
    if packet["tasks"] and not args.arbitration_response:
        if not args.arbitration_packet:
            raise RuntimeError(
                "escalations require --arbitration-packet before merged labels can be finalized"
            )
        args.arbitration_packet.parent.mkdir(parents=True, exist_ok=True)
        args.arbitration_packet.write_text(json.dumps(packet, indent=2, ensure_ascii=False))
        print(
            f"Wrote {args.arbitration_packet}; collect arbitration labels and re-run with --arbitration-response"
        )
        return
    if args.arbitration_response:
        apply_arbitration_results(merged, json.loads(args.arbitration_response.read_text()))
    artifact = merged_artifact(merged, raw_artifacts, queries)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temp = args.out.with_suffix(args.out.suffix + ".tmp")
    temp.write_text(json.dumps(artifact, indent=2, ensure_ascii=False))
    temp.replace(args.out)


# ---------------------------------------------------------------------------------------------
# §4 "Per-grade 3x3 confusion breakdown" + agreement stats
# ---------------------------------------------------------------------------------------------


@dataclass
class PairwiseAgreement:
    judge_a: str
    judge_b: str
    confusion: dict[tuple[int, int], int] = field(
        default_factory=dict
    )  # (grade_a, grade_b) -> count
    n: int = 0

    @property
    def exact_agreement_rate(self) -> float:
        if self.n == 0:
            return float("nan")
        agree = sum(c for (a, b), c in self.confusion.items() if a == b)
        return agree / self.n

    @property
    def cohens_kappa(self) -> float:
        """Standard Cohen's kappa: kappa = (p_o - p_e) / (1 - p_e), p_o = observed agreement,
        p_e = chance agreement from each judge's marginal grade distribution."""
        if self.n == 0:
            return float("nan")
        p_o = self.exact_agreement_rate
        marg_a: dict[int, int] = {}
        marg_b: dict[int, int] = {}
        for (a, b), c in self.confusion.items():
            marg_a[a] = marg_a.get(a, 0) + c
            marg_b[b] = marg_b.get(b, 0) + c
        p_e = sum((marg_a.get(g, 0) / self.n) * (marg_b.get(g, 0) / self.n) for g in (0, 1, 2))
        if p_e == 1.0:
            return 1.0 if p_o == 1.0 else 0.0
        return (p_o - p_e) / (1 - p_e)


def compute_pairwise_agreement(
    all_raw_judgments: list[RawJudgment], judge_a: str, judge_b: str
) -> PairwiseAgreement:
    """Builds the 3x3 confusion matrix + agreement stats for one judge pair across every
    (query, candidate) item both judges graded."""
    by_item_a: dict[tuple[str, str], int] = {}
    by_item_b: dict[tuple[str, str], int] = {}
    for rj in all_raw_judgments:
        key = (rj.query_id, rj.candidate_id)
        if rj.judge == judge_a:
            by_item_a[key] = rj.grade
        elif rj.judge == judge_b:
            by_item_b[key] = rj.grade

    result = PairwiseAgreement(judge_a=judge_a, judge_b=judge_b)
    common_keys = set(by_item_a) & set(by_item_b)
    for key in common_keys:
        ga, gb = by_item_a[key], by_item_b[key]
        result.confusion[(ga, gb)] = result.confusion.get((ga, gb), 0) + 1
        result.n += 1
    return result


# ---------------------------------------------------------------------------------------------
# §4 SQuAD calibration accuracy
# ---------------------------------------------------------------------------------------------


def calibration_accuracy(
    merged_judgments: list[MergedJudgment],
    squad_query_source_entities: dict[str, list[str]],
) -> dict[str, float]:
    """Per §0b item 12/17: for every SQuAD-sourced positive query, its predeclared
    source_entity_ids SHOULD be graded 2 (objectively correct answer, including force-included
    never-retrieved cases). Returns {"n": count, "accuracy": fraction graded final_grade==2},
    using final_grade (post-arbitration where applicable, per §0b item 17's calibration-feeds-
    from-final-labels intent)."""
    total = 0
    correct = 0
    for mj in merged_judgments:
        source_ids = squad_query_source_entities.get(mj.query_id)
        if source_ids and mj.candidate_id in source_ids:
            total += 1
            if mj.final_grade == 2:
                correct += 1
    return {
        "n": total,
        "accuracy": (correct / total) if total else float("nan"),
    }


if __name__ == "__main__":
    main()
