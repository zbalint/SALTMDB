#!/usr/bin/env python3
"""SALTMDB Session Bootstrap Hook Script
Lifecycle event: SessionStart (Claude Code) / PreInvocation (Antigravity) / sessionStart (Copilot)

Prints the canonical core-memory bootstrap digest, then a one-line nudge if any core memory is
overdue for review (SALTMDB rollout task 8: overdue cores otherwise only surface as a hard error
the first time an agent happens to touch store_memory/manage_relation/consolidate_memories while
one is overdue -- this surfaces it proactively instead).

Claude Code and Codex accept injected context as plain stdout. Antigravity's documented
PreInvocation contract instead requires an ``injectSteps`` JSON response, so the same computed
digest is serialized to that native shape when its camelCase payload is present. Python instead
of bash for this whole hook family keeps the parsing dependency-free and robust.
"""

import json
import sys
import time
from pathlib import Path

START = time.monotonic()
BUDGET_S = 10.0

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    HOOK_OUTPUT_MAX_CHARS,
    OVERDUE_RESERVE_CHARS,
    cap_hook_output,
    clear_state,
    digest_pending_path,
    get_session_id,
    prune_stale_state,
    read_digest_pending,
    resolve_cli,
    run_cli_bounded,
    session_digest_args,
    write_digest_pending,
)

CLI = resolve_cli()
STARTED_AT = time.time()


def remaining() -> float:
    return BUDGET_S - (time.monotonic() - START)


def run_cli(*args: str) -> str | None:
    if not CLI:
        return None
    return run_cli_bounded(CLI, args, remaining())


def bootstrap_text(
    source: str | None = None,
    missing: list[str] | None = None,
    *,
    capped: bool = True,
) -> str:
    sections: list[str] = []
    digest = run_cli("bootstrap-digest")
    if digest:
        core_section = digest.strip()
    else:
        core_section = ""
        if CLI:
            if missing is not None:
                missing.append("bootstrap-digest")
            core_section = "\n".join(
                [
                    "<saltmdb-digest-pending>",
                    (
                        "If no <saltmdb-digest> block has appeared by your first reply, run "
                        f"{CLI} bootstrap-digest with Bash and read the result before continuing."
                    ),
                    "</saltmdb-digest-pending>",
                ]
            )

    # The last-session handover is large and the session already holds it from startup; a
    # compaction only needs the core digest back, in case the built-in summarizer dropped it.
    session_digest = (
        None
        if source == "compact"
        else run_cli(
            *session_digest_args(
                len(core_section), OVERDUE_RESERVE_CHARS, STARTED_AT, capped=capped
            )
        )
    )
    session_section = ""
    if session_digest:
        session_section = session_digest.strip()
    elif not session_digest and source != "compact" and CLI and missing is not None:
        missing.append("session-digest")

    health_raw = run_cli("corpus-health")
    overdue_section = ""
    if health_raw:
        try:
            health = json.loads(health_raw)
        except json.JSONDecodeError:
            health = {}
        overdue = health.get("overdue_core_reviews", {})
        count = overdue.get("count", 0)
        if count:
            titles = [e.get("title", "") for e in overdue.get("entries", [])[:5] if e.get("title")]
            overdue_section = "\n".join(
                [
                    "<saltmdb-overdue-core-notice>",
                    f"{count} active core memory review(s) are overdue (core_review_after elapsed).",
                    *(f"- {title}" for title in titles),
                    (
                        "store_memory/manage_relation/consolidate_memories will hard-block until these are "
                        "reviewed via review_core_memory (outcome='demote' or 'archive'). Handle this before "
                        "it blocks unrelated work."
                    ),
                    "</saltmdb-overdue-core-notice>",
                ]
            )

    sections.extend(
        section for section in (core_section, overdue_section, session_section) if section
    )
    return "\n\n".join(sections)


def _pending_parts(expected: list[str], marker: dict[str, object] | None) -> list[str]:
    previous = marker.get("missing", []) if marker else []
    if not isinstance(previous, list):
        previous = []
    return [
        part
        for part in ("bootstrap-digest", "session-digest")
        if part in expected or part in previous
    ]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}

    session_id = get_session_id(payload)
    pending_path = digest_pending_path(session_id) if session_id and CLI else None
    expected = ["bootstrap-digest"]
    if payload.get("source") != "compact":
        expected.append("session-digest")

    if pending_path is not None:
        prune_stale_state("digest-pending-*", max_age_days=1)
        pending_before = _pending_parts(expected, read_digest_pending(pending_path))
        write_digest_pending(
            pending_path,
            pending_before,
            attempts=0,
            running_since=time.time(),
            started_at=STARTED_AT,
        )

    missing: list[str] = []
    text = bootstrap_text(payload.get("source"), missing, capped=not payload.get("conversationId"))
    if text:
        if payload.get("conversationId"):
            json.dump({"injectSteps": [{"ephemeralMessage": text}]}, sys.stdout)
            print()
        else:
            output = cap_hook_output(text, CLI)
            sys.stdout.write(output)
            if not output.endswith("\n") and len(output) < HOOK_OUTPUT_MAX_CHARS:
                print()
    sys.stdout.flush()

    if pending_path is None:
        return

    claimed_path = Path(f"{pending_path}.claimed")
    current = read_digest_pending(pending_path)
    claimed = read_digest_pending(claimed_path)
    union = _pending_parts(pending_before, current)
    union = _pending_parts(union, claimed)
    fetched = [part for part in expected if part not in missing]
    remaining_parts = [part for part in union if part not in fetched]
    if remaining_parts:
        # shortcut: the retry hook can race this final reconciliation by a few milliseconds;
        # retaining the union prevents that failure write from being lost.
        write_digest_pending(
            pending_path,
            remaining_parts,
            attempts=0,
            running_since=None,
            started_at=STARTED_AT,
        )
        clear_state(claimed_path)
    else:
        clear_state(pending_path, claimed_path)


if __name__ == "__main__":
    main()
