"""Cross-OS shared-DB mount classifier (reconciliation doc §2.4, plan §7).

Detects a resolved DB path sitting on a WSL<->Windows interop mount (WSL's /mnt/<drive>/...
drvfs mounts, or native Windows' \\\\wsl.localhost\\.../\\\\wsl$\\... UNC paths, including mapped
drive letters and extended UNC forms) so the daemon can refuse to start rather than risk two
independent daemons (one per OS) racing SQLite's advisory locking across that unreliable bridge.

Fail-closed policy: "cross_boundary" OR "unknown" -> refuse to start. Only "local" proceeds. A
false-positive refusal (rare, actionable) is the safe failure direction here; a false-negative
start risks silent DB corruption.
"""

import logging
import sys
from typing import Literal

logger = logging.getLogger(__name__)

Classification = Literal["local", "cross_boundary", "unknown"]


def _looks_like_drive_letter(path: str) -> bool:
    """True for /mnt/<single-letter>[/...] -- the classic WSL drvfs automount shape."""
    rest = path[len("/mnt/") :]
    return len(rest) >= 1 and rest[0].isalpha() and (len(rest) == 1 or rest[1] == "/")


def _wsl_mount_fstype(resolved_path: str) -> str | None:
    """Longest matching mount-point prefix in /proc/mounts -> its fstype column. None if
    /proc/mounts is unreadable or nothing matches (caller treats that as "unknown", not "local")."""
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        logger.debug("Could not read /proc/mounts for cross-OS mount classification: %s", e)
        return None

    best_match_len = -1
    best_fstype: str | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, fstype = parts[1], parts[2]
        # /proc/mounts escapes spaces as \040 etc.; unescape the common cases.
        mount_point = mount_point.replace("\\040", " ").replace("\\011", "\t")
        if resolved_path == mount_point or resolved_path.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_match_len:
                best_match_len = len(mount_point)
                best_fstype = fstype
    return best_fstype


def _classify_windows_drive(resolved_path: str) -> Classification:
    """Resolve a Windows drive-letter path (e.g. C:\\...) to check whether it's a mapped network
    drive that itself resolves through to \\\\wsl$\\... -- via QueryDosDeviceW. Fails closed
    ("unknown") on any error, since this path cannot be exercised/tested from this repo's
    development environment (Linux/WSL) -- flagged for real Windows validation, per the standing
    "Windows testing is not optional" note (reconciliation §2.1)."""
    if len(resolved_path) < 2 or resolved_path[1] != ":":
        return "unknown"
    drive = resolved_path[0:2]  # e.g. "C:"
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.QueryDosDeviceW(drive, buf, len(buf))  # type: ignore[attr-defined]
        if n == 0:
            return "unknown"
        target = buf.value
        if "wsl$" in target.lower() or "wsl.localhost" in target.lower():
            return "cross_boundary"
        return "local"
    except Exception as e:
        logger.debug("QueryDosDeviceW classification failed for %s: %s", drive, e)
        return "unknown"


def classify_db_path(resolved_db_path: str) -> Classification:
    """resolved_db_path must already be fully resolved (symlinks followed) -- callers pass the
    output of discovery.resolve_canonical_db_path(), never a raw path."""
    if sys.platform == "win32":
        lowered = resolved_db_path.lower()
        if lowered.startswith("\\\\wsl.localhost\\") or lowered.startswith("\\\\wsl$\\"):
            return "cross_boundary"
        if lowered.startswith("\\\\?\\unc\\wsl$\\") or lowered.startswith("\\\\?\\unc\\wsl.localhost\\"):
            return "cross_boundary"
        return _classify_windows_drive(resolved_db_path)
    else:
        if resolved_db_path.startswith("/mnt/") and _looks_like_drive_letter(resolved_db_path):
            return "cross_boundary"
        fstype = _wsl_mount_fstype(resolved_db_path)
        if fstype == "drvfs":
            return "cross_boundary"
        if fstype is None:
            return "unknown"
        return "local"
