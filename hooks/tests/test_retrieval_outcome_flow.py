"""Regression tests for the two live-only bugs fixed together (see memory 74a3b9a2):

1. saltmdb-stop-critique-gate.py's find_last_user_line() matched tool_result echo lines (also
   tagged "type":"user" in Claude Code's real transcript format) as if they were the last human
   prompt, collapsing its turn-boundary detection to just the tail after the last tool call.
2. saltmdb-stop-retrieval-outcome-gate.py had the identical bug (inlined) and, as a consequence,
   had never fired once on any real session despite search_memory being called with no
   retrieval_outcome logged many times over. It's been rewritten to track a per-session pending
   flag via structured PostToolUse tool_name/tool_input instead of scanning transcript text at
   all -- these tests cover that flow end-to-end, invoking the actual hook scripts as subprocesses
   (stdin JSON in, stdout JSON out) for the same fidelity a real harness invocation has.

The fixture at fixtures/real_session_search_memory_turn.jsonl is a synthetic structural
reproduction of the transcript shape that surfaced bug #2. It deliberately includes several
tool-result records tagged as ``type=user`` after the last human prompt, which is the minimum
shape needed to keep the original regression covered without publishing a real session.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "real_session_search_memory_turn.jsonl"


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook(script: str, payload: dict, home: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / f"{script}.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        timeout=15,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr}"
    out = result.stdout.strip()
    return json.loads(out) if out else {}


def _flag_file(home: Path, session_id: str) -> Path:
    return home / ".claude" / "hooks" / ".state" / f"retrieval-outcome-pending-{session_id}.flag"


# --- Bug #1: stop-critique-gate turn-boundary detection ---------------------------------------


def test_critique_gate_finds_real_prompt_not_tool_result_echo():
    critique_gate = _load_module("saltmdb-stop-critique-gate")

    last_line = critique_gate.find_last_user_line(str(FIXTURE))

    lines = FIXTURE.read_text().splitlines()
    # Line 8 (1-indexed) is the synthetic human prompt in this fixture; later lines tagged
    # "type":"user" are tool_result echoes carrying tool_use_id, which must be excluded.
    assert last_line == 8
    assert '"tool_use_id"' not in lines[last_line - 1]


def test_naive_pattern_would_have_picked_a_tool_result_line():
    """Documents the regression this fixture catches: the OLD pattern (no tool_use_id exclusion)
    resolves to a tool_result line, not the real prompt -- proving the fixture actually exercises
    the bug, not just a case where both approaches happen to agree."""
    import re

    naive_pattern = re.compile(r'"type"\s*:\s*"user"')
    lines = FIXTURE.read_text().splitlines()
    naive_last = None
    for i, line in enumerate(lines, start=1):
        if naive_pattern.search(line):
            naive_last = i

    assert naive_last != 8
    assert '"tool_use_id"' in lines[naive_last - 1]


# --- Bug #2: retrieval-outcome-gate end-to-end flag flow --------------------------------------


def test_gate_silent_when_nothing_pending(tmp_path):
    result = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate",
        {"session_id": "sess-a"},
        home=tmp_path,
    )
    assert result == {}


def test_search_memory_then_stop_blocks_then_outcome_clears_it(tmp_path):
    session_id = "sess-b"

    # 1. search_memory PostToolUse call sets the pending flag.
    nudge_result = _run_hook(
        "saltmdb-post-tool-response-nudges",
        {
            "tool_name": "mcp__saltmdb__search_memory",
            "session_id": session_id,
            "tool_input": {"query_keywords": "anything", "mode": "broad"},
            "tool_response": {"result": [{"id": "abc123"}]},
        },
        home=tmp_path,
    )
    assert _flag_file(tmp_path, session_id).exists()
    # Broad-mode, non-empty result: no nudge expected from this call itself.
    assert nudge_result == {}

    # 2. Stop fires: flag is pending -> blocks.
    stop_result = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate",
        {"session_id": session_id},
        home=tmp_path,
    )
    assert stop_result.get("decision") == "block"
    assert "retrieval" in stop_result["reason"].lower()

    # 3. Agent logs the outcome: circuit-breaker script clears the flag.
    _run_hook(
        "saltmdb-post-tool-failure-circuit-breaker",
        {
            "tool_name": "mcp__saltmdb__log_event",
            "session_id": session_id,
            "tool_input": {
                "event_type": "retrieval_outcome",
                "content": "abc123: used -- answered the question directly",
            },
        },
        home=tmp_path,
    )
    assert not _flag_file(tmp_path, session_id).exists()

    # 4. Stop fires again: nothing pending -> silent.
    stop_result_2 = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate",
        {"session_id": session_id},
        home=tmp_path,
    )
    assert stop_result_2 == {}


def test_unresolved_pending_flag_is_let_go_after_one_nudge(tmp_path):
    """Mirrors the pre-existing count>=1-then-clear ergonomic: don't block every Stop forever if
    the agent never logs an outcome -- nudge once, then let it go."""
    session_id = "sess-c"
    _run_hook(
        "saltmdb-post-tool-response-nudges",
        {
            "tool_name": "mcp__saltmdb__search_memory",
            "session_id": session_id,
            "tool_input": {"query_keywords": "x"},
            "tool_response": {"result": []},
        },
        home=tmp_path,
    )

    first = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate", {"session_id": session_id}, home=tmp_path
    )
    assert first.get("decision") == "block"

    second = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate", {"session_id": session_id}, home=tmp_path
    )
    assert second == {}
    assert not _flag_file(tmp_path, session_id).exists()


def test_codex_stop_output_uses_strict_schema(tmp_path):
    """Codex rejects the cross-harness compatibility keys because its Stop output schema sets
    additionalProperties=false. A Codex payload is identifiable by required turn_id+model fields
    and must receive only the supported decision/reason pair."""
    session_id = "sess-codex"
    _run_hook(
        "saltmdb-post-tool-response-nudges",
        {
            "tool_name": "mcp__saltmdb__search_memory",
            "session_id": session_id,
            "tool_input": {"query_keywords": "x"},
            "tool_response": {"result": []},
        },
        home=tmp_path,
    )

    result = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate",
        {
            "session_id": session_id,
            "turn_id": "turn-123",
            "model": "gpt-test",
            "hook_event_name": "Stop",
        },
        home=tmp_path,
    )

    assert result["decision"] == "block"
    assert set(result) == {"decision", "reason"}


def test_sessions_do_not_share_pending_state(tmp_path):
    _run_hook(
        "saltmdb-post-tool-response-nudges",
        {
            "tool_name": "mcp__saltmdb__search_memory",
            "session_id": "sess-d1",
            "tool_input": {"query_keywords": "x"},
            "tool_response": {"result": []},
        },
        home=tmp_path,
    )

    other_session_result = _run_hook(
        "saltmdb-stop-retrieval-outcome-gate", {"session_id": "sess-d2"}, home=tmp_path
    )
    assert other_session_result == {}
