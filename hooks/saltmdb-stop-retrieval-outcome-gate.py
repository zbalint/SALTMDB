#!/usr/bin/env python3
"""SALTMDB Stop Retrieval-Outcome Gate Hook Script
Lifecycle event: Stop (Claude Code) / agentStop (Copilot)

Did search_memory get called this turn with no retrieval_outcome logged afterward? This is the
enforcement half of the telemetry convention: agents call
log_event(event_type="retrieval_outcome", content="<memory_id>: used|irrelevant|insufficient --
<why>") after acting on search results, documented in the saltmdb-usage skill. Pure observation
-- this hook never touches ranking/decay/authority, it only nudges the log call.

Checks a per-session pending flag (_saltmdb_hook_common.retrieval_outcome_flag_path) instead of
scanning the transcript for a turn boundary. Earlier versions of this script (and the sibling
saltmdb-stop-critique-gate.py) tried to find "where did this turn start" by scanning the
transcript JSONL for the last line matching `"type":"user"` -- but harnesses (confirmed for
Claude Code) also tag tool_result feedback messages as `"type":"user"`, so that scan almost
always landed on the most recent tool call's result instead of the actual last human prompt,
collapsing the detection window down to just the tail after the last tool call in the turn. That
silently excluded the search_memory invocation itself whenever any further tool call followed it
in the same turn (the common case) -- confirmed live: this hook had never fired once, on any real
session, despite search_memory being called with no retrieval_outcome logged many times (see
memory 74a3b9a2).

The flag-file design sidesteps transcript-format guessing entirely: saltmdb-post-tool-response-
nudges.py sets the flag on a search_memory call and saltmdb-post-tool-failure-circuit-breaker.py
clears it on a matching log_event(retrieval_outcome) call, both using PostToolUse's structured
tool_name/tool_input (not raw transcript text) -- a mechanism that works identically regardless
of what a given harness's transcript JSONL happens to look like.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    clear_state,
    emit,
    get_field,
    prune_stale_state,
    read_count,
    read_stdin_json,
    retrieval_outcome_flag_path,
    write_count,
)

PROMPT_SENTINEL = "saltmdb-retrieval-outcome-prompt"


def main() -> None:
    data = read_stdin_json()
    session_id = get_field(data, "session_id", "sessionId") or "unknown"

    prune_stale_state("retrieval-outcome-pending-*.flag")
    flag_file = retrieval_outcome_flag_path(session_id)

    # 0/absent = nothing pending. 1 = pending, not yet nudged. 2 = already nudged once and still
    # not resolved -- let it go rather than block every subsequent Stop forever.
    state = read_count(flag_file)
    if state <= 0:
        sys.exit(0)
    if state >= 2:
        clear_state(flag_file)
        sys.exit(0)

    write_count(flag_file, 2)
    reason = (
        f"<!-- {PROMPT_SENTINEL} --> search_memory was called this turn but no retrieval "
        'outcome was logged. Call log_event(event_type="retrieval_outcome", '
        'content="<memory_id>: used|irrelevant|insufficient -- <why>") for the results you '
        "acted on (or didn't)."
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
