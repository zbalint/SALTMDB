#!/usr/bin/env python3
"""SALTMDB Session Bootstrap Hook Script
Lifecycle event: SessionStart (Claude Code) / PreInvocation (Antigravity) / sessionStart (Copilot)

Prints the canonical core-memory bootstrap digest, then a one-line nudge if any core memory is
overdue for review (SALTMDB rollout task 8: overdue cores otherwise only surface as a hard error
the first time an agent happens to touch store_memory/manage_relation/consolidate_memories while
one is overdue -- this surfaces it proactively instead).

Agent-agnostic by construction: this script only ever emits plain text to stdout, which every
harness treats as injected context (no JSON decision schema involved), so no per-harness
branching is needed here. Python instead of bash for this whole hook family: SALTMDB itself
already requires Python (this is exactly the same dependency assumption the CLI call below
makes), and the stdlib json module handles the corpus-health parsing far more robustly than a
jq-or-regex-fallback would.
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


def main() -> None:
    digest = run_cli("bootstrap-digest")
    if digest:
        sys.stdout.write(digest)

    health_raw = run_cli("corpus-health")
    if not health_raw:
        return
    try:
        health = json.loads(health_raw)
    except json.JSONDecodeError:
        return

    overdue = health.get("overdue_core_reviews", {})
    count = overdue.get("count", 0)
    if not count:
        return

    titles = [e.get("title", "") for e in overdue.get("entries", [])[:5] if e.get("title")]
    print()
    print("<saltmdb-overdue-core-notice>")
    print(f"{count} active core memory review(s) are overdue (core_review_after elapsed).")
    for t in titles:
        print(f"- {t}")
    print(
        "store_memory/manage_relation/consolidate_memories will hard-block until these are "
        "reviewed via review_core_memory (outcome='demote' or 'archive'). Handle this before it "
        "blocks unrelated work."
    )
    print("</saltmdb-overdue-core-notice>")


if __name__ == "__main__":
    main()
