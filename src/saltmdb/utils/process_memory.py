"""Cross-platform current and peak process-memory reporting for daemon logs."""

from __future__ import annotations

import logging
import re
import sys

logger = logging.getLogger(__name__)

_BYTES_PER_KIB = 1024
_BYTES_PER_MIB = 1024 * 1024
_UNAVAILABLE = "rss_mb=unavailable peak_rss_mb=unavailable"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    kernel32.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
else:
    ctypes = None
    kernel32 = None


def _parse_proc_status(text: str) -> tuple[float, float] | None:
    """Parse Linux ``/proc/self/status`` RSS and high-water-mark byte values."""
    values: dict[str, float] = {}
    for name in ("VmRSS", "VmHWM"):
        match = re.search(rf"^{name}:\s+(\d+)\s+kB\s*$", text, flags=re.MULTILINE)
        if match is None:
            return None
        values[name] = float(int(match.group(1)) * _BYTES_PER_KIB)
    return values["VmRSS"], values["VmHWM"]


def _read_windows() -> tuple[float, float] | None:
    """Read Windows working-set and peak-working-set sizes in bytes."""
    if sys.platform != "win32":
        return None
    counters_type = globals().get("_ProcessMemoryCounters")
    if ctypes is None or kernel32 is None or counters_type is None:
        return None
    counters = counters_type()
    counters.cb = ctypes.sizeof(counters)
    process = kernel32.GetCurrentProcess()
    if not kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return None
    return float(counters.WorkingSetSize), float(counters.PeakWorkingSetSize)


def read_process_memory() -> tuple[float, float] | None:
    """Return current and peak process memory in bytes, or ``None`` when unavailable."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/status", encoding="utf-8") as status_file:
                result = _parse_proc_status(status_file.read())
            if result is None:
                logger.debug("process memory fields are unavailable in /proc/self/status")
            return result
        if sys.platform == "win32":
            result = _read_windows()
            if result is None:
                logger.debug("process memory Windows API read failed")
            return result
        logger.debug("process memory is unavailable on platform %s", sys.platform)
        return None
    except Exception as exc:
        logger.debug("process memory read failed: %s", exc)
        return None


def format_process_memory() -> str:
    """Format current and peak process memory for a daemon log record."""
    try:
        result = read_process_memory()
        if result is None:
            return _UNAVAILABLE
        rss_bytes, peak_bytes = result
        return (
            f"rss_mb={rss_bytes / _BYTES_PER_MIB:.1f} peak_rss_mb={peak_bytes / _BYTES_PER_MIB:.1f}"
        )
    except Exception as exc:
        logger.debug("process memory formatting failed: %s", exc)
        return _UNAVAILABLE
