"""Before/after precision + latency regression harness for `search_memory()`.

Companion to `compare_benchmark_runs.py`. This script is the "run" half: point it at a DB copy
and a code checkout, it replays `golden_queries.json`'s fixed real-corpus query set through the
real `search_memory()` service function (not its internal primitives -- unlike
`benchmark_rerank_gap_gate.py`, which measures raw RRF-fusion ratios, this measures the actual
end-to-end tool behavior callers see, including Part 0/1/2's gap gate and ranking flags), and
writes a timestamped JSON snapshot: per-query rank/top-1/top-k/MRR/latency/empty-result plus
aggregates broken down overall, by category, by memory_type, and by expected-content length
bucket -- plus config/threshold and corpus provenance so two snapshots can be trusted as
comparable (or flagged as not) before diffing them.

Intended workflow (SALTMDB deploy sequence, memory `77aef47e`): before syncing a new commit into
the live install, copy the live DB to a scratch path once, run this script from the OLD checkout
against that copy (`--label before`), sync + restart, then run again from the NEW checkout against
the SAME copy (`--label after` -- same DB on purpose, so `compare_benchmark_runs.py`'s diff
isolates the code change, not data drift). Reusing one DB copy across both runs only holds when
the deploy is code-only (no schema/data migration) -- see MIGRATION.md's per-release entry before
assuming that; the recorded `corpus_manifest.fingerprint` lets the compare step catch it if the
assumption was wrong anyway.

This is deliberately NOT run against the live default DB (`get_db_path()`/`~/.saltmdb/saltmdb.db`)
-- per the standing SALTMDB dev rule (see `benchmark_rerank_gap_gate.py`'s docstring, and SALTMDB
memory `4ab4cbc9`, the actual incident where a rework-branch process wrote rework-only schema to
that exact live file pre-deploy), no code path here should touch it. Point `--db-path` at a
throwaway *copy* instead; every DB access in this file is read-only (SELECT via `search_memory()`
plus two small provenance queries, no INSERT/UPDATE/DELETE anywhere in this module), but the guard
below refuses to run against anything that looks like the live path regardless, since a copy costs
nothing.

Moved out of tests/ (and off the test_* naming convention) for the same reason as its siblings:
this is a deploy-time measurement pass over live-shaped data, not a deterministic CI regression
test -- results depend on the current shape of the live corpus, which drifts over time.
"""

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.memory_service import search_memory

GOLDEN_SET_DEFAULT = Path(__file__).parent / "golden_queries.json"

# Length buckets for the EXPECTED entity's full_content, in characters. Deliberately coarse --
# this exists to catch the length-dilution failure mode 021eb8ee/870a1d4e documented (a long
# document winning on generic keyword overlap over a short, specific one), not to be a precise
# corpus profiler.
LENGTH_BUCKETS = [("short", 0, 800), ("medium", 800, 2500), ("long", 2500, None)]

# Hardcoded to match embedding_service.py's TextEmbedding(model_name=...) call -- fastembed/ONNX
# doesn't expose the loaded model name as an importable constant, so this literal must be updated
# by hand if that call ever changes.
EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"


def _refuse_live_db(db_path: str) -> None:
    if "saltmdb.db" == db_path.strip().split("/")[-1] and "/.saltmdb/" in db_path:
        print(
            "Refusing to run against what looks like the live default DB path "
            f"({db_path!r}). Point --db-path at a throwaway copy instead.",
            file=sys.stderr,
        )
        sys.exit(1)


def _git_commit(repo_dir: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _percentile(values: list, pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _length_bucket(length: int | None) -> str:
    if length is None:
        return "unknown"
    for name, lo, hi in LENGTH_BUCKETS:
        if length >= lo and (hi is None or length < hi):
            return name
    return "unknown"


def _config_snapshot() -> dict:
    """Config/threshold values in effect for this run -- so a future precision delta can be
    attributed to a real code/threshold change vs. a config drift between the two runs."""
    from saltmdb import config

    return {
        "saltmdb_version": getattr(config, "__version__", None),
        "semantic_search_enabled": config.is_semantic_search_enabled(),
        "rerank_gap_skip_ratio": config.RERANK_GAP_SKIP_RATIO,
        "rerank_candidate_pool_size": config.RERANK_CANDIDATE_POOL_SIZE,
        "rerank_broad_theme_threshold": config.RERANK_BROAD_THEME_THRESHOLD,
        "cohesion_min_pairwise_threshold": config.COHESION_MIN_PAIRWISE_THRESHOLD,
        # supersession_min_similarity_threshold retired -- Track A (memory-core rework) deleted
        # SUPERSESSION_MIN_SIMILARITY_THRESHOLD along with scout_consolidated_supersessions, the
        # only thing it ever calibrated (see scratch/plans/track_a_disposition_detailed.md).
        "relation_gate_min_similarity_threshold": config.RELATION_GATE_MIN_SIMILARITY_THRESHOLD,
        "embedding_model": EMBEDDING_MODEL_NAME,
    }


def _corpus_manifest(conn) -> dict:
    """Cheap corpus fingerprint -- (id, updated_at) pairs for every non-archived entity, hashed.
    Any content edit, new memory, archive, or supersession changes it. Not a substitute for
    hashing full content (deliberately cheap), just enough to catch "the DB copy isn't the same
    between before/after" before trusting a comparison."""
    non_archived = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE status != 'archived'"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    by_type = dict(
        conn.execute(
            "SELECT memory_type, COUNT(*) FROM entities WHERE status != 'archived' "
            "GROUP BY memory_type"
        ).fetchall()
    )
    rows = conn.execute(
        "SELECT id, updated_at FROM entities WHERE status != 'archived' ORDER BY id"
    ).fetchall()
    fingerprint_src = "|".join(f"{rid}:{rupdated}" for rid, rupdated in rows)
    fingerprint = hashlib.sha256(fingerprint_src.encode()).hexdigest()[:16]
    return {
        "non_archived_entities": non_archived,
        "total_entities": total,
        "by_memory_type": by_type,
        "fingerprint": fingerprint,
    }


def _expected_entity_info(conn, entity_ids: list) -> dict:
    """Batch-fetch memory_type + content length for every golden entry's expected entity, used
    for the by_memory_type / by_length_bucket breakdowns. Missing ids (archived/deleted since
    curation) come back with type/length = None -- the maintenance_note in golden_queries.json
    covers what to do about that."""
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT id, memory_type, length(full_content) FROM entities WHERE id IN ({placeholders})",
        entity_ids,
    ).fetchall()
    return {rid: {"memory_type": rtype, "content_length": rlen} for rid, rtype, rlen in rows}


def _run_query(
    conn, db_path: str, entry: dict, top_n: int, entity_info: dict, search_flags: dict
) -> dict:
    expected = entry["expected_entity_id"]
    top_k = entry.get("top_k", 1)
    info = entity_info.get(expected, {})
    base = {
        "id": entry["id"],
        "query": entry["query"],
        "category": entry["category"],
        "held_out": entry.get("held_out", False),
        "expected_entity_id": expected,
        "expected_memory_type": info.get("memory_type"),
        "expected_length_bucket": _length_bucket(info.get("content_length")),
        "top_k": top_k,
    }

    t0 = time.perf_counter()
    try:
        results = search_memory(
            query_keywords=entry["query"],
            limit=top_n,
            include_related=False,
            db_connection=conn,
            db_path=db_path,
            **search_flags,
        )
    except Exception as e:  # search_memory() itself is documented to catch+wrap; this is belt-
        # and-suspenders in case a caller-side issue (e.g. bad entry) raises before that.
        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            **base,
            "error": str(e),
            "rank": None,
            "top1_correct": False,
            "in_top_k": False,
            "empty_result": True,
            "reciprocal_rank": 0.0,
            "latency_ms": round(latency_ms, 2),
            "returned_ids": [],
            "actual_top1_id": None,
            "actual_top1_title": None,
        }
    latency_ms = (time.perf_counter() - t0) * 1000

    error = None
    if isinstance(results, list) and results and "error" in results[0] and "id" not in results[0]:
        error = results[0]["error"]
        results = []

    returned_ids = [r["id"] for r in results] if isinstance(results, list) else []
    rank = returned_ids.index(expected) + 1 if expected in returned_ids else None
    top1_correct = rank == 1
    in_top_k = rank is not None and rank <= top_k

    return {
        **base,
        "error": error,
        "rank": rank,
        "top1_correct": top1_correct,
        "in_top_k": in_top_k,
        "empty_result": len(returned_ids) == 0,
        "reciprocal_rank": (1.0 / rank) if rank else 0.0,
        "latency_ms": round(latency_ms, 2),
        "returned_ids": returned_ids,
        # False-positive example capture: what actually won rank 1 when the expected entity
        # didn't get there, for fast human triage without re-running the query by hand.
        "actual_top1_id": results[0]["id"] if (results and not top1_correct) else None,
        "actual_top1_title": results[0]["title"] if (results and not top1_correct) else None,
    }


def _aggregate(per_query: list) -> dict:
    n = len(per_query)
    if n == 0:
        return {"n_queries": 0}
    latencies = [r["latency_ms"] for r in per_query]
    agg = {
        "n_queries": n,
        "errors": sum(1 for r in per_query if r["error"]),
        "empty_result_rate": round(sum(1 for r in per_query if r["empty_result"]) / n, 4),
        "top1_accuracy": round(sum(1 for r in per_query if r["top1_correct"]) / n, 4),
        "top_k_accuracy": round(sum(1 for r in per_query if r["in_top_k"]) / n, 4),
        "mrr": round(statistics.mean(r["reciprocal_rank"] for r in per_query), 4),
        "latency_ms_mean": round(statistics.mean(latencies), 2),
        "latency_ms_p50": round(_percentile(latencies, 0.50), 2),
        "latency_ms_p95": round(_percentile(latencies, 0.95), 2),
    }

    def _breakdown(key: str) -> dict:
        out = {}
        for val in sorted({r[key] for r in per_query if r[key] is not None}):
            subset = [r for r in per_query if r[key] == val]
            out[val] = {
                "n": len(subset),
                "top1_accuracy": round(
                    sum(1 for r in subset if r["top1_correct"]) / len(subset), 4
                ),
                "top_k_accuracy": round(sum(1 for r in subset if r["in_top_k"]) / len(subset), 4),
            }
        return out

    agg["by_category"] = _breakdown("category")
    agg["by_memory_type"] = _breakdown("expected_memory_type")
    agg["by_length_bucket"] = _breakdown("expected_length_bucket")
    agg["false_positive_examples"] = [
        {
            "id": r["id"],
            "query": r["query"],
            "expected_entity_id": r["expected_entity_id"],
            "actual_top1_id": r["actual_top1_id"],
            "actual_top1_title": r["actual_top1_title"],
        }
        for r in per_query
        if r["actual_top1_id"] is not None
    ]
    return agg


def run_snapshot(
    db_path: str, golden_set_path: str, label: str, top_n: int = 5, search_flags: dict = None
) -> dict:
    search_flags = search_flags or {}
    golden_raw = Path(golden_set_path).read_text()
    golden = json.loads(golden_raw)
    queries = golden["queries"]
    golden_set_hash = hashlib.sha256(golden_raw.encode()).hexdigest()[:12]

    conn = get_connection(db_path)
    per_query = []
    try:
        entity_info = _expected_entity_info(conn, [q["expected_entity_id"] for q in queries])
        corpus_manifest = _corpus_manifest(conn)

        # Warm-up call, timing discarded: the FIRST semantic_search() in a fresh process pays a
        # one-time fastembed/ONNX model-load cost (tens of seconds) that has nothing to do with
        # the code under test and would otherwise dominate/skew latency_ms_mean and especially
        # latency_ms_p95 for whichever query happens to run first.
        print("(warm-up query, timing discarded -- loads the embedding model once)")
        search_memory(
            query_keywords="warmup query not part of the golden set",
            limit=1,
            include_related=False,
            db_connection=conn,
            db_path=db_path,
        )

        print(f"\n=== SEARCH_MEMORY PRECISION SNAPSHOT ({label}) ===")
        print(f"db_path={db_path}  n_queries={len(queries)}  search_flags={search_flags}\n")
        for entry in queries:
            r = _run_query(conn, db_path, entry, top_n, entity_info, search_flags)
            per_query.append(r)
            status = "ERR " if r["error"] else "OK  "
            mark = "✓" if r["top1_correct"] else ("~" if r["in_top_k"] else "✗")
            ho = "[held-out]" if r["held_out"] else "          "
            print(
                f"  [{status}] {mark} {ho} rank={r['rank']!s:>4} {r['latency_ms']:7.1f}ms  "
                f"{entry['id']}"
            )
    finally:
        close_connection(conn)

    primary = [r for r in per_query if not r["held_out"]]
    held_out = [r for r in per_query if r["held_out"]]
    aggregate = _aggregate(primary)
    aggregate_held_out = _aggregate(held_out)
    aggregate_overall = _aggregate(per_query)

    print("\n--- Aggregate (primary set, use this for calibration decisions) ---")
    print(json.dumps(aggregate, indent=2))
    print("\n--- Aggregate (held-out set, informational -- do NOT tune against this) ---")
    print(json.dumps(aggregate_held_out, indent=2))

    return {
        "meta": {
            "label": label,
            "timestamp": datetime.now(UTC).isoformat(),
            "db_path": db_path,
            "golden_set_path": str(golden_set_path),
            "golden_set_hash": golden_set_hash,
            "n_queries_in_golden_set": len(queries),
            "git_commit": _git_commit(Path(__file__).resolve().parents[2]),
            "python_version": sys.version.split()[0],
            "search_flags": search_flags,
            "config_snapshot": _config_snapshot(),
            "corpus_manifest": corpus_manifest,
        },
        "per_query": per_query,
        "aggregate": aggregate,
        "aggregate_held_out": aggregate_held_out,
        "aggregate_overall": aggregate_overall,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to a SQLite DB file (use a throwaway COPY of the live DB, never the live path itself).",
    )
    parser.add_argument(
        "--golden-set", default=str(GOLDEN_SET_DEFAULT), help="Path to golden_queries.json."
    )
    parser.add_argument(
        "--label", required=True, help='Snapshot label, e.g. "before", "after", or a commit hash.'
    )
    parser.add_argument("--out", required=True, help="Output snapshot JSON path.")
    parser.add_argument(
        "--top-n", type=int, default=5, help="Result window passed to search_memory (default 5)."
    )
    parser.add_argument("--rerank-by-topic", action="store_true")
    # BooleanOptionalAction (not store_true) with default=None: an unspecified
    # --prefer-durable-types/--demote-superseded must mean "don't pass this kwarg at all" so
    # search_memory's own signature default (True as of v0.1.0-alpha.70) applies -- store_true's
    # implicit default=False would otherwise silently pin every default benchmark run to the OLD
    # False behavior forever, defeating the point of a before/after regression harness. Explicit
    # --prefer-durable-types/--no-prefer-durable-types (and the demote_superseded pair) still let
    # a future comparison deliberately pin either value.
    parser.add_argument(
        "--prefer-durable-types", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument("--demote-superseded", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    _refuse_live_db(args.db_path)

    flags = {
        k: v
        for k, v in {
            "rerank_by_topic": args.rerank_by_topic,
            "prefer_durable_types": args.prefer_durable_types,
            "demote_superseded": args.demote_superseded,
        }.items()
        if v is not None
    }
    snapshot = run_snapshot(
        args.db_path, args.golden_set, args.label, top_n=args.top_n, search_flags=flags
    )
    Path(args.out).write_text(json.dumps(snapshot, indent=2))
    print(f"\nSnapshot written to {args.out}")
