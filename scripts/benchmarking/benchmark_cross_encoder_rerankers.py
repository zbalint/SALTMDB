"""Benchmark harness for roadmap `ba2cf66f` P1#7 / design memos `1fddc04a`/`8115fa4a`: evaluates
optional ONNX cross-encoder Stage-2 reranker candidates against the calibrated non-ML baseline.

Candidates (fastembed 0.8.0's built-in TextCrossEncoder registry, verified live during item-7
planning -- SALTMDB event `345bdd37`; `BAAI/bge-reranker-large` is NOT in this registry, so
`BAAI/bge-reranker-base` substitutes as the BGE-family candidate, per the Codex-approved plan
scope decision):
    - Xenova/ms-marco-MiniLM-L-6-v2   (compact candidate, ~0.08GB)
    - BAAI/bge-reranker-base           (BGE-family candidate, ~1.04GB)
    - jinaai/jina-reranker-v2-base-multilingual (multilingual candidate, ~1.11GB -- the diverse
      test corpus is explicitly multilingual)

For each candidate, measures (roadmap item 7's own stated bar: "compare... against the calibrated
non-ML baseline... measure ranking accuracy, false injections, false abstentions... cold/warm CPU
latency, RAM, disk/download cost, packaging complexity"):

1. Cold load time -- subprocess, genuinely cold (an in-process second-model load after the first
   already warmed shared library/runtime state would understate real cold-start cost).
2. Raw-score collection, BOTH golden_queries.json and golden_queries_negative.json -- via a
   direct-pool helper that builds the fused RRF pool using the SAME internal functions
   `_compute_pool` itself calls (`_run_fts_search`, `semantic_search`, `reciprocal_rank_fusion`),
   bypassing `search_memory`'s shared gap-gate entirely for this measurement (a gap-confident query
   would otherwise skip cross-encoder scoring even in `mode="broad"`, silently producing no score
   for exactly the queries this benchmark most needs a number for). `reranker_service.score_pairs`
   itself still caps to `CROSS_ENCODER_MAX_CANDIDATES` -- every reported top-1/top-score number
   below is explicitly scoped to "top-1 within the first CROSS_ENCODER_MAX_CANDIDATES RRF-ranked
   candidates," not the full widened pool; an `expected_entity_id` that falls outside that capped
   prefix is recorded as a miss for BOTH the cross-encoder and the RRF-baseline comparison on that
   query, not silently excluded.
3. Regression check on the REAL `search_memory(mode="strict")` gate (negative set only): compares
   abstain-vs-non-abstain outcome and, for non-empty cases, the SET of accepted entity ids against
   the `use_cross_encoder=False` baseline -- NOT a byte-identical-results assertion (cross-encoder
   reordering plus the new `cross_encoder_score` field legitimately change item order/fields on the
   `non_empty`-expected categories in this file even when the gate's own decision doesn't). A
   mismatch here is a correctness regression, not a benchmark finding -- exits nonzero.
4. Warm p50/p95 latency: N repeated `score_pairs` calls at `CROSS_ENCODER_MAX_CANDIDATES`
   candidates per call.
5. RSS delta (stdlib `resource`, no new dependency) and on-disk model size (from
   `TextCrossEncoder.list_supported_models()`'s own `size_in_GB` field, a documented estimate --
   not measured from an actual downloaded cache dir, which varies by fastembed's own caching
   layout).
6. Output: a printed comparison table plus a JSON artifact under `scratch/benchmark_results/`
   (gitignored, matching `scratch/`'s existing gitignore pattern) for future `compare_benchmark_
   runs.py`-style diffing after a fastembed version bump.

Usage:
    python scripts/benchmarking/benchmark_cross_encoder_rerankers.py [--db-path PATH]
        [--models MODEL1,MODEL2,...] [--warm-iterations N]

Defaults to a fresh throwaway copy of scratch/diverse_corpus_full.db in a tempdir (never opens
that file, or any other DB, for writes) -- pass --db-path to point at a different throwaway copy.
Refuses to run against anything that looks like the live default DB path, mirroring
run_relevance_gate_holdout.py's own guard and the standing SALTMDB dev rule (memory `51baf28d`).

Moved out of the test_* naming convention (like every sibling in this directory) so
`python -m unittest discover -s tests` never collects it -- this downloads real (large) models
from the network and measures real wall-clock latency, neither of which belongs in a deterministic
unit-test run. tests/test_reranker_service.py and tests/test_search_ranking_flags.py cover the
deterministic, mocked-runner unit/seam cases this script deliberately does not try to reproduce.
"""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from saltmdb.config import (
    CROSS_ENCODER_MAX_CANDIDATES,
    RERANK_CANDIDATE_POOL_SIZE,
    get_db_path,
)
from saltmdb.db.connection import get_connection
from saltmdb.domain.services import reranker_service
from saltmdb.domain.services.memory_service import (
    _run_fts_search,
    reciprocal_rank_fusion,
    search_memory,
    semantic_search,
)
from saltmdb.utils.text import sanitize_fts_query

POSITIVE_SET_DEFAULT = Path(__file__).parent / "golden_queries.json"
NEGATIVE_SET_DEFAULT = Path(__file__).parent / "golden_queries_negative.json"
SOURCE_CORPUS_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "diverse_corpus_full.db"
RESULTS_DIR_DEFAULT = Path(__file__).parent.parent.parent / "scratch" / "benchmark_results"

CANDIDATE_MODELS_DEFAULT = [
    "Xenova/ms-marco-MiniLM-L-6-v2",
    "BAAI/bge-reranker-base",
    "jinaai/jina-reranker-v2-base-multilingual",
]


def _refuse_live_db(db_path: str) -> None:
    """Same guard convention as run_relevance_gate_holdout.py / build_diverse_test_db.py --
    refuses to run against anything that resolves to the live default DB path, even read-only."""
    live_path = os.path.abspath(get_db_path())
    if os.path.abspath(db_path) == live_path:
        raise RuntimeError(
            f"Refusing to run against the live default DB path ({live_path}). "
            "Point --db-path at a throwaway copy instead."
        )


def _load_cases(path: Path, key: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)[key]


def _fused_pool_ids(query: str, db_path: str, window: int) -> list[str]:
    """Builds the fused RRF candidate pool using the SAME internal functions `_compute_pool`
    itself calls -- deliberately bypasses search_memory's own gap-gate/mode logic entirely, since
    this measurement isn't trying to exercise those at all (see module docstring point 2)."""
    conn = get_connection(db_path)
    try:
        sanitized = sanitize_fts_query(query) if query else ""
        where_clauses = ["e.status != 'archived'"]
        params: list = []
        fts_rows = _run_fts_search(conn, sanitized, where_clauses, params, window, 0)
        semantic_rows = semantic_search(query, where_clauses, params, window, db_path, 0)
        rrf_map = reciprocal_rank_fusion(fts_rows, semantic_rows, window)
        return list(rrf_map.keys())
    finally:
        conn.close()


def _entity_texts(entity_ids: list[str], db_path: str) -> dict[str, str]:
    if not entity_ids:
        return {}
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" for _ in entity_ids)
        rows = conn.execute(
            f"SELECT id, title, full_content FROM entities WHERE id IN ({placeholders})",
            entity_ids,
        ).fetchall()
        return {row[0]: f"{row[1]}\n\n{row[2]}" for row in rows}
    finally:
        conn.close()


def _score_capped_prefix(
    query: str, pool_ids: list[str], db_path: str
) -> tuple[list[str], list[float] | None]:
    """Returns (scored_ids_in_order, scores) for the first CROSS_ENCODER_MAX_CANDIDATES of
    pool_ids -- scores is None on any disabled/failure path, matching score_pairs' own contract."""
    capped_ids = pool_ids[:CROSS_ENCODER_MAX_CANDIDATES]
    texts_by_id = _entity_texts(capped_ids, db_path)
    scored_ids = [eid for eid in capped_ids if eid in texts_by_id]
    texts = [texts_by_id[eid] for eid in scored_ids]
    scores = reranker_service.score_pairs(query, texts)
    return scored_ids, scores


def _measure_cold_load(model_name: str, repo_root: Path) -> float | None:
    """Subprocess measurement -- a genuinely cold load, not tainted by any prior model already
    having warmed shared ONNX Runtime/library state in this same process."""
    script = (
        "import os, time\n"
        f"os.environ['SALTMDB_RERANKER_MODEL'] = {model_name!r}\n"
        "from saltmdb.domain.services import reranker_service\n"
        "t0 = time.perf_counter()\n"
        f"reranker_service.get_model({model_name!r})\n"
        "print(time.perf_counter() - t0)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        return float(result.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"    cold load measurement failed: {e}", file=sys.stderr)
        return None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _model_size_gb(model_name: str) -> float | None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    for m in TextCrossEncoder.list_supported_models():
        if m.get("model") == model_name:
            return m.get("size_in_GB")
    return None


def benchmark_model(  # noqa: PLR0915 -- linear measurement sequence, splitting it up would
    # scatter one coherent per-model report across several functions for no clarity gain (same
    # rationale this codebase already applies to search_memory/store_memory's own noqa'd
    # complexity, see CONTRIBUTING.md's documented baseline).
    model_name: str,
    db_path: str,
    positive_cases: list[dict],
    negative_cases: list[dict],
    warm_iterations: int,
    repo_root: Path,
) -> dict:
    print(f"\n=== {model_name} ===")
    os.environ["SALTMDB_RERANKER_MODEL"] = model_name

    cold_load_s = _measure_cold_load(model_name, repo_root)
    print(f"  cold load: {cold_load_s}s" if cold_load_s is not None else "  cold load: FAILED")

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    reranker_service._model = None
    reranker_service._model_name = None
    reranker_service.get_model(model_name)
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_delta_kb = rss_after - rss_before

    # --- Positive set: top-1 accuracy within the capped RRF-ranked prefix ---
    positive_results = []
    for case in positive_cases:
        pool_ids = _fused_pool_ids(case["query"], db_path, RERANK_CANDIDATE_POOL_SIZE)
        scored_ids, scores = _score_capped_prefix(case["query"], pool_ids, db_path)
        expected = case["expected_entity_id"]
        in_prefix = expected in scored_ids
        rrf_top1_correct = bool(scored_ids) and scored_ids[0] == expected
        ce_top1_correct = False
        if scores is not None and in_prefix:
            best_idx = max(range(len(scores)), key=lambda i: scores[i])
            ce_top1_correct = scored_ids[best_idx] == expected
        positive_results.append(
            {
                "id": case["id"],
                "in_capped_prefix": in_prefix,
                "rrf_baseline_top1_correct": rrf_top1_correct,
                "cross_encoder_top1_correct": ce_top1_correct,
            }
        )
        marker = "OK" if ce_top1_correct else ("MISS(out-of-prefix)" if not in_prefix else "MISS")
        print(f"  [positive] {case['id']:35s} rrf={rrf_top1_correct!s:5s} ce={marker}")

    # --- Negative set: raw top score within the capped prefix (false-accept-shaped evidence) ---
    negative_raw_scores = []
    for case in negative_cases:
        pool_ids = _fused_pool_ids(case["query"], db_path, RERANK_CANDIDATE_POOL_SIZE)
        _scored_ids, scores = _score_capped_prefix(case["query"], pool_ids, db_path)
        top_score = max(scores) if scores else None
        negative_raw_scores.append({"id": case["id"], "top_score": top_score})
        print(f"  [negative] {case['id']:35s} top_score={top_score}")

    # --- Regression check on the REAL search_memory(mode="strict") gate ---
    # limit is deliberately large (RERANK_CANDIDATE_POOL_SIZE, not the query's own default 5):
    # cross-encoder reordering can legitimately push a DIFFERENT accepted candidate into/out of a
    # small top-N window without the underlying accept/reject decision changing at all -- comparing
    # ACCEPTED SETS at a small limit would conflate "the gate's decision changed" (a real
    # regression) with "reordering changed which already-accepted candidates fit in the window"
    # (expected, not a regression). A large limit surfaces every accepted candidate regardless of
    # order, isolating the comparison to what accept_or_abstain actually decided.
    #
    # No held_out carve-out here (Codex full-diff review finding, correctly rejected an earlier
    # draft's attempt at one): held_out's meaning elsewhere in this codebase (golden_queries_
    # negative.json's own convention) is "this case's OWN gate outcome sometimes flips against the
    # real corpus" -- a property of the baseline calibration itself, not of this check. This check
    # asks a completely different question: "does accept_or_abstain's decision change when
    # cross_encoder_score is present but inert?", which must hold identically for EVERY case,
    # held_out or not, precisely BECAUSE the field is inert. The large limit above already fixes
    # the one real false-positive this surfaced during implementation (a held_out case's accepted
    # set differing only due to top-5 windowing, not an actual gate-decision change) -- once fixed
    # at the source, no held_out-specific exemption is needed or sound.
    regressions = []
    for case in negative_cases:
        baseline = search_memory(
            query_keywords=case["query"],
            db_path=db_path,
            mode="strict",
            use_cross_encoder=False,
            include_related=False,
            limit=RERANK_CANDIDATE_POOL_SIZE,
        )
        with_ce = search_memory(
            query_keywords=case["query"],
            db_path=db_path,
            mode="strict",
            use_cross_encoder=True,
            include_related=False,
            limit=RERANK_CANDIDATE_POOL_SIZE,
        )
        baseline_ids = {r["id"] for r in baseline} if isinstance(baseline, list) else None
        with_ce_ids = {r["id"] for r in with_ce} if isinstance(with_ce, list) else None
        outcome_match = bool(baseline_ids) == bool(with_ce_ids)
        id_set_match = baseline_ids == with_ce_ids
        if not (outcome_match and id_set_match):
            regressions.append(
                {
                    "id": case["id"],
                    "baseline_ids": sorted(baseline_ids or []),
                    "with_cross_encoder_ids": sorted(with_ce_ids or []),
                }
            )

    # --- Warm latency ---
    warm_pool = positive_cases[0]["query"] if positive_cases else "warm latency probe query"
    pool_ids = _fused_pool_ids(warm_pool, db_path, RERANK_CANDIDATE_POOL_SIZE)
    texts_by_id = _entity_texts(pool_ids[:CROSS_ENCODER_MAX_CANDIDATES], db_path)
    warm_texts = list(texts_by_id.values())
    latencies = []
    for _ in range(warm_iterations):
        t0 = time.perf_counter()
        reranker_service.score_pairs(warm_pool, warm_texts)
        latencies.append(time.perf_counter() - t0)

    positive_total = len(positive_results)
    positive_ce_correct = sum(1 for r in positive_results if r["cross_encoder_top1_correct"])
    positive_rrf_correct = sum(1 for r in positive_results if r["rrf_baseline_top1_correct"])

    summary = {
        "model": model_name,
        "cold_load_seconds": cold_load_s,
        "rss_delta_kb": rss_delta_kb,
        "model_size_gb_documented": _model_size_gb(model_name),
        "warm_latency_p50_seconds": _percentile(latencies, 0.5),
        "warm_latency_p95_seconds": _percentile(latencies, 0.95),
        "positive_set": {
            "total": positive_total,
            "cross_encoder_top1_correct": positive_ce_correct,
            "rrf_baseline_top1_correct": positive_rrf_correct,
            "results": positive_results,
        },
        "negative_set_raw_scores": negative_raw_scores,
        "strict_mode_regressions": regressions,
    }

    print(
        f"  positive top-1: cross-encoder {positive_ce_correct}/{positive_total}, "
        f"RRF baseline {positive_rrf_correct}/{positive_total}"
    )
    print(f"  strict-mode regressions: {len(regressions)}")
    print(
        f"  warm latency p50/p95: {summary['warm_latency_p50_seconds']}s / "
        f"{summary['warm_latency_p95_seconds']}s"
    )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None, help="Throwaway DB copy to run against")
    parser.add_argument(
        "--models", default=",".join(CANDIDATE_MODELS_DEFAULT), help="Comma-separated model names"
    )
    parser.add_argument("--warm-iterations", type=int, default=20)
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent.parent
    tmp_dir = None
    orig_reranker_env = os.environ.get("SALTMDB_RERANKER_MODEL")
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
                return 1
            tmp_dir = tempfile.mkdtemp(prefix="saltmdb_cross_encoder_benchmark_")
            db_path = os.path.join(tmp_dir, "diverse_corpus_benchmark_copy.db")
            shutil.copy(SOURCE_CORPUS_DEFAULT, db_path)

        _refuse_live_db(db_path)

        positive_cases = _load_cases(POSITIVE_SET_DEFAULT, "queries")
        negative_cases = _load_cases(NEGATIVE_SET_DEFAULT, "cases")
        models = [m.strip() for m in args.models.split(",") if m.strip()]

        all_results = []
        for model_name in models:
            all_results.append(
                benchmark_model(
                    model_name,
                    db_path,
                    positive_cases,
                    negative_cases,
                    args.warm_iterations,
                    repo_root,
                )
            )

        RESULTS_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
        out_path = RESULTS_DIR_DEFAULT / f"cross_encoder_benchmark_{int(time.time())}.json"
        out_path.write_text(json.dumps(all_results, indent=2))
        print(f"\nWrote results to {out_path}")

        total_regressions = sum(len(r["strict_mode_regressions"]) for r in all_results)
        if total_regressions:
            print(
                f"\n*** {total_regressions} strict-mode regression(s) found -- see JSON for detail ***",
                file=sys.stderr,
            )
            return 1
        return 0
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    finally:
        if orig_reranker_env is None:
            os.environ.pop("SALTMDB_RERANKER_MODEL", None)
        else:
            os.environ["SALTMDB_RERANKER_MODEL"] = orig_reranker_env
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
