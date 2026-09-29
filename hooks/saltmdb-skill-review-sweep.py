#!/usr/bin/env python3
"""SALTMDB Skill-Review Sweep Script
Lifecycle event: Manual / cron only (no lifecycle event).

Mining, diagnosing, and proposing skill and hook improvements is a low-frequency operation
(e.g. run every few weeks or when telemetry volume is meaningful). Overfitting a single bad
session to a permanent skill/hook edit is a key failure mode this design deliberately avoids,
which is why this script is manual/cron-invocation only rather than being wired to a per-turn
or per-session lifecycle event.

Cannot call MCP tools directly (a bare script has no agent context) -- it shells out to whichever
headless CLI-based agent is available (claude -p or codex exec), carrying the review sweep prompt.
"""

import os
import shutil
import sys

from _saltmdb_hook_common import run_quiet

REVIEW_PROMPT = (
    "You are the SALTMDB skill-review sweep agent. Perform a low-frequency review of "
    "accumulated failure and retrieval telemetry to propose skill or hook improvements.\n\n"
    "Before mining, check for the most recent prior '[SALTMDB Skill-Review Sweep]' memory via "
    "mcp__saltmdb__search_memory. If one exists, only mine events created after that date.\n\n"
    "Execute the following 5-step procedure using your mcp__saltmdb__* tools:\n"
    "1. Mine: Call mcp__saltmdb__get_events for event_type='issue' and separately for "
    "event_type='retrieval_outcome' (order='oldest_first', limit 300 each). Discard events "
    "that read as synthetic or unrelated-domain noise by evaluating event content rather than "
    "relying on agent_id blocklists.\n"
    "2. Diagnose: Group remaining events by the hook, skill, tool, or component they implicate "
    "(e.g. files under hooks/ or skills/, or specific MCP tool names). Only treat a group as a "
    "real pattern if it contains at least 2 corroborating events. For each real pattern, write "
    "a short causal diagnosis of why the failure occurred.\n"
    "3. Pair-check: Count total retrieval_outcome events found. If fewer than 20 total events, "
    "explicitly skip pairing and label every finding as 'single-diagnosis, unpaired -- lower "
    "confidence' rather than fabricating a comparison. If 20 or more events, compare failing "
    "pattern clusters against similar succeeding events to sharpen the diagnosis.\n"
    "4. Propose: For each pattern with a specific, actionable diagnosis, draft a minimal, "
    "surgical text diff (a few changed/added lines, never a full rewrite) targeting the single "
    "implicated file. Hard constraint: do not call any file-editing tool (never auto-apply file "
    "edits) -- the proposed diff is text output only, to be embedded in the memory in step 5.\n"
    "5. Gate: Call mcp__saltmdb__store_memory (owner_id='agent_hook_skillreview', "
    "memory_type='fact') to record findings as a single memory titled "
    "'[SALTMDB Skill-Review Sweep] <ISO date> -- N pattern(s) found, M diff(s) proposed' (or '0 "
    "patterns found' if nothing qualified). Include mined event counts, date range, causal "
    "diagnoses, proposed diffs verbatim, and confidence level (paired vs unpaired). "
    "No file may ever be edited or committed by this sweep. If no new patterns are found, store "
    "a short memory recording that no new lessons were identified. Do not report back "
    "conversationally -- background sweep, quiet exit."
)

TIMEOUT_SECS = int(os.environ.get("SALTMDB_SKILLREVIEW_TIMEOUT", "600"))


def main() -> None:
    if shutil.which("claude"):
        if run_quiet(["claude", "-p", REVIEW_PROMPT], TIMEOUT_SECS):
            return
    if shutil.which("codex"):
        if run_quiet(["codex", "exec", REVIEW_PROMPT], TIMEOUT_SECS):
            return
    # No headless CLI-based agent available on PATH -- nothing this script can do without one
    # (storing memories requires an agent's own MCP tool context, not raw SQL). Fail silent/open.
    sys.exit(0)


if __name__ == "__main__":
    main()
