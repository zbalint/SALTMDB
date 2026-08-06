"""Calibration benchmark for Part 1's rerank score-gap gate (`_rrf_gap_confident` in
`memory_service.py`, SALTMDB memory `870a1d4e` elaborating on `021eb8ee`'s live 12-query rerank
accuracy battery).

`870a1d4e`'s original mechanism note derived a provisional `~1.5` midpoint from exactly 2 real
data points out of that battery -- Q1's ~1.0x tie (query "chunk embedding backfill timing
seconds", where `rerank_by_topic` correctly fixed a length-dilution miss) and Q8's ~2.0x decisive
win (query "dismiss_event mixed-type ID sanitization bug fix", where `rerank_by_topic` wrongly
overrode a clean hybrid winner). The original battery's other ~8 query texts were never captured
as a reusable fixture, only summarized qualitatively -- so this script does not attempt to
byte-for-byte replay `021eb8ee`. Instead it re-runs the two named queries as fixed regression
anchors, plus 10 newly hand-picked real queries against real live-corpus content (SALTMDB's own
memory titles/topics, known to exist at calibration time), giving genuine RRF-score-distribution
coverage across a comparable decisive-win / genuine-tie spread -- the same "real corpus, not
synthetic text" principle `benchmark_rerank_thresholds.py`'s docstring already establishes for a
different (cosine-similarity) axis.

This is deliberately NOT run against the live default DB (`get_db_path()`/`~/.saltmdb/saltmdb.db`)
-- per the standing SALTMDB dev rule, no code path here should touch that file. Point `--db-path`
at a throwaway *copy* of it instead (this script never writes, but a copy keeps the live DB
completely out of the loop regardless).

Unlike `benchmark_rerank_thresholds.py` (raw cosine similarity via `embed_texts`, no DB involved),
this benchmark measures RRF-fusion-score distributions, which only exist relative to a real
candidate pool -- so it calls `_run_fts_search`/`semantic_search`/`reciprocal_rank_fusion`
directly, exactly the same three calls `search_memory()` itself makes internally, against a real
(copied) database.

Moved out of tests/ (and off the test_* naming convention) for the same reason as its siblings:
this is a one-time-per-recalibration measurement pass over live data, not a deterministic
regression test.
"""

import argparse
import sys

from saltmdb.config import RERANK_CANDIDATE_POOL_SIZE
from saltmdb.db.connection import close_connection, get_connection
from saltmdb.domain.services.memory_service import (
    _run_fts_search,
    reciprocal_rank_fusion,
    semantic_search,
)
from saltmdb.utils.text import sanitize_fts_query

# Each query is real and hand-picked against known live-corpus content at calibration time (not
# synthetic). `expectation` is a human-judged label from inspecting these same queries' actual
# results at calibration time, recorded here for reproducibility -- NOT re-derived automatically
# by this script (there is no ground-truth oracle table in SALTMDB to check against).
#   "decisive": top-1 clearly the right, specific answer via a strong keyword/phrase match --
#               rerank_by_topic should be free to skip without changing the outcome.
#   "ambiguous": genuinely close call between plausible candidates (near-duplicate/correction-pair
#                topics, generic/broad phrasing, or a length-dilution-prone setup) -- rerank has
#                real signal to add here, the gate must NOT skip it.
QUERIES = [
    # Fixed regression anchors from 021eb8ee/870a1d4e:
    {
        "query": "chunk embedding backfill timing seconds",
        "expectation": "ambiguous",
        "note": "Q1 -- length-dilution case rerank correctly fixed (870a1d4e).",
    },
    {
        "query": "dismiss_event mixed-type ID sanitization bug fix",
        "expectation": "decisive",
        "note": "Q8 -- clean hybrid winner rerank wrongly overrode (870a1d4e).",
    },
    # New queries, hand-picked against known real SALTMDB corpus content:
    {
        "query": "RELATION_GATE_MIN_SIMILARITY_THRESHOLD value locked 0.6505",
        "expectation": "decisive",
        "note": "specific numeric-constant phrase, strong literal FTS match expected.",
    },
    {
        "query": "CADET task kill wait operations crash host Claude Code process",
        "expectation": "decisive",
        "note": "core memory, distinctive phrasing, should dominate both channels.",
    },
    {
        "query": "entity_chunk_embeddings PARTITION KEY storage blowup fix",
        "expectation": "decisive",
        "note": "recent commit topic, distinctive technical phrase.",
    },
    {
        "query": "vector_cluster consolidation_request size cap",
        "expectation": "decisive",
        "note": "recent commit topic, distinctive technical phrase.",
    },
    {
        "query": "CADET dashboard port 8420 conflict crashing MCP connection",
        "expectation": "decisive",
        "note": "specific bug memo, distinctive phrasing.",
    },
    {
        "query": "similar_to auto-link duplicate threshold match store_memory",
        "expectation": "decisive",
        "note": "specific design-decision memo, distinctive phrasing.",
    },
    {
        "query": "SALTMDB core memory audits is_core hardcoded consolidation",
        "expectation": "ambiguous",
        "note": "broad governance topic, multiple memories plausibly touch this phrasing.",
    },
    {
        "query": "SALTMDB rework current status next steps",
        "expectation": "ambiguous",
        "note": "Q12-style recency query -- generic boilerplate phrasing shared by many handovers.",
    },
    {
        "query": "kubernetes helm ingress autoscaling",
        "expectation": "ambiguous",
        "note": "negative control from 021eb8ee -- no real answer in corpus, low-confidence 'best of a bad lot' on both channels.",
    },
    {
        "query": "how do I bake sourdough bread with a stand mixer",
        "expectation": "ambiguous",
        "note": "second negative control -- fully unrelated to this corpus, no channel should agree confidently.",
    },
]


def _measure(conn, db_path: str, query_text: str) -> dict:
    sanitized = sanitize_fts_query(query_text)
    where_clauses = ["e.status != 'archived'"]
    params: list = []
    window = RERANK_CANDIDATE_POOL_SIZE

    fts_rows = _run_fts_search(conn, sanitized, where_clauses, params, window, 0)
    semantic_rows = semantic_search(query_text, where_clauses, params, window, db_path, 0)
    rrf_score_map = reciprocal_rank_fusion(fts_rows, semantic_rows, window)

    fts_ids = {r[0] for r in fts_rows}
    semantic_ids = {eid for eid, _ in semantic_rows}
    ids = list(rrf_score_map.keys())
    scores = list(rrf_score_map.values())

    if len(scores) < 2 or scores[1] <= 0:
        return {"ratio": None, "dual_channel_top1": None, "top1_id": ids[0] if ids else None}

    top1_id = ids[0]
    ratio = scores[0] / scores[1]
    dual_channel = top1_id in fts_ids and top1_id in semantic_ids
    return {"ratio": ratio, "dual_channel_top1": dual_channel, "top1_id": top1_id}


def run_gap_gate_calibration(db_path: str) -> dict:
    conn = get_connection(db_path)
    results = []
    try:
        print("\n=== RERANK GAP-GATE CALIBRATION (real live-corpus RRF scores) ===")
        print(f"db_path={db_path}\n")
        for q in QUERIES:
            m = _measure(conn, db_path, q["query"])
            results.append({**q, **m})
            ratio_str = f"{m['ratio']:.4f}" if m["ratio"] is not None else "n/a"
            dual_str = m["dual_channel_top1"] if m["ratio"] is not None else "n/a"
            print(
                f"  [{q['expectation']:9s}] ratio={ratio_str:8s} dual_channel_top1={dual_str!s:5s} "
                f"top1={m['top1_id']}  -- {q['query']!r}"
            )
    finally:
        close_connection(conn)

    decisive_ratios = [
        r["ratio"] for r in results if r["expectation"] == "decisive" and r["ratio"] is not None
    ]
    ambiguous_ratios = [
        r["ratio"] for r in results if r["expectation"] == "ambiguous" and r["ratio"] is not None
    ]

    print("\n--- Summary ---")
    if decisive_ratios:
        print(
            f"decisive:  n={len(decisive_ratios)}  min={min(decisive_ratios):.4f}  "
            f"max={max(decisive_ratios):.4f}"
        )
    if ambiguous_ratios:
        print(
            f"ambiguous: n={len(ambiguous_ratios)}  min={min(ambiguous_ratios):.4f}  "
            f"max={max(ambiguous_ratios):.4f}"
        )
    if decisive_ratios and ambiguous_ratios:
        suggested = (min(decisive_ratios) + max(ambiguous_ratios)) / 2
        print(
            f"\nSuggested RERANK_GAP_SKIP_RATIO (midpoint of adjacent-bucket extremes) = "
            f"{suggested:.4f}"
        )
        print(
            "NOTE: also cross-check the 'ambiguous' bucket's dual_channel_top1 values above -- "
            "the gate requires BOTH the ratio AND dual-channel agreement, so an ambiguous query "
            "that happens to have a high ratio but single-channel top1 is still correctly gated "
            "into rerank by the dual-channel check alone, independent of this ratio."
        )

    return {
        "results": results,
        "decisive_ratios": decisive_ratios,
        "ambiguous_ratios": ambiguous_ratios,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to a SQLite DB file (use a throwaway COPY of the live DB, never the live path itself).",
    )
    args = parser.parse_args()
    if "saltmdb.db" == args.db_path.strip().split("/")[-1] and "/.saltmdb/" in args.db_path:
        print(
            "Refusing to run against what looks like the live default DB path "
            f"({args.db_path!r}). Point --db-path at a throwaway copy instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    run_gap_gate_calibration(args.db_path)
