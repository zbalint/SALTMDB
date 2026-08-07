"""Holdout evaluation runner for search_memory's mode="strict" relevance-abstention gate (Part D
of plans/scalable-strolling-stallman.md -- see SALTMDB memory `9c199005`/`c27792a1`/`b9b75764`).

Replaces an earlier `benchmark_relevance_gate_threshold.py`, which tried to lock a standalone
RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE constant (worst-negative+margin methodology, same shape as
every other benchmark_*_threshold.py in this directory) from a small 6-document hand-built control
corpus. That constant was REMOVED, not just re-tuned: running the SAME calibration methodology
against the real, ~21.7k-entity diverse test corpus (scratch/diverse_corpus_full.db) proved a raw
absolute cosine-distance floor doesn't generalize -- see config.py's own note where that constant
used to live, and accept_or_abstain's docstring for the full investigation. There is nothing left
to "lock" here: accept_or_abstain's DIRECT semantic-only rule reuses the ALREADY-CALIBRATED
RERANK_SAME_TOPIC_THRESHOLD as-is. This script's job instead is exactly what Part D also asked
for: a holdout evaluation pass, run against real corpus-scale data, reported honestly (including
the residual false-accept rate this design accepts, not hides) BEFORE the later default-flip step
(search_memory's mode default staying "broad" until a separate, later change) is even considered.

Usage:
    python scripts/benchmarking/run_relevance_gate_holdout.py [--db-path PATH]

Defaults to a fresh throwaway copy of scratch/diverse_corpus_full.db in a tempdir (never opens
that file, or any other DB, for writes) -- pass --db-path to point at a different throwaway copy.
Refuses to run against anything that looks like the live default DB path, mirroring
benchmark_precision_snapshot.py's own guard and the standing SALTMDB dev rule (memory `51baf28d`).

Moved out of the test_* naming convention (like every sibling in this directory) so
`python -m unittest discover -s tests` never collects it -- corpus-scale data drifts, this is not
a deterministic regression test. tests/test_relevance_gate.py covers the deterministic,
engineered-fixture unit cases (tied candidates, mid-chain forks, resolved-successor-absent-from-
pool, etc.) that this script deliberately does NOT try to reproduce against noisy real data.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from saltmdb.config import get_db_path
from saltmdb.domain.services.memory_service import search_memory

GOLDEN_SET_DEFAULT = Path(__file__).parent / "golden_queries_negative.json"
SOURCE_CORPUS_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "diverse_corpus_full.db"


def _refuse_live_db(db_path: str) -> None:
    """Same guard convention as benchmark_precision_snapshot.py / build_diverse_test_db.py --
    refuses to run against anything that resolves to the live default DB path, even read-only."""
    live_path = os.path.abspath(get_db_path())
    if os.path.abspath(db_path) == live_path:
        raise RuntimeError(
            f"Refusing to run against the live default DB path ({live_path}). "
            "Point --db-path at a throwaway copy instead."
        )


def _load_golden_set(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["cases"]


def _evaluate_case(case: dict, db_path: str) -> dict:
    query = case["query"]
    category = case["category"]
    results = search_memory(
        query_keywords=query, db_path=db_path, limit=5, mode="strict", include_related=False
    )
    if isinstance(results, dict) and "error" in results:
        return {"id": case["id"], "passed": False, "detail": f"error: {results['error']}"}

    result_ids = [r.get("id") for r in results]
    result_titles = [r.get("title") for r in results]

    if case["expected_outcome"] == "empty":
        passed = results == []
        detail = "abstained (empty)" if passed else f"returned {len(results)}: {result_titles}"
    else:
        passed = len(results) > 0
        detail = f"returned {len(results)}: {result_titles}"
        if passed and "expected_top_title_contains" in case:
            needle = case["expected_top_title_contains"]
            passed = needle in (result_titles[0] or "")
            detail += f" (expected top title to contain {needle!r})"
        if passed and "expected_resolved_id" in case:
            passed = case["expected_resolved_id"] in result_ids
            detail += f" (expected id {case['expected_resolved_id']} present)"
        if passed and "expected_absent_id" in case:
            passed = case["expected_absent_id"] not in result_ids
            detail += f" (expected id {case['expected_absent_id']} ABSENT)"

    return {
        "id": case["id"],
        "category": category,
        "held_out": case.get("held_out", False),
        "passed": passed,
        "detail": detail,
    }


def run_holdout(db_path: str, golden_set_path: Path = GOLDEN_SET_DEFAULT) -> dict:
    _refuse_live_db(db_path)
    cases = _load_golden_set(golden_set_path)

    print(f'\n=== mode="strict" relevance-abstention gate holdout ({len(cases)} cases) ===\n')
    results = []
    for case in cases:
        res = _evaluate_case(case, db_path)
        results.append(res)
        marker = "PASS" if res["passed"] else "FAIL"
        held_out_tag = " [held_out]" if res.get("held_out") else ""
        print(f"  [{marker}]{held_out_tag} {res['id']:40s} {res['detail']}")

    primary = [r for r in results if not r["held_out"]]
    held_out = [r for r in results if r["held_out"]]
    primary_pass = sum(1 for r in primary if r["passed"])
    held_out_pass = sum(1 for r in held_out if r["passed"])

    print(
        f"\n  primary:   {primary_pass}/{len(primary)} passed"
        f"{'  *** REGRESSION ***' if primary_pass < len(primary) else ''}"
    )
    print(
        f"  held_out:  {held_out_pass}/{len(held_out)} passed (informational, not a required bar)"
    )

    return {"results": results, "primary_pass": primary_pass, "primary_total": len(primary)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Throwaway DB copy to run against")
    parser.add_argument("--golden-set", default=str(GOLDEN_SET_DEFAULT))
    args = parser.parse_args()

    tmp_dir = None
    try:
        if args.db_path:
            db_path = args.db_path
        else:
            if not SOURCE_CORPUS_DEFAULT.exists():
                print(
                    f"Default source corpus not found: {SOURCE_CORPUS_DEFAULT}. "
                    "Run build_diverse_test_db.py first, or pass --db-path.",
                    file=sys.stderr,
                )
                sys.exit(1)
            tmp_dir = tempfile.mkdtemp(prefix="saltmdb_relevance_gate_holdout_")
            db_path = os.path.join(tmp_dir, "diverse_corpus_holdout_copy.db")
            shutil.copy(SOURCE_CORPUS_DEFAULT, db_path)

        outcome = run_holdout(db_path, Path(args.golden_set))
        sys.exit(0 if outcome["primary_pass"] == outcome["primary_total"] else 1)
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
