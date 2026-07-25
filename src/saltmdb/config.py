import os
from pathlib import Path

__version__ = "0.1.0-alpha.47"

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

# Dedup / supersession thresholds (cosine similarity, calibrated for bge-small-en-v1.5)
DEDUP_SUPERSESSION_THRESHOLD = 0.75   # >= this -> log a supersession_candidate event
DEDUP_DUPLICATE_THRESHOLD = 0.85      # >= this -> warn the caller of a likely duplicate
DEDUP_LEXICAL_THRESHOLD = 0.40        # non-semantic (word_sim) fallback threshold

# BM25 hybrid re-ranking weights (src/saltmdb/domain/services/memory_service.py:_run_fts_search)
BM25_TITLE_WEIGHT = 10.0
BM25_CONTENT_WEIGHT = 1.0
BM25_ALIAS_WEIGHT = 5.0
RELATION_COUNT_PENALTY = 0.1

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
QG_PERPLEXITY_VALIDITY_MIN = 0.15

