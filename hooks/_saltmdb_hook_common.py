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
import subprocess
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


def read_transcript_full(transcript_path: str) -> str:
    """Reads a transcript file in full, or "" if unreadable. For checks that need to know
    whether something happened ANYWHERE in the session (e.g. "was search_memory ever called"),
    not just recently -- a bounded tail window would let the check re-trigger deep into a long
    session purely because the matching line scrolled out of the window, which is a real bug a
    fixed-window version of this hook had (caught live: the search-gate hook denied a Bash call
    outright mid-session despite search_memory having already been called many times earlier)."""
    path = Path(transcript_path)
    if not transcript_path or not path.is_file():
        return ""
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def read_transcript_tail(transcript_path: str, max_lines: int = 400) -> str:
    """Reads a transcript file's last max_lines as raw text, or "" if unreadable. Deliberately
    text, not parsed JSONL -- see module docstring. Only appropriate for a check that is
    genuinely about "this turn"/"recently", not "ever this session" -- see read_transcript_full
    for the distinction and why it matters."""
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


def stop_block_payload(data: dict, reason: str) -> dict:
    """Builds a blocking Stop response for the invoking harness.

    Codex validates Stop output with ``additionalProperties: false`` and accepts only its
    Stop-specific keys. Its input uniquely supplies both ``turn_id`` and ``model``; when those
    fields are present, emit the minimal Claude-compatible ``decision``/``reason`` pair that is
    also valid under Codex's strict schema. Other harnesses keep the redundant compatibility
    fields used by the existing Claude Code/Copilot registrations.
    """
    if data.get("turn_id") and data.get("model"):
        return {"decision": "block", "reason": reason}
    return {
        "decision": "block",
        "reason": reason,
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def state_dir() -> Path:
    d = Path.home() / ".claude" / "hooks" / ".state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def retrieval_outcome_flag_path(session_id: str) -> Path:
    """Shared state-file path for the search_memory -> retrieval_outcome pending flag: written by
    saltmdb-post-tool-response-nudges.py (on a search_memory call), cleared by
    saltmdb-post-tool-failure-circuit-breaker.py (on a log_event(retrieval_outcome) call) or by
    saltmdb-stop-retrieval-outcome-gate.py itself (once nudged), and read by the latter at Stop.
    Centralized here so the three scripts can't drift on the filename formula.

    This flag-file design deliberately replaces transcript-text turn-boundary scanning for this
    check: PostToolUse hooks receive tool_name/tool_input as structured JSON regardless of
    harness, so tracking state this way needs no assumption about what a given harness's
    transcript JSONL shape looks like (the assumption that caused a confirmed live bug -- see
    memory 74a3b9a2 -- when the old implementation tried to find "the last real user prompt line"
    by scanning for a bare `"type":"user"` substring, which also matches tool-result echo lines).
    """
    return state_dir() / f"retrieval-outcome-pending-{session_id}.flag"


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


def run_quiet(cmd: list[str], timeout_secs: int) -> bool:
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_secs,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
