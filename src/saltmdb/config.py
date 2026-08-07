import os
from pathlib import Path

__version__ = "0.1.0-alpha.68"

# Path to the root of the repository (3 levels up from src/saltmdb/config.py)
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
VIEWER_SHIM_PATH = str(_PACKAGE_ROOT / "saltmdb_viewer.py")


def get_db_path() -> str:
    """Resolve central database path from SALTMDB_DB_PATH or default ~/.saltmdb/saltmdb.db."""
    default_dir = os.path.expanduser("~/.saltmdb")
    os.makedirs(default_dir, exist_ok=True)
    return os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))


def is_semantic_search_enabled() -> bool:
    """Check SALTMDB_ENABLE_SEMANTIC env var. Defaults to True (enabled).

    Hybrid FTS5 + Dense Vector RRF search is enabled by default.
    Set SALTMDB_ENABLE_SEMANTIC=false (or 0/off/no) to explicitly disable vector search.
    """
    val = os.environ.get("SALTMDB_ENABLE_SEMANTIC", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def get_viewer_port() -> int:
    """Resolve the web viewer's port from SALTMDB_VIEWER_PORT. Defaults to 8080."""
    return int(os.environ.get("SALTMDB_VIEWER_PORT", "8080"))


def get_viewer_host() -> str:
    """Resolve the web viewer's bind host from SALTMDB_VIEWER_HOST. Defaults to 127.0.0.1 (loopback only)."""
    return os.environ.get("SALTMDB_VIEWER_HOST", "127.0.0.1")


def is_viewer_enabled() -> bool:
    """Check SALTMDB_VIEWER_ENABLED env var. Defaults to True (enabled).

    Controls whether the MCP server auto-starts the web viewer on startup.
    Set SALTMDB_VIEWER_ENABLED=false (or 0/off/no) to disable auto-start.
    """
    val = os.environ.get("SALTMDB_VIEWER_ENABLED", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


# Dedup / supersession thresholds (cosine similarity, calibrated for bge-small-en-v1.5)
DEDUP_SUPERSESSION_THRESHOLD = 0.75  # >= this -> log a supersession_candidate event
DEDUP_DUPLICATE_THRESHOLD = 0.85  # >= this -> warn the caller of a likely duplicate
DEDUP_LEXICAL_THRESHOLD = 0.40  # non-semantic (word_sim) fallback threshold

# Sliding-window chunking for chunk-level embeddings (entity_chunk_embeddings).
# Empirically settled across 3 benchmark rounds (see scripts/benchmarking/) -- do not re-tune
# without new benchmark evidence.
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200

# Cross-chunk topic reranking (search_memory's rerank_by_topic, see
# src/saltmdb/domain/services/memory_service.py:rerank_candidates_by_topic). Separate RERANK_*
# prefix from DEDUP_* above -- different subsystem/phase, independently tunable. Threshold values
# locked from scripts/benchmarking/benchmark_rerank_thresholds.py's real bge-small-en-v1.5
# Mean(Max(cosine_similarity)) measurements over hand-labeled same-topic/related-theme/unrelated
# triplets (see SALTMDB memory `4b178f4b`) -- do not re-tune without new benchmark evidence.
RERANK_CANDIDATE_POOL_SIZE = 20  # widened Stage-1 pool pulled before Stage-2 scores it
RERANK_SAME_TOPIC_THRESHOLD = 0.7680  # topic_score >= this -> "SAME_SPECIFIC_TOPIC"
RERANK_BROAD_THEME_THRESHOLD = (
    0.5322  # topic_score >= this (and below SAME_TOPIC) -> "BROADLY_RELATED_THEMES"
)

# Confidence gate on rerank_by_topic itself (search_memory's _rrf_gap_confident, see
# memory_service.py). A structurally different axis from the two RERANK_* thresholds above: those
# score topic *purity* via raw cosine similarity, this scores hybrid-search *confidence* via
# fused RRF rank-position scores (see reciprocal_rank_fusion, k=60) -- do not alias to either.
# Locked from scripts/benchmarking/benchmark_rerank_gap_gate.py's real live-corpus run (SALTMDB
# memory 870a1d4e, elaborated by the calibration write-up stored after this run): a 12-query
# calibration set (2 fixed regression anchors from 021eb8ee's original battery, plus 10 newly
# hand-picked real queries -- not a replay of that original 12-query battery itself) showed real
# "decisive" (human-judged, clean single-topic hit) queries split into two
# bands -- 2 queries with full dual-channel rank-0 agreement (both FTS and vector search picked
# the same top-1) at a clean ratio of ~2.03, and 5 "soft decisive" queries at 1.02-1.12 where RRF's
# harmonic rank decay compresses the margin even for a genuinely correct, unambiguous winner.
# Real "ambiguous" queries (near-duplicate/broad/recency-prone topics) measured 1.03-1.08,
# overlapping the soft-decisive band -- so a naive midpoint-of-bucket-extremes threshold (~1.05)
# would misfire on real ambiguous queries. The one incident this gate is meant to prevent (SALTMDB
# memory 870a1d4e's Q8 regression) was specifically the clean dual-rank-0-agreement pattern, and
# running rerank on a "soft decisive" query is empirically harmless (021eb8ee: rerank preserved the
# correct top-1 in 8/8 already-unambiguous ties) -- so the threshold is set just below the clean
# dual-rank-0-agreement value, not at the overlapping midpoint, erring toward "still rerank" on
# anything short of the sharpest signal. Combined with the dual-channel-membership check in
# _rrf_gap_confident (top1 must appear in BOTH channels' result sets, not just have a high ratio)
# -- do not re-tune without new benchmark evidence.
RERANK_GAP_SKIP_RATIO = 1.9  # rrf_top1/rrf_top2 >= this (AND top1 dual-channel) -> skip rerank

# BM25 hybrid re-ranking weights (src/saltmdb/domain/services/memory_service.py:_run_fts_search)
BM25_TITLE_WEIGHT = 10.0
BM25_CONTENT_WEIGHT = 1.0
BM25_ALIAS_WEIGHT = 5.0
RELATION_COUNT_PENALTY = 0.1

# FTS5 query-centered snippet generation (src/saltmdb/domain/services/memory_service.py:_run_fts_search)
# max_tokens must be in FTS5's valid range 1-64. Comparable to the retired top-of-doc
# heuristic's ~150 chars / ~25-30 words, a bit more generous since a centered excerpt is
# denser signal per token than an arbitrary opening line.
SNIPPET_MAX_TOKENS = 32
# Markers wrapped around each matched token so the excerpt visually shows why it matched.
# Deliberately not markdown "**" -- full_content is itself markdown, so "**" could
# nest/collide with real emphasis already in the source text. Set both to "" to disable
# highlighting and get a plain excerpt.
SNIPPET_MATCH_START = "<mark>"
SNIPPET_MATCH_END = "</mark>"
SNIPPET_ELLIPSIS = " ... "

# Quality gate thresholds (src/saltmdb/utils/nlp.py:evaluate_memory_quality)
QG_MIN_LENGTH = 20
QG_MAX_SYMBOL_RATIO = 0.35
QG_MIN_ENTROPY = 2.5
QG_MAX_ENTROPY = 5.3
QG_MAX_3GRAM_DUP = 0.30
QG_MAX_5GRAM_DUP = 0.20
QG_MIN_TTR = 0.35
QG_CLI_MIN = 2.0
QG_CLI_MAX = 26.0

# SQLite write-transaction retry/backoff (src/saltmdb/db/connection.py:write_transaction_retrying)
# Applied on top of (not instead of) PRAGMA busy_timeout; only catches "database is locked"
RETRY_MAX_ATTEMPTS = 3  # up to 3 retries beyond the first attempt (4 tries total)
RETRY_BASE_DELAY_S = 0.05  # base backoff, doubled per attempt, before jitter
RETRY_JITTER_S = 0.05  # uniform random jitter added to each backoff, avoids thundering herd

# Librarian leader-election lock (src/saltmdb/db/locks.py, src/saltmdb/domain/services/librarian_service.py)
LIBRARIAN_LOCK_STALE_MINUTES = 10  # promoted from a hardcoded "-10 minutes" literal in locks.py
LIBRARIAN_TRIGGER_COOLDOWN_S = (
    300  # promoted from a hardcoded 300 literal in librarian_service.py's trigger_librarian()
)

# Pairwise cohesion gate (src/saltmdb/domain/services/cohesion_service.py,
# relation_service.py:commit_consolidation, librarian_service.py:consolidate_vector_clusters).
# Memory-core rework Phase 3 -- see plans/ and SALTMDB memory `5c09effa`. Locked from
# scripts/benchmarking/benchmark_cohesion_threshold.py's real bge-small-en-v1.5 MIN-pairwise-
# cosine measurements over hand-crafted positive (genuinely related fragment groups) and negative
# (the confirmed `6a8fec3d` 37-way-omnibus and `3deae748` chaining-incident shapes) classes --
# do not re-tune without new benchmark evidence.
COHESION_MIN_PAIRWISE_THRESHOLD = 0.6547
# Separate, lower operating point for consolidate_vector_clusters: the benchmark's Population B
# (larger, 6-8-item Librarian candidate-pool clusters) measured a meaningfully lower positive/
# negative separation band than Population A's smaller commit-gate parent sets (2-4 items) --
# MIN over more items has more chances of hitting a weaker pair even within a genuinely cohesive
# group -- so this is intentionally NOT aliased to COHESION_MIN_PAIRWISE_THRESHOLD (see
# librarian_service.py:B3).
CLUSTER_MIN_PAIRWISE_THRESHOLD = 0.5108
COHESION_OVERRIDE_MIN_LENGTH = 20  # mirrors QG_MIN_LENGTH's "not a throwaway string" floor
# Defensive cap on find_connected_vector_clusters' multi-subset cohesive extraction, whose
# worst-case cost is O(k^4) per connected component (see librarian_service.py:B1). A prior
# benchmark (SALTMDB memory `760e8ee1`) found real Librarian batches run ~28-35 entities; 75 is
# comfortably above that with headroom. Components larger than this are skipped (logged, not
# proposed) rather than run through the full extraction.
COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION = 75

# Review-safety cap on a single emitted vector_cluster consolidation_request, distinct from
# COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION above (a compute-cost cap on the *component* fed
# into extraction, not a review-size cap on what gets extracted out of it). An extracted subset
# that itself clears CLUSTER_MIN_PAIRWISE_THRESHOLD can still be large -- up to
# COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION members -- and asking an agent to review/synthesize
# that many memories in one commit_consolidation call creates excessive cognitive pressure and
# encourages shallow acceptance (confirmed failure mode, the prior 37-item omnibus merge
# incident). consolidate_vector_clusters splits any oversized extracted group into several
# disjoint, individually-reviewable requests of this size or smaller instead of emitting it
# whole. Policy choice, not benchmarked -- no re-tuning evidence needed to change it.
MAX_CONSOLIDATION_REQUEST_SIZE = 8

# Memory-core rework Phase 4 -- see plans/eager-beaming-hippo.md and SALTMDB memory `32f9ac84`.
# Locked from scripts/benchmarking/benchmark_supersession_threshold.py's real bge-small-en-v1.5
# consolidated-summary-vs-raw-fragment cosine measurements over hand-crafted positive (a raw
# fragment concretely updating/extending the SAME specific fact a consolidated summary describes)
# and negative (a raw fragment from the same broad domain but a DIFFERENT specific fact) classes --
# 0% false-accept, 0% false-reject on the benchmark corpus -- do not re-tune without new benchmark
# evidence. This is a structurally different comparison shape from COHESION_MIN_PAIRWISE_THRESHOLD/
# CLUSTER_MIN_PAIRWISE_THRESHOLD (post-merge synthesis centroid vs raw fragment, not raw-vs-raw),
# so it is NOT aliased to either.
SUPERSESSION_MIN_SIMILARITY_THRESHOLD = 0.7557
# Cardinality floor (policy, not benchmarked) for scout_consolidated_supersessions: minimum
# number of mutually-cohesive new raw fragments required to propose a supersession candidate.
# Promoted unchanged from the pre-rework hardcoded literal; matches
# find_connected_vector_clusters' own min_cluster_size=3 default.
SUPERSESSION_MIN_OVERLAP_COUNT = 3

# Memory-core rework Phase 5 -- manage_relation governance gate (see
# plans/structured-finding-matsumoto.md and SALTMDB memory `5c09effa`/`6490fe88`).
# Locked from scripts/benchmarking/benchmark_relation_gate_threshold.py's real bge-small-en-v1.5
# raw-entity-pair cosine measurements over hand-crafted positive (two texts about the SAME
# specific fix/decision, the shape a strong predicate should concretely connect) and negative
# (the confirmed `c0ebc365` fingerprint: same broad domain, different specific fact) classes --
# 0% false-accept, 0% false-reject on the benchmark corpus -- do not re-tune without new
# benchmark evidence. Structurally different comparison shape from COHESION_MIN_PAIRWISE_THRESHOLD
# (whole parent SET, MIN pairwise) and SUPERSESSION_MIN_SIMILARITY_THRESHOLD (consolidated-summary
# vs raw-fragment) -- this is exactly one raw-vs-raw pair per call -- so it is NOT aliased to
# either.
RELATION_GATE_MIN_SIMILARITY_THRESHOLD = 0.6505
# Predicates treated as similarity/judgment claims (relation_service.py:store_relation's gate) --
# exactly the three implicated in the `c0ebc365` incident. depends_on (structural, not a
# similarity claim), consolidated_from (system-managed, gated separately by commit_consolidation
# itself), and similar_to (already *defined* by a similarity score) are deliberately excluded.
RELATION_GATE_STRONG_PREDICATES = frozenset({"elaborates_on", "resolves", "supersedes"})
# Predicate pairs that must never coexist on the same directional (source_id, target_id) edge --
# a structural contradiction, checked regardless of predicate strength. Scoped to same-direction
# only; reverse-direction contradiction is a real but separate question, not sized here.
RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS = frozenset(
    {frozenset({"supersedes", "elaborates_on"})}
)

# Rework Phase 6 -- supersession-chain resolution + relevance-abstention gate for search_memory's
# new mode="strict" (see plans/scalable-strolling-stallman.md and SALTMDB memory `9c199005`).
# Structural cap on _resolve_supersession_chains' recursive-CTE walk, matching
# analyze_lineage/analyze_dependencies' own existing depth-cap precedent (relation_service.py).
# Policy choice, not benchmarked -- a `supersedes` chain longer than 10 hops abstains (leaves the
# candidate unsubstituted) rather than being treated as trustworthy.
SUPERSESSION_CHAIN_MAX_DEPTH = 10

# NOTE: accept_or_abstain's (memory_service.py) DIRECT semantic-only acceptance rule
# (search_memory mode="strict") deliberately does NOT use a standalone
# RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE-style raw-cosine-distance constant. An earlier version of
# this gate had one (0.4086, calibrated the same worst-negative+margin way as every threshold
# above, 0% false-accept/0% false-reject on a small 6-document control corpus) -- it was removed
# after scripts/benchmarking/run_relevance_gate_holdout.py's holdout pass against the real
# 21k-entity diverse test corpus (scratch/diverse_corpus_full.db) proved it doesn't generalize: an
# unrelated/nonsense query's nearest entity-embedding neighbor routinely measured 0.22-0.34
# distance at that scale, fully overlapping the small control corpus's positive-class range. A
# fixed absolute distance floor gets less discriminating as the candidate pool grows, not more --
# it is not fixable by re-tuning the number, the signal shape itself doesn't hold at scale. See
# accept_or_abstain's own docstring for the full investigation (a rank/margin-based variant was
# also tried and also failed for the same reason). The gate instead reuses the already-calibrated,
# chunk-level RERANK_SAME_TOPIC_THRESHOLD below (via rerank_candidates_by_topic's semantic_verdict
# == "SAME_SPECIFIC_TOPIC"), which the same holdout pass confirmed DOES separate the two classes at
# real corpus scale, at the cost of a higher (accepted, not hidden) false-reject rate on weakly/
# broadly-paraphrased semantic-only matches -- see run_relevance_gate_holdout.py's docstring and
# output for the measurements.

# Hard cap on mode="strict"'s pagination overfetch loop (memory_service.py:search_memory, Part
# C2): resolution/dedup/the relevance gate can all shrink the raw FTS+semantic candidate_window
# down to fewer than `limit` survivors, so strict mode retries with a doubled candidate_window
# until either enough survivors are found or the underlying corpus is exhausted (both channels
# returned fewer rows than requested). This is the absolute ceiling on that doubling, independent
# of RERANK_CANDIDATE_POOL_SIZE (the *initial* widened window) -- policy safety valve against
# pathological queries, not benchmarked.
STRICT_OVERFETCH_CANDIDATE_CAP = 200
