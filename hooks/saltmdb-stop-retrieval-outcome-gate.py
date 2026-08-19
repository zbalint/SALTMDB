#!/usr/bin/env python3
"""SALTMDB Stop Retrieval-Outcome Gate Hook Script
Lifecycle event: Stop (Claude Code) / agentStop (Copilot)

Did search_memory get called this turn with no retrieval_outcome logged afterward? Same
transcript-scan + sentinel-marker-cap shape as saltmdb-stop-critique-gate.py, registered as a
separate hook entry (single responsibility per script) rather than folded into that one.

This is the enforcement half of the telemetry convention: agents call
log_event(event_type="retrieval_outcome", content="<memory_id>: used|irrelevant|insufficient --
<why>") after acting on search results, documented in the saltmdb-usage skill. Pure observation
-- this hook never touches ranking/decay/authority, it only nudges the log call.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    clear_state,
    emit,
    find_last_line_matching,
    get_field,
    prune_stale_state,
    read_count,
    read_stdin_json,
    read_transcript_from_line,
    state_dir,
    write_count,
)

PROMPT_SENTINEL = "saltmdb-retrieval-outcome-prompt"
ACK_SENTINEL = "saltmdb-retrieval-outcome-logged"
SEARCH_MEMORY_PATTERN = re.compile(r'"name"\s*:\s*"[^"]*search_memory"')
RETRIEVAL_OUTCOME_PATTERN = re.compile(r'"event_type"\s*:\s*"retrieval_outcome"')


def main() -> None:
    data = read_stdin_json()
    transcript_path = get_field(data, "transcript_path", "transcriptPath")
    session_id = get_field(data, "session_id", "sessionId") or "unknown"

    if not transcript_path or not Path(transcript_path).is_file():
        sys.exit(0)

    prune_stale_state("retrieval-outcome-*.count")
    state_file = state_dir() / f"retrieval-outcome-{session_id}.count"

    last_user_line = None
    lines = Path(transcript_path).read_text(errors="replace").splitlines()
    for i, line in enumerate(lines, start=1):
        if '"type"' in line and '"user"' in line:
            last_user_line = i
    last_prompt_line = find_last_line_matching(transcript_path, PROMPT_SENTINEL)

    window_start = None
    if last_prompt_line is not None and (
        last_user_line is None or last_prompt_line >= last_user_line
    ):
        window_start = last_prompt_line + 1
    elif last_user_line is not None:
        window_start = last_user_line
        clear_state(state_file)

    segment = (
        read_transcript_from_line(transcript_path, window_start)
        if window_start is not None
        else "\n".join(lines[-400:])
    )

    # Nothing to report on if search_memory wasn't called this turn.
    if not SEARCH_MEMORY_PATTERN.search(segment):
        sys.exit(0)

    # Already logged an outcome, or explicitly acknowledged nothing applies -- satisfied.
    if RETRIEVAL_OUTCOME_PATTERN.search(segment) or ACK_SENTINEL in segment:
        clear_state(state_file)
        sys.exit(0)

    count = read_count(state_file)
    if count >= 1:
        clear_state(state_file)
        sys.exit(0)
    write_count(state_file, 1)

    reason = (
        f"<!-- {PROMPT_SENTINEL} --> search_memory was called this turn but no retrieval "
        'outcome was logged. Call log_event(event_type="retrieval_outcome", '
        'content="<memory_id>: used|irrelevant|insufficient -- <why>") for the results you '
        "acted on (or didn't), or if none of this turn's searches are worth reporting on, "
        f"include the exact line <!-- {ACK_SENTINEL} -->."
    )
    emit(
        {
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
    )


if __name__ == "__main__":
    main()
