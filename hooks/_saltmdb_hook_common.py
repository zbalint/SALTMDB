"""Shared helpers for SALTMDB lifecycle hook scripts.

Not a hook itself -- imported by the saltmdb-*.py scripts in this directory. Kept dependency-free
(stdlib only) since it runs inside whatever bare Python 3 the host harness's hook subprocess has
on PATH, not necessarily SALTMDB's own venv.

Design principle (see README.md "Design principle"): hook script bodies are fully agent-agnostic
-- only *registration* (the harness config snippets) is harness-specific. This module is the
shared implementation of that: alias-tolerant input parsing, redundant multi-schema JSON output,
and best-effort raw-text transcript scanning (not a strict per-harness JSONL schema parse, since
the exact transcript line shape is not authoritatively documented across all three harnesses --
scanning for a recognizable substring pattern is more robust than trusting one assumed schema).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Merged, case-insensitive read-only tool-name vocabulary (union of Claude Code/Antigravity/
# Copilot's own read-only tools) -- a tool matching this is never gated regardless of
# search_memory history.
READ_ONLY_TOOL_PREFIXES = re.compile(r"^(view|read|grep|list|glob|search|ls|cat|find)", re.I)

# Merged risky tool-name vocabulary used by the Stop-time gates to decide whether a turn touched
# files/commands at all (union of Claude: Edit|Write|NotebookEdit|Bash|PowerShell; Antigravity:
# replace_file_content|write_to_file|run_command).
RISKY_TOOL_NAMES = re.compile(
    r'"name"\s*:\s*"(Edit|Write|NotebookEdit|Bash|PowerShell'
    r"|replace_file_content|write_to_file|run_command)\"",
)


def read_stdin_json() -> dict:
    """Reads and parses the hook's stdin payload. Never raises -- an empty/malformed payload
    (some harnesses may send nothing for events that carry no data) yields {}, and callers
    should fail open (exit 0, no output) rather than crash a hook subprocess."""
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def get_field(data: dict, *aliases: str) -> str:
    """Tries every known field-name alias in turn (e.g. transcript_path/transcriptPath); first
    non-empty match wins. Returns "" if none present -- callers treat that as "unknown", not an
    error, since not every harness sends every field for every event."""
    for alias in aliases:
        val = data.get(alias)
        if val:
            return str(val)
    return ""


def read_transcript_tail(transcript_path: str, max_lines: int = 400) -> str:
    """Reads a transcript file's last max_lines as raw text, or "" if unreadable. Deliberately
    text, not parsed JSONL -- see module docstring."""
    path = Path(transcript_path)
    if not transcript_path or not path.is_file():
        return ""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:])


def read_transcript_from_line(transcript_path: str, start_line: int) -> str:
    """Reads a transcript file from a given 1-indexed line number to EOF, or "" if unreadable."""
    path = Path(transcript_path)
    if not transcript_path or not path.is_file():
        return ""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(start_line - 1, 0) :])


def find_last_line_matching(transcript_path: str, pattern: str) -> int | None:
    """1-indexed line number of the LAST line matching pattern (plain substring, not regex --
    every caller here searches for a literal sentinel/tool-name string), or None if no match /
    unreadable file."""
    path = Path(transcript_path)
    if not transcript_path or not path.is_file():
        return None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    last = None
    for i, line in enumerate(lines, start=1):
        if pattern in line:
            last = i
    return last


def emit(payload: dict) -> None:
    """Writes one JSON payload to stdout. Callers build a single dict carrying every harness's
    expected shape redundantly (decision/reason, permissionDecision/permissionDecisionReason,
    hookSpecificOutput/systemMessage) -- see each script's own emit_* helper for its exact shape.
    Unrecognized keys are assumed ignored by harnesses that don't look for them; this needs
    empirical per-harness verification (see README.md)."""
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def state_dir() -> Path:
    d = Path.home() / ".claude" / "hooks" / ".state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_stale_state(glob_pattern: str, max_age_days: int = 2) -> None:
    """Best-effort cleanup of stale per-session state files older than max_age_days."""
    import time

    cutoff = time.time() - max_age_days * 86400
    for f in state_dir().glob(glob_pattern):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def read_count(state_file: Path) -> int:
    try:
        return int(state_file.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_count(state_file: Path, count: int) -> None:
    try:
        state_file.write_text(str(count))
    except OSError:
        pass


def clear_state(*state_files: Path) -> None:
    for f in state_files:
        try:
            f.unlink()
        except OSError:
            pass
