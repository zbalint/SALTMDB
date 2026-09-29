#!/usr/bin/env python3
"""SALTMDB Stop Self-Critique Gate Hook Script
Lifecycle event: Stop (Claude Code) / agentStop (Copilot)

Two bounded, sentinel-gated stages before a turn that touched files/commands is allowed to
close:
  Stage 1 (existing): mandatory self-reflection, questions loaded from QUESTIONS_CONFIG (see
           load_questions()) so the wording/count can change without touching this script.
  Stage 2 ("reflection-to-memory closer"): once Stage 1 is answered, require the answer to
           either become a store_memory call or an explicit one-line acknowledgment that no
           durable lesson applies -- without this, a genuine finding surfaced by Stage 1 can be
           verbalized and just evaporate, which is the actual hinge of the "learn from mistakes"
           goal this whole hook family serves.

Per-session fire cap (user directive, 2026-09-04): this hook stands in for a human periodically
asking "what are you unsure about" -- a human wouldn't do that on literally every risky tool
call, so Stage 1 is capped to a small, randomized number of distinct episodes per session (1-4,
rolled once per session_id and cached in state -- see get_or_roll_session_cap()). Once that many
Stage-1 episodes have been asked, the gate goes quiet for the rest of the session regardless of
further risky tool use. The cap counts distinct episodes (a prompt actually got emitted), not
raw retries of one still-unanswered prompt -- retrying an unanswered episode must not burn
additional cap budget, or a slow/distracted reply would silently exhaust the whole session's
budget on one unanswered question. Stage 2 (the store-or-opt-out follow-up) is never capped: it
isn't a new interruption, it's completing one already granted by its Stage-1 episode.

Deliberately NOT switched to the PostToolUse-flag-file mechanism the sibling
saltmdb-stop-retrieval-outcome-gate.py uses (which sidesteps transcript-format assumptions
entirely) -- out of scope for this change; the session-level cap here is pure counting logic
with no transcript-format dependency, so it is correct on every runtime regardless of that
gap. See memory for the cross-runtime transcript-shape caveat this still carries for the
underlying RISKY_TOOL_NAMES trigger scan itself (confirmed correct for Claude Code only).

Agent-agnostic output: emits both Claude Code's {"decision":"block"} schema and Copilot's
{"permissionDecision":...} schema in one payload. This fixes a confirmed bug: agentStop was
previously wired directly to a script that only emitted the decision:block schema, which
agentStop has no documented support for.
"""

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    RISKY_TOOL_NAMES,
    clear_state,
    emit,
    find_last_line_matching,
    get_field,
    get_session_id,
    prune_stale_state,
    read_count,
    read_stdin_json,
    read_transcript_from_line,
    state_dir,
    stop_block_payload,
    write_count,
)

MARKER = "saltmdb-self-critique-done"
PROMPT_SENTINEL = "saltmdb-stop-critique-prompt"
NO_LESSON_SENTINEL = "saltmdb-no-lesson-this-turn"
STAGE2_PROMPT_SENTINEL = "saltmdb-stop-critique-stage2-prompt"
STORE_MEMORY_PATTERN = re.compile(r'"name"\s*:\s*"[^"]*store_memory"')
QUESTIONS_CONFIG = Path(__file__).resolve().parent / "stop-critique-questions.json"
DEFAULT_QUESTIONS = [
    "What are you least confident about in what you just did?",
    "What's the biggest thing about this you probably haven't thought to ask?",
]
MIN_SESSION_CAP = 1
MAX_SESSION_CAP = 4
USER_TURN_PATTERN = re.compile(r'"type"\s*:\s*"user"')
# A transcript line tagged "type":"user" can be either a genuine human prompt OR a tool_result
# fed back to the model (confirmed for Claude Code's transcript format) -- exclude the latter so
# the turn-boundary scan below doesn't collapse to "since the last tool call" instead of "since
# the last real user prompt" (see memory 74a3b9a2 for the confirmed live bug this caused).
TOOL_RESULT_ECHO_PATTERN = re.compile(r'"tool_use_id"|"tool_call_id"')


def emit_block(data: dict, reason: str) -> None:
    emit(stop_block_payload(data, reason))
    sys.exit(0)


def load_questions() -> list[str]:
    """Read the question list from QUESTIONS_CONFIG. Falls back to DEFAULT_QUESTIONS on any
    missing/malformed config -- a typo in the JSON file must never crash this hook and block the
    agent outright."""
    try:
        raw = json.loads(QUESTIONS_CONFIG.read_text())
        questions = raw.get("questions")
        if (
            isinstance(questions, list)
            and questions
            and all(isinstance(q, str) and q.strip() for q in questions)
        ):
            return questions
    except (OSError, ValueError):
        pass
    return DEFAULT_QUESTIONS


def build_stage1_reason(questions: list[str]) -> str:
    numbered = " ".join(f"({i}) {q}" for i, q in enumerate(questions, start=1))
    noun = "question" if len(questions) == 1 else "questions"
    pronoun = "it" if len(questions) == 1 else "each"
    return (
        f"<!-- {PROMPT_SENTINEL} --> Before finishing, answer the following {noun} about the "
        f"work you just did this turn: {numbered} Begin your reply with the exact line "
        f"<!-- {MARKER} --> (so this check does not re-trigger), then answer {pronoun} concisely."
    )


def get_or_roll_session_cap(cap_state: Path) -> int:
    """Session-level Stage-1 fire cap, randomized once per session_id and cached on disk so
    every invocation this session agrees on the same number. read_count() returns 0 for a
    missing/unreadable file, which doubles as "not yet rolled" since a real cap is always >=1."""
    existing = read_count(cap_state)
    if existing > 0:
        return existing
    cap = random.randint(MIN_SESSION_CAP, MAX_SESSION_CAP)
    write_count(cap_state, cap)
    return cap


def find_last_user_line(transcript_path: str) -> int | None:
    path = Path(transcript_path)
    if not transcript_path or not path.is_file():
        return None
    last = None
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
        if USER_TURN_PATTERN.search(line) and not TOOL_RESULT_ECHO_PATTERN.search(line):
            last = i
    return last


def main() -> None:  # noqa: C901, PLR0912, PLR0915 -- Stage 1/Stage 2 episode state machine kept inline
    data = read_stdin_json()
    transcript_path = get_field(data, "transcript_path", "transcriptPath")
    session_id = get_session_id(data) or "unknown"

    if not transcript_path or not Path(transcript_path).is_file():
        sys.exit(0)

    prune_stale_state("stop-critique-*.count")
    prune_stale_state("stop-critique-*.stage2")
    prune_stale_state("stop-critique-*.session_fires")
    prune_stale_state("stop-critique-*.session_cap")
    count_state = state_dir() / f"stop-critique-{session_id}.count"
    stage2_state = state_dir() / f"stop-critique-{session_id}.stage2"
    session_fires_state = state_dir() / f"stop-critique-{session_id}.session_fires"
    session_cap_state = state_dir() / f"stop-critique-{session_id}.session_cap"

    last_user_line = find_last_user_line(transcript_path)
    last_prompt_line = find_last_line_matching(transcript_path, PROMPT_SENTINEL)
    last_stage2_prompt_line = find_last_line_matching(transcript_path, STAGE2_PROMPT_SENTINEL)

    if last_prompt_line is not None:
        window_start = last_prompt_line + 1
    elif last_user_line is not None:
        window_start = last_user_line
    else:
        window_start = None

    segment = (
        read_transcript_from_line(transcript_path, window_start)
        if window_start is not None
        else read_transcript_from_line(transcript_path, max(1, _line_count(transcript_path) - 400))
    )

    # New user turn since our last prompt: reset both stages, fall through to the Stage 1 check.
    if last_user_line is not None and (
        last_prompt_line is None or last_user_line > last_prompt_line
    ):
        clear_state(count_state, stage2_state)

    # Stage 1: check whether an already-issued prompt was answered BEFORE gating on whether a
    # risky tool call is present in this same segment -- the reply containing the marker is
    # often pure text with no further tool call, so requiring both in the same window would
    # silently pass the gate without ever detecting the answer.
    stage1_satisfied = last_prompt_line is not None and MARKER in segment

    if not stage1_satisfied:
        if not RISKY_TOOL_NAMES.search(segment):
            sys.exit(0)
        count = read_count(count_state)
        if count >= 2:
            clear_state(count_state)
            sys.exit(0)
        # Cap check only on a genuinely new episode (count 0 -> 1) -- a retry of one still-
        # unanswered episode (count 1 -> 2) must not consume additional session budget.
        if count == 0:
            cap = get_or_roll_session_cap(session_cap_state)
            fires_so_far = read_count(session_fires_state)
            if fires_so_far >= cap:
                sys.exit(0)
            write_count(session_fires_state, fires_so_far + 1)
        write_count(count_state, count + 1)
        emit_block(data, build_stage1_reason(load_questions()))

    # Stage 1 satisfied. Stage 2: did the reflection turn into a stored memory, or an explicit
    # opt-out?
    marker_line = find_last_line_matching(transcript_path, MARKER)
    after_marker = read_transcript_from_line(transcript_path, marker_line) if marker_line else ""

    if STORE_MEMORY_PATTERN.search(after_marker) or NO_LESSON_SENTINEL in after_marker:
        clear_state(count_state, stage2_state)
        sys.exit(0)

    # Already asked once for stage 2 in this window and still nothing -- let it go rather than
    # loop.
    if (
        last_stage2_prompt_line is not None
        and marker_line is not None
        and last_stage2_prompt_line > marker_line
    ):
        clear_state(count_state, stage2_state)
        sys.exit(0)

    stage2_count = read_count(stage2_state)
    if stage2_count >= 1:
        clear_state(stage2_state)
        sys.exit(0)
    write_count(stage2_state, 1)

    emit_block(
        data,
        f"<!-- {STAGE2_PROMPT_SENTINEL} --> You just answered the self-critique questions. If "
        "that reflection surfaced a genuine, durable lesson (a bug root cause, a rule worth "
        "remembering, a constraint you didn't know before), call store_memory for it now -- a "
        "reflection that isn't captured just evaporates. If nothing durable came out of it, say "
        f"so explicitly by including the exact line <!-- {NO_LESSON_SENTINEL} --> in your reply.",
    )


def _line_count(transcript_path: str) -> int:
    return len(Path(transcript_path).read_text(errors="replace").splitlines())


if __name__ == "__main__":
    main()
