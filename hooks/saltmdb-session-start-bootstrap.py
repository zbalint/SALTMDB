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
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_cli() -> str | None:
    """Locate the saltmdb-cli executable without assuming any one install layout -- SALTMDB can be
    installed anywhere (a venv the user chose, pipx, a global install), not just the
    ``~/.mcp/SALTMDB`` layout this project's own maintainer happens to use. Precedence, most
    explicit first: (1) ``SALTMDB_CLI_PATH`` env var, for a hook subprocess whose PATH doesn't
    carry the real install's bin dir; (2) ``saltmdb-cli`` on PATH, the normal case for a pip/pipx
    install; (3) the legacy ``~/.mcp/SALTMDB/.venv/bin/saltmdb-cli`` default, kept only as a
    last-resort fallback for that one specific layout -- never the primary strategy."""
    override = os.environ.get("SALTMDB_CLI_PATH")
    if override and Path(override).is_file():
        return override
    on_path = shutil.which("saltmdb-cli")
    if on_path:
        return on_path
    legacy = Path.home() / ".mcp" / "SALTMDB" / ".venv" / "bin" / "saltmdb-cli"
    return str(legacy) if legacy.is_file() else None


CLI = _resolve_cli()


def run_cli(*args: str) -> str | None:
    if not CLI:
        return None
    try:
        result = subprocess.run(
            [CLI, *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def bootstrap_text(source: str | None = None) -> str:
    sections: list[str] = []
    digest = run_cli("bootstrap-digest")
    if digest:
        sections.append(digest.strip())

    # The last-session handover is large and the session already holds it from startup; a
    # compaction only needs the core digest back, in case the built-in summarizer dropped it.
    session_digest = None if source == "compact" else run_cli("session-digest")
    if session_digest:
        sections.append(session_digest.strip())

    health_raw = run_cli("corpus-health")
    if not health_raw:
        return "\n\n".join(section for section in sections if section)
    try:
        health = json.loads(health_raw)
    except json.JSONDecodeError:
        return "\n\n".join(section for section in sections if section)

    overdue = health.get("overdue_core_reviews", {})
    count = overdue.get("count", 0)
    if not count:
        return "\n\n".join(section for section in sections if section)

    titles = [e.get("title", "") for e in overdue.get("entries", [])[:5] if e.get("title")]
    notice = [
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
    sections.append("\n".join(notice))
    return "\n\n".join(section for section in sections if section)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        payload = {}

    text = bootstrap_text(payload.get("source"))
    if not text:
        return
    if payload.get("conversationId"):
        json.dump({"injectSteps": [{"ephemeralMessage": text}]}, sys.stdout)
        print()
        return
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()


if __name__ == "__main__":
    main()
