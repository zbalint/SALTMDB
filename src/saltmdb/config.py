import os
from pathlib import Path

__version__ = "0.1.0-alpha.65"

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
