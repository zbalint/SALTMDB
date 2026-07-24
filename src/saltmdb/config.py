import os
from pathlib import Path

__version__ = "0.1.0-alpha.31"

# Path to the root of the repository (3 levels up from src/saltmdb/config.py)
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
VIEWER_SHIM_PATH = str(_PACKAGE_ROOT / "saltmdb_viewer.py")

def get_db_path() -> str:
    """Resolve central database path from SALTMDB_DB_PATH or default ~/.saltmdb/saltmdb.db."""
    default_dir = os.path.expanduser("~/.saltmdb")
    os.makedirs(default_dir, exist_ok=True)
    return os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))

def is_semantic_search_enabled() -> bool:
    """Check SALTMDB_ENABLE_SEMANTIC env var.

    Gates the read-path (search_memory hybrid FTS5+semantic) only.
    Write-path embedding generation is always active regardless of this flag.
    """
    val = os.environ.get("SALTMDB_ENABLE_SEMANTIC", "").strip().lower()
    return val in ("1", "true", "yes", "on")

