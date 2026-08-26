#!/usr/bin/env python3
"""SALTMDB Session Wrap-Up Reminder Hook Script
Lifecycle event: SessionEnd (Claude Code) -- true session close, not every Stop/turn end.

AGENT_GUIDE.md Phase C ("Session Wrap-up: Commit & Link") is a manual checklist today. This is
pure automation, no judgment call: a fixed reminder to check get_events(order="oldest_first") for
anything durable that only exists in the ephemeral event ledger before the session closes for
good.

Best-effort by design: not all harnesses distinguish a true session close from an ordinary
turn-level Stop, and a hook firing at session close may have nothing left to act on (the session
is already ending) -- register this only where a genuine SessionEnd-class event exists; don't
wire it to a harness's plain Stop event, which saltmdb-stop-critique-gate.py and
saltmdb-stop-retrieval-outcome-gate.py already cover per-turn.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import emit  # noqa: E402

REASON = (
    "SALTMDB session wrap-up reminder: before this session closes, check "
    "get_events(context_id=<your thread handle>) or get_events(agent_id=<configured SALTMDB_OWNER_ID>, "
    'order="oldest_first") for anything durable (a decision, a fix, a rule) that was logged as '
    "an event this session but never promoted to store_memory. The event ledger is not itself "
    "long-term memory."
)


def main() -> None:
    # SessionEnd has no next model turn to receive context into, so only the top-level
    # systemMessage field is valid here -- hookSpecificOutput.additionalContext is documented
    # for PreToolUse/UserPromptSubmit/PostToolUse/PostToolBatch/Stop/SubagentStop only and fails
    # schema validation for SessionEnd.
    emit({"systemMessage": REASON})


if __name__ == "__main__":
    main()
