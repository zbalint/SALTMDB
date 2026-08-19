#!/usr/bin/env python3
"""SALTMDB Post-Tool Failure Circuit-Breaker Hook Script
Lifecycle event: PostToolUse (Claude Code, Antigravity) / postToolUse (Copilot, if supported)
Register for: mcp__saltmdb__log_event

Fingerprints repeated log_event(event_type="issue") calls with a matching error_code in a short
window and enforces CLAUDE.md rule 2 ("On a failing command/tool/test -- especially 2 consecutive
times -- stop. Search memory for precedent, then form a deliberate new plan. Don't loop.") as a
nudge instead of relying on the agent remembering it mid-loop.

Best-effort by design: transcript line matching, not a structured event-log query (a hook script
only sees the transcript, not the SALTMDB events table) -- a heuristic near-window fingerprint
match, not a guaranteed-precise one.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import emit, get_field, read_stdin_json  # noqa: E402

ISSUE_PATTERN = re.compile(r'"event_type"\s*:\s*"issue"')
EVENT_TYPE_PATTERN = re.compile(r'"event_type"\s*:\s*"([^"]*)"')
ERROR_CODE_PATTERN = re.compile(r'"error_code"\s*:\s*"([^"]*)"')


def main() -> None:
    data = read_stdin_json()
    tool_name = get_field(data, "tool_name", "toolName", "tool", "name")
    if not tool_name.endswith("log_event"):
        return

    transcript_path = get_field(data, "transcript_path", "transcriptPath")
    if not transcript_path or not Path(transcript_path).is_file():
        return

    tool_input = data.get("tool_input") or data.get("toolInput") or data.get("input") or data
    # json.dumps, not str(): tool_input is a parsed dict at this point, and str() on a dict uses
    # Python repr (single quotes), which the JSON-shaped regexes below wouldn't match.
    input_text = json.dumps(tool_input)
    event_match = EVENT_TYPE_PATTERN.search(input_text)
    error_match = ERROR_CODE_PATTERN.search(input_text)
    if not event_match or event_match.group(1) != "issue":
        return
    if not error_match or not error_match.group(1):
        return
    error_code = error_match.group(1)

    lines = Path(transcript_path).read_text(errors="replace").splitlines()[-200:]
    # prior_count includes this call's own just-appended entry, so >=2 means this is the 2nd+
    # repeat.
    prior_count = sum(1 for line in lines if error_code in line and ISSUE_PATTERN.search(line))

    if prior_count >= 2:
        reason = (
            f"SALTMDB circuit breaker (CLAUDE.md rule 2): error_code '{error_code}' has now "
            f"been logged as an issue {prior_count} times in this window. Stop -- do not retry "
            "the same action again. Search memory for precedent on this exact error, then form "
            "a deliberately new plan before proceeding."
        )
        emit(
            {
                "systemMessage": reason,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": reason,
                },
            }
        )


if __name__ == "__main__":
    main()
