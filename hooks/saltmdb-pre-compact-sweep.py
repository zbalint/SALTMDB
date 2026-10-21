#!/usr/bin/env python3
"""SALTMDB Pre-Compact Sweep command hook.

A harness may invoke this command entry as a PreCompact hook. It reads the
transcript named in the hook payload through the validated reader, supplies a
bounded tail on standard input, and asks a headless agent to persist durable
lessons. A manual or cron run has no hook stdin and therefore no
``transcript_path``; it does nothing.
"""

import json
import os
import shutil
import sys

from _saltmdb_hook_common import get_field, read_stdin_json, read_transcript, run_quiet

_MAX_BYTES_ENV = "SALTMDB_PRECOMPACT_MAX_BYTES"
_DEFAULT_MAX_BYTES = 262144
_MIN_MAX_BYTES = 1024
_MAX_MAX_BYTES = 4194304
_BOUNDARIES = (('"subtype":"compact_boundary"', "system", "compact_boundary"),)
_NEWLINE_TEXT = "\n"
_NEWLINE_BYTES = b"\n"
SWEEP_PROMPT = (
    "You are the SALTMDB pre-compaction sweep hook. The current session's conversation "
    "transcript is about to be compacted and working context will be lost. Review the "
    "conversation (JSON lines supplied on standard input) for: unresolved decisions, root-cause "
    "fixes to bugs/issues, new architectural rules, or user preferences established in this session but NOT yet persisted "
    "to SALTMDB. For each item found, first call mcp__saltmdb__search_memory to confirm it is "
    "not already recorded, and if genuinely new, call mcp__saltmdb__store_memory to persist it, "
    "and mcp__saltmdb__log_event for any issue/fix worth a short-term event log entry. Do not "
    "report back conversationally -- this is a background sweep."
)

TIMEOUT_SECS = int(os.environ.get("SALTMDB_PRECOMPACT_TIMEOUT", "60"))


def _line_is_boundary(line: str, boundary: tuple[str, str, str]) -> bool:
    substring, expected_type, expected_subtype = boundary
    if substring not in line:
        return False
    try:
        record = json.loads(line)
    except ValueError:
        return False
    return (
        isinstance(record, dict)
        and record.get("type") == expected_type
        and record.get("subtype") == expected_subtype
    )


def _tail_after_boundary(text: str) -> str:
    lines = text.splitlines(keepends=True)
    boundary_index = -1
    for index, line in enumerate(lines):
        for boundary in _BOUNDARIES:
            if _line_is_boundary(line, boundary):
                boundary_index = index
                break
    return "".join(lines[boundary_index + 1 :])


def _max_bytes() -> int:
    raw_limit = os.environ.get(_MAX_BYTES_ENV)
    try:
        limit = int(raw_limit) if raw_limit is not None else _DEFAULT_MAX_BYTES
    except ValueError:
        limit = _DEFAULT_MAX_BYTES
    return limit if _MIN_MAX_BYTES <= limit <= _MAX_MAX_BYTES else _DEFAULT_MAX_BYTES


def _trim_tail(tail: str, limit: int) -> str:
    candidate = tail[-limit:]
    prefix = tail[: -len(candidate)] if candidate else tail
    encoded = candidate.encode("utf-8")
    start = max(0, len(encoded) - limit)
    kept = encoded[start:]
    cut_at_line_start = (start == 0 and (not prefix or prefix.endswith(_NEWLINE_TEXT))) or (
        start > 0 and encoded[start - 1 : start] == _NEWLINE_BYTES
    )
    if prefix and start == 0 and not prefix.endswith(_NEWLINE_TEXT):
        cut_at_line_start = False
    if not cut_at_line_start:
        search = kept[:-1] if kept.endswith(_NEWLINE_BYTES) else kept
        newline = search.find(_NEWLINE_BYTES)
        if newline >= 0:
            kept = kept[newline + 1 :]
    return kept.decode("utf-8", errors="ignore")


def _sweep_input(text: str) -> str:
    return _trim_tail(_tail_after_boundary(text), _max_bytes())


def main() -> None:
    transcript_path = get_field(read_stdin_json(), "transcript_path", "transcriptPath")
    if not transcript_path:
        sys.exit(0)
    text = read_transcript(transcript_path)
    if text is None:
        sys.exit(0)
    payload = _sweep_input(text)
    if not payload.strip():
        sys.exit(0)
    preferred = os.environ.get("SALTMDB_HOOK_PREFERRED_AGENT")
    commands = (["codex", "exec"], ["claude", "-p"])
    if preferred == "claude":
        commands = tuple(reversed(commands))
    for command in commands:
        if shutil.which(command[0]) and run_quiet([*command, SWEEP_PROMPT], TIMEOUT_SECS, payload):
            return
    # No headless CLI-based agent available on PATH -- nothing this script can do without one
    # (storing memories requires an agent's own MCP tool context, not raw SQL). Fail silent/open.
    sys.exit(0)


if __name__ == "__main__":
    main()
