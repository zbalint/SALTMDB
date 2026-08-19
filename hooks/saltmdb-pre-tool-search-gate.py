#!/usr/bin/env python3
"""SALTMDB Pre-Tool Search Gate Hook Script
Lifecycle event: PreToolUse (Claude Code, Antigravity) / preToolUse (Copilot)

Enforces Rule 1 ("Think Before You Leap"): denies a risky edit/bash/file-write tool call until
at least one search_memory call is recorded in this session's transcript.

Agent-agnostic by design (see README.md "Design principle"): this single script body is
registered from all three harnesses' configs. It differs from an earlier split
(saltmdb-pre-action-gate.sh + saltmdb-copilot-pre-tool.sh) in two ways:
  1. It does the tool-name check itself (Copilot's preToolUse fires for every tool call,
     unfiltered -- Claude Code / Antigravity pre-filter via their own "matcher" config, so for
     those two this internal check is a harmless no-op).
  2. It emits every harness's expected JSON shape in one redundant payload instead of guessing
     which schema the caller wants -- unrecognized keys are assumed ignored (this needs
     empirical per-harness verification; see README.md). This replaces
     saltmdb-copilot-pre-tool.sh, which reimplemented the same decision logic with drift risk
     instead of sharing it.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    READ_ONLY_TOOL_PREFIXES,
    emit,
    get_field,
    read_stdin_json,
    read_transcript_full,
)

SEARCH_MEMORY_PATTERN = re.compile(r'"(name|tool|toolName)"\s*:\s*"[^"]*search_memory"')


def emit_allow() -> None:
    emit(
        {
            "permissionDecision": "allow",
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"},
        }
    )
    sys.exit(0)


def emit_deny(reason: str) -> None:
    emit(
        {
            "decision": "block",
            "reason": reason,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    )
    sys.exit(0)


def main() -> None:
    data = read_stdin_json()
    tool_name = get_field(data, "tool_name", "toolName", "tool", "name")
    transcript_path = get_field(data, "transcript_path", "transcriptPath")

    if tool_name and READ_ONLY_TOOL_PREFIXES.match(tool_name):
        emit_allow()

    # Unbounded, not a tail window: this check is "was search_memory called ANYWHERE this
    # session", not "recently" -- a tail window let this gate re-trigger deep into a long
    # session purely because the matching line scrolled out of a fixed-size window (a real bug,
    # caught live).
    segment = read_transcript_full(transcript_path)
    if not segment:
        # Can't verify search history -- fail open rather than block on missing/unreadable data.
        sys.exit(0)

    if SEARCH_MEMORY_PATTERN.search(segment):
        emit_allow()

    emit_deny(
        "SALTMDB Rule 1 (Think Before You Leap): call search_memory for the relevant "
        "component/task before editing files or running commands. This gate fires once per "
        "session -- after your first search_memory call, further edits/commands this session go "
        "through unblocked."
    )


if __name__ == "__main__":
    main()
