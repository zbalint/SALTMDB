"""Diffs two `benchmark_precision_snapshot.py` JSON snapshots and reports regressions.

Companion to `benchmark_precision_snapshot.py`. This is the "compare" half of the before/after
deploy workflow (SALTMDB memory `77aef47e`'s deploy sequence): run the snapshot script once
against the OLD code (`--label before`), once against the NEW code on the SAME DB copy
(`--label after`), then run this script on the two resulting JSON files.

Per-query, a REGRESSION is: the expected entity was correctly ranked within `top_k` before and is
NOT after (a genuine precision loss), OR its rank got numerically worse while staying within
tolerance (a softer signal, reported separately, not counted toward the exit code). An IMPROVEMENT
is the mirror case. Anything else (both pass, both fail, or a within-tolerance rank move for a
category whose top_k > 1) is UNCHANGED. This applies uniformly to primary AND held-out queries --
held_out only changes which set a threshold gets *tuned* against (see golden_queries.json's
`held_out_meaning`), it does not exempt a query from being flagged as a real regression here.

Exit code is nonzero iff at least one real regression (was-in-tolerance, now-not) is found, so this
can gate a deploy script (`... && echo "safe to keep deployed" || echo "consider rolling back"`).
Two provenance checks run first and print loud warnings (not hard failures -- the comparison still
runs) if they don't match: `golden_set_hash` (same query set on both sides?) and
`corpus_manifest.fingerprint` (same DB content on both sides? -- a mismatch here means the "before"
and "after" runs were NOT against the same DB copy, which breaks the whole point of isolating the
diff to the code change).
"""

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _config_diff(before_cfg: dict, after_cfg: dict) -> dict:
    keys = sorted(set(before_cfg) | set(after_cfg))
    return {
        k: {"before": before_cfg.get(k), "after": after_cfg.get(k)}
        for k in keys
        if before_cfg.get(k) != after_cfg.get(k)
    }


def compare(before: dict, after: dict) -> dict:
    before_by_id = {r["id"]: r for r in before["per_query"]}
    after_by_id = {r["id"]: r for r in after["per_query"]}
    common_ids = [qid for qid in before_by_id if qid in after_by_id]
    only_before = set(before_by_id) - set(after_by_id)
    only_after = set(after_by_id) - set(before_by_id)

    regressions, improvements, rank_shifts, unchanged = [], [], [], []
    for qid in common_ids:
        b, a = before_by_id[qid], after_by_id[qid]
        b_ok, a_ok = b["in_top_k"], a["in_top_k"]
        row = {
            "id": qid,
            "query": b["query"],
            "category": b["category"],
            "held_out": b.get("held_out", False),
            "before_rank": b["rank"],
            "after_rank": a["rank"],
            "before_latency_ms": b["latency_ms"],
            "after_latency_ms": a["latency_ms"],
            "latency_delta_ms": round(a["latency_ms"] - b["latency_ms"], 2),
            "after_actual_top1_title": a.get("actual_top1_title"),
        }
        if b_ok and not a_ok:
            regressions.append(row)
        elif a_ok and not b_ok:
            improvements.append(row)
        elif b_ok and a_ok and b["rank"] != a["rank"]:
            rank_shifts.append(row)
        else:
            unchanged.append(row)

    def _delta(agg_key: str, metric_key: str):
        bv = before.get(agg_key, {}).get(metric_key)
        av = after.get(agg_key, {}).get(metric_key)
        if bv is None or av is None:
            return None
        return round(av - bv, 4)

    agg_deltas = {
        m: _delta("aggregate", m)
        for m in (
            "top1_accuracy",
            "top_k_accuracy",
            "mrr",
            "empty_result_rate",
            "latency_ms_mean",
            "latency_ms_p50",
            "latency_ms_p95",
        )
    }
    held_out_agg_deltas = {
        m: _delta("aggregate_held_out", m)
        for m in ("top1_accuracy", "top_k_accuracy", "mrr", "empty_result_rate")
    }

    before_meta, after_meta = before["meta"], after["meta"]
    return {
        "golden_set_hash_match": before_meta.get("golden_set_hash")
        == after_meta.get("golden_set_hash"),
        "corpus_fingerprint_match": before_meta.get("corpus_manifest", {}).get("fingerprint")
        == after_meta.get("corpus_manifest", {}).get("fingerprint"),
        "config_diff": _config_diff(
            before_meta.get("config_snapshot", {}), after_meta.get("config_snapshot", {})
        ),
        "before_meta": before_meta,
        "after_meta": after_meta,
        "only_in_before": sorted(only_before),
        "only_in_after": sorted(only_after),
        "regressions": regressions,
        "improvements": improvements,
        "rank_shifts_within_tolerance": rank_shifts,
        "unchanged_count": len(unchanged),
        "aggregate_before": before["aggregate"],
        "aggregate_after": after["aggregate"],
        "aggregate_deltas": agg_deltas,
        "held_out_aggregate_deltas": held_out_agg_deltas,
    }


def _print_row(row: dict) -> None:
    ho = " [held-out]" if row["held_out"] else ""
    extra = (
        f"  -> won by: {row['after_actual_top1_title']!r}" if row["after_actual_top1_title"] else ""
    )
    print(
        f"    {row['id']:38s}{ho:11s}[{row['category']:24s}] "
        f"rank {row['before_rank']!s:>4} -> {row['after_rank']!s:<4}  "
        f"latency {row['before_latency_ms']:7.1f}ms -> {row['after_latency_ms']:7.1f}ms "
        f"({row['latency_delta_ms']:+.1f}ms){extra}"
    )


def _print_provenance_warnings(result: dict) -> None:
    if not result["golden_set_hash_match"]:
        print(
            "\n⚠️  WARNING: golden_set_hash differs between the two snapshots -- comparison may "
            "not be apples-to-apples. Re-run both against the same golden_queries.json."
        )
    if not result["corpus_fingerprint_match"]:
        print(
            "\n⚠️  WARNING: corpus_manifest.fingerprint differs between the two snapshots -- the "
            "two runs were NOT against the same DB content. Precision deltas below may reflect "
            "data drift, not the code change you're actually trying to measure."
        )
    if result["config_diff"]:
        print("\n⚠️  Config/threshold values differ between the two snapshots:")
        for k, v in result["config_diff"].items():
            print(f"    {k}: {v['before']!r} -> {v['after']!r}")
    if result["only_in_before"] or result["only_in_after"]:
        print(
            f"\n⚠️  {len(result['only_in_before'])} quer(y/ies) only in before, "
            f"{len(result['only_in_after'])} only in after -- skipped in the diff below."
        )


def print_report(result: dict) -> None:
    print("=== SEARCH_MEMORY PRECISION -- BEFORE/AFTER COMPARISON ===")
    print(
        f"before: label={result['before_meta']['label']!r} "
        f"commit={result['before_meta'].get('git_commit')} "
        f"({result['before_meta']['timestamp']})"
    )
    print(
        f"after:  label={result['after_meta']['label']!r} "
        f"commit={result['after_meta'].get('git_commit')} "
        f"({result['after_meta']['timestamp']})"
    )
    _print_provenance_warnings(result)

    print(f"\n--- REGRESSIONS ({len(result['regressions'])}) ---")
    if result["regressions"]:
        for row in result["regressions"]:
            _print_row(row)
    else:
        print("    none")

    print(f"\n--- IMPROVEMENTS ({len(result['improvements'])}) ---")
    if result["improvements"]:
        for row in result["improvements"]:
            _print_row(row)
    else:
        print("    none")

    print(f"\n--- RANK SHIFTS WITHIN TOLERANCE ({len(result['rank_shifts_within_tolerance'])}) ---")
    for row in result["rank_shifts_within_tolerance"]:
        _print_row(row)

    print(f"\n--- UNCHANGED: {result['unchanged_count']} queries ---")

    print("\n--- Aggregate deltas, primary set (after - before) ---")
    print(json.dumps(result["aggregate_deltas"], indent=2))
    print("\n--- Aggregate deltas, held-out set (after - before, informational) ---")
    print(json.dumps(result["held_out_aggregate_deltas"], indent=2))

    verdict = "REGRESSION FOUND" if result["regressions"] else "NO REGRESSIONS"
    n_ho_regressions = sum(1 for r in result["regressions"] if r["held_out"])
    if n_ho_regressions:
        verdict += f" ({n_ho_regressions} in the held-out set)"
    print(f"\n=== VERDICT: {verdict} ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--before", required=True, help="Path to the 'before' snapshot JSON.")
    parser.add_argument("--after", required=True, help="Path to the 'after' snapshot JSON.")
    parser.add_argument("--out", help="Optional path to write the full comparison as JSON.")
    args = parser.parse_args()

    before_snapshot = _load(args.before)
    after_snapshot = _load(args.after)
    comparison = compare(before_snapshot, after_snapshot)
    print_report(comparison)

    if args.out:
        Path(args.out).write_text(json.dumps(comparison, indent=2))
        print(f"\nFull comparison written to {args.out}")

    sys.exit(1 if comparison["regressions"] else 0)
