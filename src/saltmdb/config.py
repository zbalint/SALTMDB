import os
import re

__version__ = "0.1.0-alpha.104"

_OWNER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def validate_owner_id(owner_id: str) -> str:
    """Validate and return one deployment-configured adapter identity.

    The MCP adapter has one immutable owner for its entire process lifetime.  Keeping the
    validation primitive separate from :func:`get_owner_id` lets startup wiring and isolated
    tests use the exact same contract without reintroducing a tool-call identity binding path.
    """
    if not isinstance(owner_id, str):
        owner_id = ""
    owner_id = owner_id.strip()
    if not _OWNER_ID_RE.fullmatch(owner_id):
        raise RuntimeError(
            "SALTMDB_OWNER_ID is required and must match ^[a-z][a-z0-9_-]{0,63}$. "
            "Configure it in the MCP server environment before starting SALTMDB."
        )
    return owner_id


def get_owner_id() -> str:
    """Return the required, deployment-configured adapter identity."""
    return validate_owner_id(os.environ.get("SALTMDB_OWNER_ID", ""))


def get_db_path() -> str:
    """Resolve central database path from SALTMDB_DB_PATH or default ~/.saltmdb/saltmdb.db."""
    default_dir = os.path.expanduser("~/.saltmdb")
    os.makedirs(default_dir, exist_ok=True)
    return os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))


def get_content_dump_dir() -> str:
    """Resolve the local directory large-content dumps are written to (see
    CONTENT_FILE_DUMP_THRESHOLD_CHARS), from SALTMDB_CONTENT_DUMP_DIR or default
    ~/.saltmdb/content_dumps. Same-machine, same-user convention as get_db_path -- SALTMDB is
    local-first, so the calling agent can always read a path under its own home directory."""
    default_dir = os.path.join(os.path.expanduser("~/.saltmdb"), "content_dumps")
    dump_dir = os.environ.get("SALTMDB_CONTENT_DUMP_DIR", default_dir)
    os.makedirs(dump_dir, exist_ok=True)
    return dump_dir


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
DEDUP_LEXICAL_THRESHOLD = 0.40  # non-semantic (word_sim) fallback threshold
# Dedup cross-encoder final-judge candidate.  This is deliberately separate from the
# search-time, opt-in CROSS_ENCODER_* settings below.
DEDUP_CROSS_ENCODER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
DEDUP_CROSS_ENCODER_MAX_CANDIDATES = 30  # matches the FTS duplicate pre-filter LIMIT 30
# Raw CE logit >= this counts as a duplicate candidate. Set from a single eyeball banding
# pass over candidate_results.json (2026-08-21, see SALTMDB memory `710882a0` follow-up):
# scores >=6.2 were ~100% genuine near-duplicates in sample, 5.0-6.2 ~50%, 4.0-5.0 ~35-45%,
# 3.0-4.0 ~10%, below 3.0 near-zero. 4.0 trades away most of the low-precision zone while
# keeping recall for duplicates that aren't literal spec-revision-chain matches. Not a
# labeled/calibrated value -- revisit with real labels before treating this as final.
DEDUP_CROSS_ENCODER_THRESHOLD = 4.0

# Sliding-window chunking for chunk-level embeddings (entity_chunk_embeddings).
# Empirically settled across 3 benchmark rounds (see scripts/benchmarking/) -- do not re-tune
# without new benchmark evidence.
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200

# Cross-chunk topic scoring (search_memory's mode="strict" relevance-gate evidence, see
# src/saltmdb/domain/services/memory_service/search_primitives.py:_score_topics_with_fallback --
# the retired full-pool topic-reranking path no longer exists; RERANK_CANDIDATE_POOL_SIZE now
# sizes Stage-2 pool widening for the fixed cross-encoder pipeline stage instead). Separate
# RERANK_* prefix from DEDUP_* above -- different
# subsystem/phase, independently tunable. Threshold values
# locked from scripts/benchmarking/benchmark_rerank_thresholds.py's real bge-small-en-v1.5
# Mean(Max(cosine_similarity)) measurements over hand-labeled same-topic/related-theme/unrelated
# triplets (see SALTMDB memory `4b178f4b`) -- do not re-tune without new benchmark evidence.
RERANK_CANDIDATE_POOL_SIZE = 20  # widened Stage-1 pool pulled before Stage-2 scores it
RERANK_SAME_TOPIC_THRESHOLD = 0.7680  # topic_score >= this -> "SAME_SPECIFIC_TOPIC"
RERANK_BROAD_THEME_THRESHOLD = (
    0.5322  # topic_score >= this (and below SAME_TOPIC) -> "BROADLY_RELATED_THEMES"
)
# Query-focused extractive preview for search_memory results (relevance_preview /
# relevance_preview_meta fields). Reuses entity_chunk_embeddings (CHUNK_SIZE_CHARS,
# CHUNK_OVERLAP_CHARS above) via a new sibling function to rerank_candidates_by_topic --
# see get_relevance_preview_data in search_primitives.py. NOT benchmarked -- these are
# placeholder defaults pending real measurement (SALTMDB grilling session 2026-09-14,
# search_memory design discussion); do not treat as final without new benchmark evidence,
# matching this file's existing convention for RERANK_*/DEDUP_CROSS_ENCODER_THRESHOLD above.
RELEVANCE_PREVIEW_CHUNK_PERCENT = 0.20  # fraction of a candidate's total chunk count selected
RELEVANCE_PREVIEW_MIN_CHUNKS = 1  # floor on selected chunk count before the opening-chunk add
RELEVANCE_PREVIEW_MAX_CHUNKS = 4  # ceiling on selected chunk count before the opening-chunk add
RELEVANCE_PREVIEW_MAX_EXPANSION_CHARS = 200  # per-side cap when expanding a selected chunk's
# span outward to the nearest paragraph/block boundary in full_content
RELEVANCE_PREVIEW_MERGE_GAP_CHARS = 50  # two expanded spans within this many chars of each
# other (or overlapping) merge into one contiguous excerpt instead of staying separate
RELEVANCE_PREVIEW_TOTAL_BUDGET_CHARS = 20000  # cumulative cap across one search_memory
# response's previews combined; never removes a result row, only omits relevance_preview
# on lower-ranked results once the running total would exceed this
RELEVANCE_PREVIEW_WARNING_PREFIX = "[UNVERIFIED PREVIEW -- call get_memory before citing as fact] "  # prepended to relevance_preview text itself so the warning survives at the point the
# model actually reads and cites the string, not just in the docstring/schema around it

# Stage-2 chunk candidate generation (search_memory's opt-in
# ``use_chunk_candidates`` path).  The values are intentionally a small, explicit experiment
# matrix: callers may choose only these windows/multipliers so benchmark fingerprints cannot
# accidentally describe an unbounded or incomparable search.  These defaults are used only when
# the opt-in flag is enabled; the ordinary two-channel search never changes its candidate-window
# or pagination mechanics.
CHUNK_CANDIDATE_OVERSAMPLING_OPTIONS = (4, 8, 12)
CHUNK_CANDIDATE_WINDOW_OPTIONS = (20, 40, 60)
CHUNK_CANDIDATE_DEFAULT_OVERSAMPLING = 4
CHUNK_CANDIDATE_DEFAULT_WINDOW = 20
CHUNK_RRF_WEIGHT_OPTIONS = (0.5, 1.0, 1.5)
CHUNK_CANDIDATE_DEFAULT_WEIGHT = 1.0

# Optional caller-supplied retrieval text.  This is deliberately a separate source from
# ``full_content``: it is an opt-in candidate-generation aid, never authoritative memory data.
RETRIEVAL_TEXT_MAX_CHARS = 4000
RETRIEVAL_TEXT_RRF_WEIGHT_OPTIONS = (0.5, 1.0, 1.5)
RETRIEVAL_TEXT_DEFAULT_FTS_WEIGHT = 1.0
RETRIEVAL_TEXT_DEFAULT_VECTOR_WEIGHT = 1.0

# Confidence gate on Stage-2 reranking (search_memory's _rrf_gap_confident, see
# memory_service/ranking.py) -- observability-only now that the cross-encoder stage runs
# unconditionally (see orchestrator.py's forcing-block comment), but the threshold itself is
# still real: it drives the "decisive winner" debug log. A structurally different axis from the
# two RERANK_* thresholds above: those
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

# BM25 hybrid re-ranking weights (src/saltmdb/domain/services/memory_service/search_primitives.py:_run_fts_search)
BM25_TITLE_WEIGHT = 10.0
BM25_CONTENT_WEIGHT = 1.0
BM25_ALIAS_WEIGHT = 5.0

# FTS5 query-centered snippet generation (src/saltmdb/domain/services/memory_service/search_primitives.py:_run_fts_search)
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
# Length tiers used by the mechanical structure gate.  These are deliberately structural
# tripwires, not prose-quality targets: short notes remain free-form, while longer notes must
# provide enough visual separation for reliable retrieval and review.
QG_PARAGRAPH_BREAK_MIN_LENGTH = 500
QG_HEADING_OR_LIST_MIN_LENGTH = 1500
QG_MULTI_HEADING_MIN_LENGTH = 4000
# Advisory-only (never blocks a write) -- flags a payload long enough that an agent following
# saltmdb-usage's "write rich, comprehensive memories" guidance may legitimately exceed this,
# see skills/saltmdb-usage/SKILL.md for the explicit "safe to ignore" note.
QG_OVERSIZED_PAYLOAD_THRESHOLD = 8000

# Large-content transport mitigation -- distinct from QG_OVERSIZED_PAYLOAD_THRESHOLD above, which
# only flags content quality advisorily and never changes what is returned. When a memory's
# content exceeds this many characters, get_memory/revise_memory/supersede_memory responses dump
# it to a local file (see saltmdb.utils.text.large_content_descriptor) and return a
# content_file_path instead of inlining the full string -- keeps a single large memory (e.g. a
# growing wayfinder map) from tripping an MCP client's own response-size limit, confirmed live
# 2026-09-14 when a ~94KB supersede_memory response for one exceeded the calling agent's own
# tool-output limit.
CONTENT_FILE_DUMP_THRESHOLD_CHARS = 20000

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
# Milestone C (wayfinder ticket "clustering trigger and freshness/maintenance", standing
# constraint 18, memory 20b4c507) -- community_detection_service's write-triggered recompute
# cooldown, same shape as LIBRARIAN_TRIGGER_COOLDOWN_S above (same shared _librarian_trigger_pool,
# same _system_locks atomic-claim pattern, new task_name='community_detection'). Confirmed at its
# seeded value (LIBRARIAN_TRIGGER_COOLDOWN_S's own order of magnitude) by the Milestone C/C.5
# benchmark run (wayfinder ticket 787ebf0c, memory 463753f9): the required cooldown-degrade probe
# -- a same-topic zero-edge memory queried mid-cooldown, before a fresh recompute_communities()
# call -- got a normal, correct, non-crashing match against the still-valid prior-cycle centroid,
# with no sign of harmful staleness. No longer a placeholder.
COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S = 300

# Milestone D (wayfinder ticket "Milestone D hierarchy mechanics," standing constraint 25, memory
# 94579e0f) -- community_detection_service's hierarchical sub-clustering trigger: a community whose
# member_count exceeds this value is a candidate for recursive re-run of Leiden on its own induced
# subgraph (see recompute_communities/_process_partition_group). A community at or below this value
# never recurses, regardless of internal heterogeneity -- a pure size trigger, not a compound
# size+heterogeneity signal, per constraint 25's own explicit locked choice. PLACEHOLDER: not yet
# benchmarked against SALTMDB's own corpus -- seeded above the live-corpus median (~10, per probe
# 13549b72) and comfortably below the live-corpus's own confirmed "definitely too large, definitely
# heterogeneous" tier (41+ members, same probe), so a community this size or smaller is expected to
# already be plausibly topic-coherent without recursion, not derived from any benchmark of its own.
# Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28)
# via its own concrete quantitative sweep against the live corpus. Do not remove the placeholder
# framing when tuning this; replace this comment with the benchmark citation once a real value is
# locked.
COMMUNITY_HIERARCHY_SIZE_THRESHOLD = 20

# Milestone D (wayfinder ticket "Milestone D hierarchy mechanics," standing constraint 25, memory
# 94579e0f) -- the maximum `communities.level` a recursive sub-clustering pass may ever produce. A
# still-oversized community at this level never recurses further, regardless of its own
# member_count -- capped iterative recursion, not unbounded, per constraint 25's own explicit locked
# choice. Level 0 (the original whole-graph pass) always counts against this cap: a value of 2 means
# levels 0, 1, and 2 may exist, and a level-2 community never produces a level-3 child. PLACEHOLDER:
# not yet benchmarked against SALTMDB's own corpus -- seeded at a small value since the live-corpus
# cost probe (memory acd52d4a) measured only one recursion level's worth of overhead (~3.3% on top
# of the full-graph pass); a deeper cap multiplies that cost per additional level and has not itself
# been measured. Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing
# constraint 28) via its own concrete quantitative sweep against the live corpus. Do not remove the
# placeholder framing when tuning this; replace this comment with the benchmark citation once a real
# value is locked.
COMMUNITY_HIERARCHY_MAX_DEPTH = 2

# Pairwise cohesion gate (src/saltmdb/domain/services/cohesion_service.py and
# relation_service.py:commit_consolidation). Memory-core rework Phase 3 -- see plans/ and SALTMDB memory
# `5c09effa`. Locked from scripts/benchmarking/benchmark_cohesion_threshold.py's real
# bge-small-en-v1.5 MIN-pairwise-cosine measurements over hand-crafted positive (genuinely related
# fragment groups) and negative (the confirmed `6a8fec3d` 37-way-omnibus and `3deae748`
# chaining-incident shapes) classes -- do not re-tune without new benchmark evidence.
COHESION_MIN_PAIRWISE_THRESHOLD = 0.6547
COHESION_OVERRIDE_MIN_LENGTH = 20  # mirrors QG_MIN_LENGTH's "not a throwaway string" floor

# Cap on how many entities a single commit_consolidation-family call may archive as parents in one
# commit. This is enforced by relation_service's consolidation path.
MAX_CONSOLIDATION_REQUEST_SIZE = 8

# Memory-core rework Phase 5 -- manage_relation governance gate (see
# plans/structured-finding-matsumoto.md and SALTMDB memory `5c09effa`/`6490fe88`).
# Locked from scripts/benchmarking/benchmark_relation_gate_threshold.py's real bge-small-en-v1.5
# raw-entity-pair cosine measurements over hand-crafted positive (two texts about the SAME
# specific fix/decision, the shape a strong predicate should concretely connect) and negative
# (the confirmed `c0ebc365` fingerprint: same broad domain, different specific fact) classes --
# 0% false-accept, 0% false-reject on the benchmark corpus -- do not re-tune without new
# benchmark evidence. Structurally different comparison shape from COHESION_MIN_PAIRWISE_THRESHOLD
# (whole parent SET, MIN pairwise) -- this is exactly one raw-vs-raw pair per call -- so it is NOT
# aliased to it.
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

# Milestone A slice A1 (wayfinder ticket G4, memory 5d578d13) -- retrieve_context's graph-expansion
# fan-out bound: max_out_of_network_neighbors = CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS *
# num_primary_hits, adopting GraphRAG local-search's own formula/default as-is (verified against
# GraphRAG source during wayfinder research ticket 5a3694d1). Milestone B calibration (memory
# 4c0c77bd) benchmarked this against 14 real retrieve_context probes on the live corpus: fan_out
# truncation (dropped_count) was 0 in every probe, including the densest (14 eligible expansion
# candidates) -- the cap was never once the binding constraint, so no live evidence supports
# changing it. Confirmed at its existing value, no longer a placeholder.
CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS = 10

# Milestone A slice A2 (wayfinder ticket G7, memory 0cb1d191) -- retrieve_context's contradicts-
# conflict-set reserve: bounds how many net-new (conflict_only) entities an unresolved contradicts
# conflict set may pull in, additive to (never drawn from) CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's
# general fan-out cap above. Deliberately a FLAT constant, not scaled by num_primary_hits like the
# fan-out cap -- G3's "small-but-nonzero" framing is an absolute visibility floor for conflicts,
# not a proportional budget. Milestone B calibration (memory 4c0c77bd) seeded the corpus's first
# real contradicts edge (a genuine, source-verified conflict between two embedding-model-accuracy
# memories) and confirmed the resulting conflict set (2 members) sits comfortably under this cap.
# Confirmed at its existing value, no longer a placeholder.
CONTEXT_EXPANSION_CONTRADICTS_CAP = 5
# Milestone C.5 (wayfinder ticket "orphan-to-community assignment mechanics," standing constraint
# 16, memory 1c15e9ac) -- retrieve_context's orphan-community reserve: bounds how many net-new
# (orphan_community) entities a call may pull in across ALL zero-edge orphan primary hits combined,
# additive to (never drawn from) CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's fan-out cap and
# CONTEXT_EXPANSION_CONTRADICTS_CAP's conflict reserve. A FLAT constant, mirroring
# CONTEXT_EXPANSION_CONTRADICTS_CAP's own shape exactly (not scaled by orphan-hit or primary-hit
# count, unlike CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS) -- orphan-community is a sparse
# force-include mechanism, not G4's always-on default case. Fixed now, NOT a Milestone-C/C.5-
# benchmark placeholder (unlike COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD below) -- mirrors
# LINEAGE_HISTORICAL_CAP's own "low-stakes and reversible, do not defer" precedent: getting this
# number wrong costs at most a few extra/fewer context entries, recoverable by re-tuning without
# calibration evidence. Seeded at the same value as CONTEXT_EXPANSION_CONTRADICTS_CAP's own current
# value, since constraint 16 explicitly mirrors that constant's shape.
CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP = 5

# Milestone C.5 (wayfinder ticket "orphan-to-community assignment mechanics," standing constraint
# 16, memory 1c15e9ac) -- the minimum cosine similarity between a zero-edge orphan primary hit's
# own entity_embeddings vector and a community's PageRank-weighted centroid (community_embeddings)
# required to assign that orphan to that community at all. Below this threshold, the orphan gets no
# orphan_community assignment for this call, full stop -- never force-assigned to its
# nearest-however-distant community. Locked from the Milestone C/C.5 benchmark run (wayfinder
# ticket 787ebf0c, memories 463753f9/c030edc4): a direct find_orphan_community_matches threshold
# sweep against real bge-small-en-v1.5 embeddings found 11/11 true same-cluster admissions with
# ZERO false-positive cross-topic admissions at every threshold from 0.50 up to 0.80 -- the
# similarity gap between real same-topic and cross-topic pairs is wide enough that false-positive
# risk was never the binding constraint anywhere in the tested range. 0.60 is the highest threshold
# in that zero-false-positive range that still admits all 11/11 true positives (0.65 dropped 2/11
# for no corresponding safety benefit). No longer a placeholder.
COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD = 0.60

# Milestone D (wayfinder ticket "Milestone D primary-search seeding," standing constraint 27,
# memory 6ca4317c) -- strategy:"global" retrieve_context's seed selection: the number of nearest
# (by cosine similarity of the query embedding to each leaf community's own centroid) leaf
# communities taken as seeds for a global-mode call. Unconditional best-effort top-K -- no minimum-
# similarity abstention floor in v1 (a deliberate choice, not an oversight: constraint 27 exists
# specifically because retrieve_context's existing strict abstention gate already broke this exact
# query shape once, see b9b75764/e957aa78; adding a second uncalibrated threshold in the same
# subsystem for the same reason would repeat that mistake). A FLAT constant, not scaled by anything
# -- unlike CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's per-primary-hit-count scaling, there is no
# analogous per-call quantity to scale a community seed count against in this design. PLACEHOLDER:
# not yet benchmarked against SALTMDB's own corpus -- seeded at the same order of magnitude as
# CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's own current value, not derived from any benchmark of
# its own. Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing
# constraint 28) via its own concrete quantitative sweep against the live corpus. Do not remove the
# placeholder framing when tuning this; replace this comment with the benchmark citation once a
# real value is locked.
CONTEXT_GLOBAL_TOP_K_COMMUNITIES = 10
# Milestone D2 Bug B fix (SALTMDB memory 510c19ff, live DNS repro bb609f00) -- strategy:"global"
# retrieve_context's community-seeding relative admission gate: a candidate leaf community is
# admitted only if its centroid-to-query cosine similarity is within this absolute gap of the
# single best-matching (rank-1) community's own similarity in the same call. Rank-1 itself is
# always force-included regardless of its own absolute similarity (never subject to this gap), so
# this floor can only ever shrink the existing CONTEXT_GLOBAL_TOP_K_COMMUNITIES window, never
# produce an empty seed set on its own -- structurally avoiding the exact false-negative failure
# mode (local strategy's strict abstention gate, memory b9b75764, original incident
# e957aa78/33cb492f) that constraint 27's original "no floor in v1" choice was written to avoid
# repeating. This is an ABSOLUTE GAP anchored to this call's own top match, not a FIXED absolute
# similarity floor on raw centroid similarity -- the fixed-absolute-floor shape was already tried
# and abandoned once for the entity-level relevance gate elsewhere in this file (see the
# RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE removal NOTE below RERANK_GAP_SKIP_RATIO: an absolute
# cosine-distance/-similarity cutoff does not generalize as candidate-pool size grows).
# CALIBRATED (wayfinder ticket f3f03936, SALTMDB memory f138c6d0, 2026-09-19): a live-corpus sweep
# across five fresh, previously-unbenchmarked topics (Vonini, Incus/firewall, CADET quota bugs,
# ACIE dogfooding, homelab WalnutPi) at gap in/{0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50} found 0.10
# to be the largest value that stays clean (zero off-topic seeded representatives) across every
# topic while still capturing every additional genuinely-relevant community available -- 0.05
# under-recalls a second real community on some topics, 0.15+ starts admitting off-topic
# representatives on at least one topic. The prior 0.5 placeholder was confirmed too loose,
# corroborating the live DNS-query finding in memory b87b9d46.
CONTEXT_GLOBAL_SEED_SIMILARITY_GAP = 0.10

# Milestone D (wayfinder ticket "Milestone D retrieval/synthesis mechanics," standing constraint 26,
# memory 51127287) -- strategy:"global" retrieve_context's representative-slot reserve: how many of
# the CONTEXT_GLOBAL_TOP_K_COMMUNITIES seeded leaf communities actually get their own constraint-19
# representative force-included as a guaranteed, budget-accounted (but never budget-gated) slot.
# Milestone D4 fix (SALTMDB memory 6dc8924d, live DNS repro this same session) -- ranked by each
# admitted representative's own real per-query similarity (post Bug A fix, memory 3c40dd0e), NOT by
# its community's centroid/seed-rank similarity -- the weakest-matching representatives are dropped
# first if the eligible count exceeds this cap, and CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP
# below can additionally shrink this window before the cap is even reached. Independently capped
# from CONTEXT_GLOBAL_MEMBER_POOL_CAP below -- a community whose representative candidate is dropped
# here (by either the cap or the gap) still contributes its own other members to the member pool on
# equal footing; the dropped representative candidate itself is never redirected into the member
# pool (this constant's own pre-existing cap-independent behavior, unchanged by the D4 fix).
# PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus -- seeded at the same order of
# magnitude as CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP's own current value, mirroring that constant's
# own "sparse force-include mechanism" shape, not derived from any benchmark of its own. Recalibrated
# by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28) via its own
# concrete quantitative sweep against the live corpus. Do not remove the placeholder framing when
# tuning this; replace this comment with the benchmark citation once a real value is locked.
CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP = 5
# Milestone D4 fix (SALTMDB memory 6dc8924d, live DNS repro this same session) -- strategy:"global"
# retrieve_context's representative-slot relative admission gate: an admitted representative
# candidate is kept only if its own real per-query similarity is within this absolute gap of the
# single best-matching representative's own similarity in the same call. The best representative
# itself is always force-included regardless of its own absolute similarity (its own gap-to-itself
# is always 0, so it is never subject to this gap), so this floor can only ever shrink the existing
# CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP window, never produce an empty representative_reserve on
# its own when at least one eligible representative candidate exists. This mirrors
# CONTEXT_GLOBAL_SEED_SIMILARITY_GAP above exactly -- same ABSOLUTE GAP (not FIXED absolute floor)
# shape and the same generalization reasoning (see that constant's own comment and the
# RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE removal NOTE below RERANK_GAP_SKIP_RATIO) -- just applied one
# stage later, to each representative's own real per-query similarity instead of its community's
# centroid similarity. Closes the gap D3 (memory 9c4d6277's decision 6) left open: a community
# relevant enough to be seeded does not guarantee its own single best member is itself a strong
# match, and representative_reserve previously had no floor of its own at all.
# CALIBRATED (wayfinder ticket f3f03936, SALTMDB memory f138c6d0, 2026-09-19): kept equal to
# CONTEXT_GLOBAL_SEED_SIMILARITY_GAP per D3/D4's own original design intent -- the same live-corpus
# sweep (five fresh topics plus a dedicated small/organically-weak-community probe, comparing
# retrieve_context(global) against search_memory(broad)) found no evidence requiring the two
# constants to diverge; 0.10 kept representative selection sane on a real 2-member community
# (298d2f7a) with zero degradation.
CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP = 0.10

# Milestone D (wayfinder ticket "Milestone D retrieval/synthesis mechanics," standing constraint 26,
# memory 51127287) -- strategy:"global" retrieve_context's member-pool node-eligibility cap: the
# maximum combined candidate-member count, across every seeded leaf community, that is even offered
# to context_budget_service.pack_context_budget's own separate real-token packing pass. A node-
# eligibility cap, not a token-count cap -- mirrors CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS/G4's own
# "independent from the token-budget axis" precedent (constraint 25's own explicit framing), applied
# here at the community-member level. Ranked by each member's own query-embedding similarity across
# the whole combined pool (never per-community sub-pools) before this cap truncates it, matching
# constraint 26's own "one shared, continuously relevance-ranked pool" requirement. PLACEHOLDER: not
# yet benchmarked against SALTMDB's own corpus -- seeded at CONTEXT_GLOBAL_TOP_K_COMMUNITIES times a
# small constant, giving each seeded community a comparable member-slot budget on average to what a
# single Milestone-A expansion pass typically admits, not derived from any benchmark of its own.
# Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28)
# via its own concrete quantitative sweep against the live corpus. Do not remove the placeholder
# framing when tuning this; replace this comment with the benchmark citation once a real value is
# locked.
CONTEXT_GLOBAL_MEMBER_POOL_CAP = 30

# Rework Phase 6 -- supersession-chain resolution + relevance-abstention gate for search_memory's
# new mode="strict" (see plans/scalable-strolling-stallman.md and SALTMDB memory `9c199005`).
# Structural cap on _resolve_supersession_chains' recursive-CTE walk, matching
# analyze_lineage/analyze_dependencies' own existing depth-cap precedent (relation_service.py).
# Policy choice, not benchmarked -- a `supersedes` chain longer than 10 hops abstains (leaves the
# candidate unsubstituted) rather than being treated as trustworthy.
SUPERSESSION_CHAIN_MAX_DEPTH = 10

# Milestone A slice A3 (wayfinder standing constraint 15, memory 0ca97ddc) -- retrieve_context's
# lineage.historical display cap: the number of most-recent supersession-chain entries shown by
# default per lineage[head]. Deliberately decoupled from get_lineage's own general-purpose
# max_depth=10 traversal bound above (a different, downstream concern -- display size, not
# traversal depth). UNLIKE CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS/CONTEXT_EXPANSION_CONTRADICTS_CAP,
# this is NOT a Milestone-B placeholder -- constraint 15 already fixed this number now (getting it
# wrong is low-stakes and reversible, one extra get_lineage call recovers the full chain), so do
# not add placeholder framing here or flag it for recalibration. An ancestor beyond this cap can
# still appear in lineage[head].historical when it is force-included as a resolved contradicts-edge
# endpoint (Milestone A slice A3, wayfinder gap 4cbf26ac) -- this constant bounds only the normal,
# non-force-included window.
LINEAGE_HISTORICAL_CAP = 5
# Milestone A slice A4 (wayfinder ticket G8, memory cdf2c7cf) -- retrieve_context's context-budget
# packing: the default real-token-count ceiling (via fastembed's TextEmbedding.token_count(), see
# context_budget_service.py) applied when a caller does not supply their own budget_tokens value.
# Milestone B calibration (memory 4c0c77bd) benchmarked the old 4000 default against 11 ordinary
# probes on the live corpus: usage ranged 1948-3952 tokens (several pinned at 97-99% of the
# ceiling), with 7/11 probes hitting non-trivial expansion truncation (worst: 11/14 and 9/11
# eligible candidates dropped) -- 4000 was simply too tight for this corpus's real memory sizes, not
# evidence expansion itself needed bounding differently. Raised to 8000; re-verified this
# eliminates truncation for ordinary queries and cuts it sharply (e.g. 11/14 dropped -> 3/14) even
# on the densest probe in the sample. No longer a placeholder.
CONTEXT_BUDGET_DEFAULT_TOKENS = 8000

# Milestone A slice A4 (wayfinder ticket G8, memory cdf2c7cf) -- retrieve_context's context-budget
# packing: the hard ceiling a caller-supplied budget_tokens is clamped DOWN to if it exceeds this
# value (never clamped up -- a caller requesting less than this gets exactly what they asked for).
# Milestone B calibration (memory 4c0c77bd) raised this alongside CONTEXT_BUDGET_DEFAULT_TOKENS,
# keeping the same 4x default:ceiling ratio the original 4000/16000 pair had. No longer a
# placeholder.
CONTEXT_BUDGET_MAX_TOKENS = 32000

# NOTE: accept_or_abstain's (memory_service/ranking.py) DIRECT semantic-only acceptance rule
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

# Hard cap on mode="strict"'s pagination overfetch loop (memory_service/orchestrator.py:search_memory, Part
# C2): resolution/dedup/the relevance gate can all shrink the raw FTS+semantic candidate_window
# down to fewer than `limit` survivors, so strict mode retries with a doubled candidate_window
# until either enough survivors are found or the underlying corpus is exhausted (both channels
# returned fewer rows than requested). This is the absolute ceiling on that doubling, independent
# of RERANK_CANDIDATE_POOL_SIZE (the *initial* widened window) -- policy safety valve against
# pathological queries, not benchmarked.
STRICT_OVERFETCH_CANDIDATE_CAP = 200

# Cross-encoder reranking (search_memory's fixed Stage-2 final-reranker slot as of
# candidate/search-ce-final-reranker, merged d1655d2). The pipeline no longer has per-call
# enable/force flags; actual scoring remains deployment-configured through SALTMDB_RERANKER_MODEL
# and deterministically falls back to RRF order when disabled or unavailable. See
# src/saltmdb/domain/services/reranker_service.py). Roadmap ba2cf66f P1#7 / design memos
# 1fddc04a/8115fa4a: an ONNX-only Stage-2 pairwise reranker, no PyTorch, no new
# dependency (fastembed is already pinned and already wraps ONNX Runtime for the bi-encoder).


def get_reranker_model_name() -> str | None:
    """SALTMDB_RERANKER_MODEL env var, stripped. Unset/empty -> None (disabled, the default).

    This deployment-level switch controls the fixed final-reranker stage; it is not exposed as a
    per-call search_memory parameter. Unset keeps RRF ordering, while a supported model enables
    cross-encoder ordering for every eligible query in that daemon process.
    """
    val = os.environ.get("SALTMDB_RERANKER_MODEL", "").strip()
    return val or None


# fastembed 0.8.0's TextCrossEncoder built-in registry as verified live during item-7 planning
# (SALTMDB event `345bdd37`) -- does NOT include BAAI/bge-reranker-large (design memos
# 1fddc04a/8115fa4a assumed it existed; verified absent from fastembed.rerank.cross_encoder
# .TextCrossEncoder.list_supported_models()). BAAI/bge-reranker-base substitutes as the
# BGE-family candidate instead (Codex-approved substitution, item-7 plan round 1).
CROSS_ENCODER_SUPPORTED_MODELS = frozenset(
    {
        "Xenova/ms-marco-MiniLM-L-6-v2",
        "Xenova/ms-marco-MiniLM-L-12-v2",
        "BAAI/bge-reranker-base",
        "jinaai/jina-reranker-v1-tiny-en",
        "jinaai/jina-reranker-v1-turbo-en",
        "jinaai/jina-reranker-v2-base-multilingual",
    }
)
CROSS_ENCODER_CANDIDATE_CAP_OPTIONS = (10, 15, 20)
CROSS_ENCODER_TEXT_CAP_OPTIONS = (1000, 2000)
CROSS_ENCODER_MAX_CANDIDATES = 10  # default experiment cap; opt-in calls may use 15 or 20
# forward pass per candidate, materially more expensive per-item than the bi-encoder's single
# batched call
CROSS_ENCODER_MAX_CHARS = 1000  # default candidate text cap; opt-in calls may use 2000
# latency data if warranted, not a pre-committed final value
CROSS_ENCODER_MAX_QUERY_CHARS = 300  # query is concatenated into EVERY pair scored -- capped
# independently of candidate length (Codex plan-review round-1 finding)

# Track B backend daemon (see scratch/plans/track_b_daemon_detailed.md §2-§6, 5 rounds of Codex
# plan review). The daemon is the sole process that opens SQLite; per-agent stdio MCP processes
# become thin frontend adapters talking to it over local TCP RPC.

# Election-port / probe-port pairing: one fixed slot per canonical DB path derives BOTH ports as a
# single (2i, 2i+1) pair, so a genuine election-port collision between two DB paths always also
# collides on the probe port (collision-preservation by construction, round-4 fix after round-3's
# independently-hashed derivation broke this). Range chosen inside the IANA dynamic/private port
# range (49152-65535); 8000 pairs is enormous overkill for the realistic number of DBs one user
# runs, kept far below the range ceiling.
DAEMON_PORT_PAIR_BASE = 49500
DAEMON_PORT_PAIR_COUNT = 8000  # ports 49500-65499 (8000 pairs * 2)

# RPC wire protocol (daemon/protocol.py): length-prefixed JSON framing.
DAEMON_RPC_MAX_MESSAGE_BYTES = 33_554_432  # 32 MiB
DAEMON_RPC_CONNECT_TIMEOUT_S = 2.0
DAEMON_RPC_CALL_TIMEOUT_S = 60.0  # generous for a cold embedding-model load or a large search

# Probe-port identify responder (daemon/server.py): ordinary hygiene bounds, not safety-critical
# (the probe port is not the ownership-arbitration mechanism -- that's the election guard alone).
DAEMON_IDENTIFY_MAX_CONCURRENT = 8
DAEMON_IDENTIFY_READ_TIMEOUT_S = 1.0

# Bounded, best-effort latency/resource-hygiene drain during daemon shutdown (NOT a data-safety
# mechanism -- SQLite's own WAL+busy_timeout+write_transaction_retrying machinery, already relied
# on throughout this codebase including today's actual multi-process architecture, is what makes
# brief overlap between an outgoing daemon and its successor safe). Currently informational only;
# no code path blocks on this value as of the round-5 shutdown-sequence design (immediate
# cancel_futures + prompt listener close), kept as a named constant in case a future bounded-wait
# step is added.
DAEMON_SHUTDOWN_DRAIN_TIMEOUT_S = 5.0

# _DaemonState.begin_goodbye()'s lease-drain wait (daemon/server.py, fix for a 2026-08-26 review
# finding): bounded so a lease that is never released -- a hung dispatch_tool call, or a future
# bug that skips _release_caller_lease's finally -- cannot wedge the session-closing thread (and
# the client's goodbye RPC with it) forever. Chosen shorter than DAEMON_RPC_CALL_TIMEOUT_S so the
# daemon gives up and logs before the client's own goodbye-ack read would have timed out anyway.
DAEMON_GOODBYE_LEASE_DRAIN_TIMEOUT_S = 10.0

# ensure_daemon_running()'s discovery-retry loop (daemon/client.py).
DAEMON_DISCOVERY_RETRY_ATTEMPTS = 40  # 40 * 0.25s = 10s bounded window
DAEMON_DISCOVERY_RETRY_DELAY_S = 0.25
# Periodic re-spawn interval within that same loop (in attempts, not seconds) -- closes a
# drain-retry livelock where a single speculative spawn could lose the race against a still-
# shutting-down prior owner with nothing left retrying (Codex round-2 finding).
DAEMON_RESPAWN_RETRY_INTERVAL = 8

# Daemon grace-period shutdown timer once the last session disconnects -- matches the pre-Track-B
# viewer liveness watchdog's existing grace_period default exactly, no user-visible behavior change.
DAEMON_SHUTDOWN_GRACE_PERIOD_S = 30

# _embed_pool stall visibility (daemon/embed_stall_monitor.py) -- H6 fix. The monitor reports
# stale pending embeddings periodically; it deliberately does not terminate a daemon with a live
# client session. Once a daemon is truly idle, the existing 30-second grace shutdown already
# exits it and a later client respawns a fresh pool. Recovering a stall while a session remains
# connected needs an explicit lifecycle-policy change and is intentionally separate work.
EMBED_STALL_CHECK_INTERVAL_S = 300
EMBED_STALL_PENDING_AGE_THRESHOLD_S = 300

# Core-memory bootstrap governance (see plans/core_memory_bootstrap_governance_detailed.md and
# src/saltmdb/domain/services/core_governance_service.py, the sole owner of these rules). is_core
# is a scarce, temporary bootstrap-delivery mechanism, not a general "important knowledge" tier --
# these three limits are independent hard caps, enforced inside every write transaction that can
# create/promote/enlarge a core memory, never just advisory.
CORE_MAX_ACTIVE = 5  # max non-archived is_core=1 entities at once, global across the whole DB
CORE_MAX_CONTENT_CHARS = 2500  # max full_content per core, Unicode code points (len(text))
CORE_MAX_RENDERED_CHARS = 15000  # max exact rendered bootstrap digest, Unicode code points
CORE_REASON_MIN_CHARS = 20
CORE_REASON_MAX_CHARS = 500
CORE_EXIT_MIN_CHARS = 20
CORE_EXIT_MAX_CHARS = 500
CORE_REVIEW_RATIONALE_MIN_CHARS = 20
CORE_REVIEW_RATIONALE_MAX_CHARS = 1000
CORE_MAX_DETAIL_MEMORY_IDS = 3  # per-core cap on core_detail_memory_ids, the sole governed
# declaration of a core's linked detail memories -- incidental graph edges are never adopted into it
CORE_DEFAULT_REVIEW_DAYS = 14
CORE_MAX_REVIEW_DAYS = 30  # both the default-omitted-timestamp ceiling and retain's own bound
CORE_BOOTSTRAP_ERROR_MAX_CHARS = 12000  # hard cap on render_bootstrap_error's output (resolved
# review finding #6) -- comfortably below CORE_MAX_RENDERED_CHARS so a heavily corrupt active-core
# set can never itself trigger the hook truncation/spill behavior this feature exists to prevent
