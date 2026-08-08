"""Canonical DB-path resolution, election/probe-port derivation, and the discovery file.

See scratch/plans/track_b_daemon_detailed.md §2 for the full design and its 5-round Codex review
trail. This module is the single source of truth for "which database is this" and "which ports
does its daemon use" -- every other module calls into this one rather than re-deriving either.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, UTC
from typing import Any

from saltmdb.config import (
    DAEMON_PORT_PAIR_BASE,
    DAEMON_PORT_PAIR_COUNT,
    get_db_path,
)

logger = logging.getLogger(__name__)

DISCOVERY_SCHEMA_VERSION = 1


def resolve_canonical_db_path(raw_path: str | None = None) -> str:
    """The single source of truth for "which database is this". Every consumer of DB identity --
    daemon_key/election-port derivation, discovery-file lookup, the spawned daemon subprocess's
    own environment, and every identity comparison -- calls this, never get_db_path() directly.
    """
    return os.path.realpath(raw_path or get_db_path())


def daemon_key(canonical_db_path: str) -> str:
    return hashlib.sha256(canonical_db_path.encode("utf-8")).hexdigest()[:16]


def _slot(key: str) -> int:
    return int(key[:4], 16) % DAEMON_PORT_PAIR_COUNT


def election_port(key: str) -> int:
    return DAEMON_PORT_PAIR_BASE + 2 * _slot(key)


def probe_port(key: str) -> int:
    """Always election_port(key) + 1 -- the paired, ordinary (non-exclusive, freely
    accept()-ing) port used for identify diagnosis (daemon/protocol.py, daemon/server.py). Never
    the election/guard socket itself -- see server.py's ownership-mutex implementation for why."""
    return election_port(key) + 1


def _discovery_dir() -> str:
    d = os.path.expanduser("~/.saltmdb")
    os.makedirs(d, exist_ok=True)
    return d


def discovery_path(key: str) -> str:
    return os.path.join(_discovery_dir(), f"daemon_{key}.json")


def read(key: str) -> dict[str, Any] | None:
    """Best-effort read of the discovery file. Returns None if missing, unreadable, or malformed
    -- callers treat that identically to "no daemon known yet", never as an error."""
    path = discovery_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "service_port" not in data or "auth_token" not in data:
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write(key: str, db_path: str, daemon_pid: int, service_port: int, auth_token: str) -> None:
    """Atomic, permission-safe discovery-file write. Corrected after Codex round 5 (finding:
    "os.open(..., O_CREAT | O_TRUNC, 0o600) applies 0o600 only when *creating* a new file. A
    pre-existing permissively readable temp file is truncated and reused without its permissions
    being tightened") -- the temp path is PID-qualified and opened with O_EXCL, never silently
    reusing/truncating a pre-existing file's permissions."""
    final_path = discovery_path(key)
    tmp_path = f"{final_path}.{os.getpid()}.tmp"
    payload = {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "db_path": db_path,
        "daemon_pid": daemon_pid,
        "service_port": service_port,
        "auth_token": auth_token,
        "started_at": datetime.now(UTC).isoformat(),
    }
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, final_path)  # atomic on same-volume POSIX and Windows renames
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def remove(key: str) -> None:
    """Best-effort discovery-file removal on clean shutdown. Never raises -- a stale file left
    behind by a crash is harmless (see server.py's startup-race handling)."""
    try:
        os.remove(discovery_path(key))
    except OSError as e:
        logger.debug("Discovery file removal failed for key %s: %s", key, e)
