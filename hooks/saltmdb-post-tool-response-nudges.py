#!/usr/bin/env python3
"""SALTMDB Post-Tool Response Nudges Hook Script
Lifecycle event: PostToolUse (Claude Code, Antigravity) / postToolUse (Copilot, if supported)
Register for: mcp__saltmdb__store_memory, mcp__saltmdb__search_memory

Inspects a SALTMDB tool call's *response*, not just its name -- a capability none of the
pre-existing hooks used. Consolidated into one script (rather than three near-identical ones)
because all three nudges share the same input-parsing/response-inspection plumbing; each is
independently gated below.

  1. Near-duplicate follow-through: store_memory's response carried duplicate_candidates but
     nothing required the agent to act on it beyond eyeballing (this happened for real: memory
     798e6bc9 stored with 21 duplicate candidates flagged, none acted on).
  2. Unlinked-memory nudge: a store_memory call this turn with no matching manage_relation call
     afterward -- enforces CLAUDE.md's "proactively link every durable memory" rule, which is
     currently unenforced prose.
  3. Empty-strict-result nudge: search_memory(mode="strict") returned [] -- a valid abstention,
     not an error, but nothing prompts trying broad mode or confirming genuinely new territory.

Also sets the retrieval-outcome-pending flag (see _saltmdb_hook_common.retrieval_outcome_flag_path)
on every search_memory call, for saltmdb-stop-retrieval-outcome-gate.py to check at Stop --
cleared by saltmdb-post-tool-failure-circuit-breaker.py on a matching log_event(retrieval_outcome)
call. Structured PostToolUse tool_name/tool_input tracking, not transcript scanning -- see that
gate script's docstring for why.

Agent-agnostic output: non-blocking in all three cases (a nudge, not a gate) -- a PostToolUse
hook has already-committed side effects to react to, not a decision to gate.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    emit,
    get_field,
    get_session_id,
    get_tool_name,
    read_stdin_json,
    read_transcript,
    retrieval_outcome_flag_path,
    search_memory_called_flag_path,
    write_count,
)

MAX_RESPONSE_TEXT_BYTES = 1024 * 1024
MANAGE_RELATION_PATTERN = "manage_relation"
STORE_MEMORY_PATTERN = "store_memory"
# Fallback only when the response cannot be parsed as JSON; require one object in the array.
DUPLICATE_FALLBACK_PATTERN = re.compile(r'"duplicate_candidates"\s*:\s*\[\s*\{')
NEAR_DUPLICATE_PATTERN = re.compile(r'"NEAR_DUPLICATE"')


def emit_nudge(reason: str) -> None:
    emit(
        {
            "systemMessage": reason,
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason},
        }
    )
    sys.exit(0)


def response_text(data: dict[str, Any]) -> str:
    """tool_response can be a nested object (Claude Code) or a raw JSON string depending on
    harness -- try both: first as a native field, then fall back to the whole input."""
    for field in ("tool_response", "toolResponse", "response"):
        val = data.get(field)
        if val:
            return json.dumps(val) if isinstance(val, (dict, list)) else str(val)
    return json.dumps(data)


def _content_block_object(value: list[Any]) -> dict[str, Any] | None:
    text = "".join(
        block.get("text", "")
        for block in value
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    if not text or len(text.encode("utf-8")) > MAX_RESPONSE_TEXT_BYTES:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _response_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return _content_block_object(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def response_object(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the parsed tool response as a dict when available, else None."""
    for field in ("tool_response", "toolResponse", "response"):
        parsed = _response_value(data.get(field))
        if parsed is not None:
            return parsed
    return None


def has_real_duplicate_candidates(data: dict[str, Any], resp_text: str) -> bool:
    """True only when duplicate_candidates is a genuinely non-empty list (or the NEAR_DUPLICATE
    warning code is present) -- not just because the response's always-present
    `"duplicate_candidates": []` key was matched as a bare substring."""
    obj = response_object(data)
    if isinstance(obj, dict):
        nested_value = obj.get("data")
        nested_candidates = (
            nested_value.get("duplicate_candidates") if isinstance(nested_value, dict) else None
        )
        for candidates in (obj.get("duplicate_candidates"), nested_candidates):
            if isinstance(candidates, list) and candidates:
                return True
        return False
    # Couldn't parse the response as an object -- fall back to a tightened regex that requires
    # at least one object inside the array, rather than matching the bare (always-present) key.
    return bool(
        DUPLICATE_FALLBACK_PATTERN.search(resp_text) or NEAR_DUPLICATE_PATTERN.search(resp_text)
    )


def handle_store_memory(data: dict[str, Any], resp_text: str, transcript_path: str) -> None:
    if has_real_duplicate_candidates(data, resp_text):
        emit_nudge(
            "SALTMDB nudge: this store_memory call returned duplicate_candidates. Before "
            "moving on, either call supersede_memory (one clear replacement) or "
            "consolidate_memories (merge several) for the genuinely overlapping ones, or "
            "explicitly note why none apply -- don't just note the warning and continue."
        )

    text = read_transcript(transcript_path)
    if text is None:
        return
    lines = text.splitlines()
    last_store_line = None
    for i, line in enumerate(lines, start=1):
        if STORE_MEMORY_PATTERN in line:
            last_store_line = i
    if last_store_line is None:
        return
    after = "\n".join(lines[last_store_line - 1 :])
    if MANAGE_RELATION_PATTERN not in after:
        emit_nudge(
            "SALTMDB nudge: a memory was just stored with no manage_relation call after it. Per "
            "the memory-quality standard, proactively link every durable memory to its "
            "meaningful context (decision/plan/issue/evidence it relates to) unless no "
            "meaningful connection genuinely exists."
        )


def handle_search_memory(data: dict[str, Any], resp_text: str, session_id: str) -> None:
    # Both flags first, unconditionally -- the empty-strict-result nudge below is allowed to
    # exit early via emit_nudge(), and both flags must be set regardless of whether that nudge
    # fires. search_memory_called_flag_path is the pre-tool search gate's primary "has
    # search_memory ever been called this session" signal (see its docstring for why it exists
    # independently of retrieval_outcome_flag_path, which tracks a different, clearable thing).
    write_count(retrieval_outcome_flag_path(session_id), 1)
    write_count(search_memory_called_flag_path(session_id), 1)

    input_text = json.dumps(data)
    if '"mode"' in input_text and '"strict"' in input_text and "NO_MATCH" in resp_text:
        emit_nudge(
            'SALTMDB nudge: search_memory(mode="strict") returned an empty data.items page with '
            "a NO_MATCH warning. This is a valid abstention, not an error -- but before treating "
            "this as 'nothing exists', consider retrying with mode=\"broad\" or confirming this is "
            "genuinely new territory rather than a phrasing mismatch."
        )


def main() -> None:
    data = read_stdin_json()
    tool_name = get_tool_name(data)
    transcript_path = get_field(data, "transcript_path", "transcriptPath")
    session_id = get_session_id(data) or "unknown"
    resp_text = response_text(data)

    if tool_name.endswith("store_memory"):
        handle_store_memory(data, resp_text, transcript_path)
    elif tool_name.endswith("search_memory"):
        handle_search_memory(data, resp_text, session_id)


if __name__ == "__main__":
    main()
