#!/usr/bin/env python3
"""SALTMDB Checkable-Fact Drift Sweep Script
Lifecycle event: Manual / cron only (no lifecycle event).

Periodic verification of checkable fact memories against live repository source code to detect
and flag stale references (e.g. file:line citations or renamed symbols) without destroying original
memories.

Cannot call MCP tools directly (a bare script has no agent context) -- it shells out to whichever
headless CLI-based agent is available (claude -p or codex exec), carrying the drift sweep prompt.
"""

import os
import shutil
import sys

from _saltmdb_hook_common import run_quiet

DRIFT_SWEEP_PROMPT = (
    "You are the SALTMDB checkable-fact drift sweep agent. Perform a periodic sweep of "
    "memories that cite repository source code to detect stale or drifted claims.\n\n"
    "Execute the following procedure using your mcp__saltmdb__* and Read/Grep tools:\n"
    "1. Mine: Call mcp__saltmdb__search_memory (mode='broad') for candidate memories likely "
    "to cite a repo file:line reference or a named code constant/function/class, capped at roughly "
    "50 candidates per run. Skip any candidate that already carries a 'drift_flag' in its metadata "
    "unless that flag's 'flagged_at' timestamp is more than 30 days old.\n"
    "2. Extract: For each candidate, extract the specific checkable claim (the file:line or named symbol "
    "and what it is claimed to say or be).\n"
    "3. Re-verify: Re-verify each claim against the ACTUAL current repository source using Read/Grep tools "
    "-- never trust the memory's own claim.\n"
    "4. Flag: For each genuinely drifted memory, call mcp__saltmdb__get_memory first to fetch its current "
    "title, content, and tags verbatim. Then call mcp__saltmdb__store_memory with entity_id=<id>, "
    "title=<unchanged>, content=<unchanged>, tags=<unchanged>, and "
    "metadata={'drift_flag': {'reason': '<one sentence: what changed>', 'cited_ref': '<the file:line or symbol that drifted>', "
    "'flagged_at': '<ISO 8601 UTC timestamp>', 'flagged_by': 'agent_hook_driftsweep'}}. "
    "Never call consolidate_memories, revise_memory, or supersede_memory as part of flagging, and never change "
    "title, content, or tags on the flagged memory.\n"
    "5. Record: Store one summary memory via mcp__saltmdb__store_memory ("
    "memory_type='fact') titled '[SALTMDB Drift Sweep] <ISO date> -- N checked, M flagged', listing every "
    "flagged memory's id, title, cited_ref, and reason in the content, or a short 'no drift found' memory if M=0. "
    "Do not report back conversationally -- background sweep, quiet exit."
)

TIMEOUT_SECS = int(os.environ.get("SALTMDB_DRIFTSWEEP_TIMEOUT", "900"))


def main() -> None:
    if shutil.which("claude"):
        if run_quiet(["claude", "-p", DRIFT_SWEEP_PROMPT], TIMEOUT_SECS):
            return
    if shutil.which("codex"):
        if run_quiet(["codex", "exec", DRIFT_SWEEP_PROMPT], TIMEOUT_SECS):
            return
    # No headless CLI-based agent available on PATH -- nothing this script can do without one
    # (storing memories requires an agent's own MCP tool context, not raw SQL). Fail silent/open.
    sys.exit(0)


if __name__ == "__main__":
    main()
