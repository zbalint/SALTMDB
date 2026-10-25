"""Canonical DB-path resolution, election/probe-port derivation, and the discovery file.

See scratch/plans/track_b_daemon_detailed.md §2 for the full design and its 5-round Codex review
trail. This module is the single source of truth for "which database is this" and "which ports
does its daemon use" -- every other module calls into this one rather than re-deriving either.
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, UTC
from typing import IO, Any, cast

from saltmdb.config import (
    DAEMON_PORT_PAIR_BASE,
    DAEMON_PORT_PAIR_COUNT,
    get_db_path,
)

logger = logging.getLogger(__name__)

DISCOVERY_SCHEMA_VERSION = 1
_SERVICE_PORT = "service_port"
_AUTH_FIELD = "auth_token"
_DAEMON_PID = "daemon_pid"
_DB_PATH = "db_path"
_STARTED_AT = "started_at"
_ownership_warnings: set[str] = set()


def resolve_canonical_db_path(raw_path: str | None = None) -> str:
    """The single source of truth for "which database is this". Every consumer of DB identity --
    daemon_key/election-port derivation, discovery-file lookup, the spawned daemon subprocess's
    own environment, and every identity comparison -- calls this, never get_db_path() directly.
    """
    return os.path.realpath(raw_path or get_db_path())


def daemon_key(canonical_db_path: str) -> str:
    return hashlib.sha256(canonical_db_path.encode("utf-8")).hexdigest()[:16]


def daemon_log_path(canonical_db_path: str) -> str:
    """Return the owner-scoped log path for a canonical database path."""
    base = os.path.join(os.path.expanduser("~/.saltmdb"), "logs")
    return os.path.join(base, daemon_key(canonical_db_path), "daemon.log")


def open_daemon_log(canonical_db_path: str) -> IO[str]:
    """Create and open the owner-only append log for a canonical database path."""
    path = daemon_log_path(canonical_db_path)
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return open(
        path,
        "a",
        encoding="utf-8",
        opener=lambda opened_path, flags: os.open(opened_path, flags, 0o600),
    )


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
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def discovery_path(key: str) -> str:
    return os.path.join(_discovery_dir(), f"daemon_{key}.json")


def spawn_stamp_path(key: str) -> str:
    return os.path.join(_discovery_dir(), f"spawn_{key}.stamp")


def _valid(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    port = data.get(_SERVICE_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    token = data.get(_AUTH_FIELD)
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 256
        or any(not "\x21" <= char <= "\x7e" for char in token)
    ):
        return False
    pid: object = cast(object, data.get(_DAEMON_PID))
    if _DAEMON_PID in data and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        return False
    return _DB_PATH not in data or isinstance(data[_DB_PATH], str)


def _owned_file(file: Any, path: str) -> bool:
    if sys.platform == "win32":
        return True
    st = os.fstat(file.fileno())
    if st.st_uid == os.getuid() and not st.st_mode & 0o022:
        return True
    if path not in _ownership_warnings:
        logger.warning(
            "Discovery file %s is not owned by the current user or writable by group/others",
            path,
        )
        _ownership_warnings.add(path)
    return False


def _typed_record(data: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        _SERVICE_PORT: int(data[_SERVICE_PORT]),
        _AUTH_FIELD: str(data[_AUTH_FIELD]),
    }
    if _DAEMON_PID in data:
        record[_DAEMON_PID] = int(data[_DAEMON_PID])
    if _DB_PATH in data:
        record[_DB_PATH] = str(data[_DB_PATH])
    started_at = data.get(_STARTED_AT)
    if isinstance(started_at, str):
        try:
            record[_STARTED_AT] = datetime.fromisoformat(started_at).isoformat()
        except (TypeError, ValueError):
            return record
    return record


def read(key: str) -> dict[str, Any] | None:
    """Best-effort read of the discovery file. Returns None if missing, unreadable, or malformed
    -- callers treat that identically to "no daemon known yet", never as an error."""
    path = discovery_path(key)
    try:
        with open(path, "r", encoding="utf-8") as f:
            if not _owned_file(f, path):
                return None
            data = json.load(f)
        return _typed_record(data) if _valid(data) else None
    except (OSError, ValueError, RecursionError):
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
