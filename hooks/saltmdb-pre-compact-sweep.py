#!/usr/bin/env python3
"""SALTMDB Pre-Compact Sweep Hook Script
Lifecycle event: PreCompact (Claude-Code-only today -- confirmed absent from Antigravity's and
Copilot's own lifecycle event sets; AGENT_GUIDE.md §7).

Makes the sweep a real, standalone script instead of only existing as an inline "type": "agent"
prompt block in claude-settings-example.json. Claude Code's native "agent"-type PreCompact hook
(see that file) remains the best mechanism where it's available -- it runs as a proper subagent
with full SALTMDB tool access, no extra process needed. This script is the portable fallback: for
manual/cron invocation, for harnesses without a native agent-type hook, or for anyone who wants
the sweep runnable outside a hook event entirely. It cannot call MCP tools directly (a bare
script has no agent context) -- it shells out to whichever headless CLI-based agent is available,
carrying the exact same sweep prompt.
"""

import os
import shutil
import subprocess
import sys

SWEEP_PROMPT = (
    "You are the SALTMDB pre-compaction sweep hook. The current session's conversation "
    "transcript is about to be compacted and working context will be lost. Review the "
    "conversation so far for: unresolved decisions, root-cause fixes to bugs/issues, new "
    "architectural rules, or user preferences established in this session but NOT yet persisted "
    "to SALTMDB. For each item found, first call mcp__saltmdb__search_memory to confirm it is "
    "not already recorded, and if genuinely new, call mcp__saltmdb__store_memory "
    '(owner_id="agent_hook_precompact") to persist it, and mcp__saltmdb__log_event for any '
    "issue/fix worth a short-term event log entry. Do not report back conversationally -- this "
    "is a background sweep."
)

TIMEOUT_SECS = int(os.environ.get("SALTMDB_PRECOMPACT_TIMEOUT", "60"))


def run_quiet(cmd: list[str]) -> bool:
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=TIMEOUT_SECS,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> None:
    if shutil.which("claude"):
        if run_quiet(["claude", "-p", SWEEP_PROMPT]):
            return
    if shutil.which("codex"):
        if run_quiet(["codex", "exec", SWEEP_PROMPT]):
            return
    # No headless CLI-based agent available on PATH -- nothing this script can do without one
    # (storing memories requires an agent's own MCP tool context, not raw SQL). Fail silent/open.
    sys.exit(0)


if __name__ == "__main__":
    main()
