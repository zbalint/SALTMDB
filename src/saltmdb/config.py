import os
import re

__version__ = "0.1.0-alpha.102"

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
# see AGENT_GUIDE.md/skills/saltmdb-usage/SKILL.md for the explicit "safe to ignore" note.
QG_OVERSIZED_PAYLOAD_THRESHOLD = 8000

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

# Rework Phase 6 -- supersession-chain resolution + relevance-abstention gate for search_memory's
# new mode="strict" (see plans/scalable-strolling-stallman.md and SALTMDB memory `9c199005`).
# Structural cap on _resolve_supersession_chains' recursive-CTE walk, matching
# analyze_lineage/analyze_dependencies' own existing depth-cap precedent (relation_service.py).
# Policy choice, not benchmarked -- a `supersedes` chain longer than 10 hops abstains (leaves the
# candidate unsubstituted) rather than being treated as trustworthy.
SUPERSESSION_CHAIN_MAX_DEPTH = 10

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
