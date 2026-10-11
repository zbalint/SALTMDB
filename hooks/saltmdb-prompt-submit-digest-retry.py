#!/usr/bin/env python3
"""SALTMDB digest retry hook.

Lifecycle event: UserPromptSubmit (Claude Code)

Completes a SessionStart digest that was left pending after a slow or killed hook. The marker is
claimed before CLI work so concurrent prompts cannot deliver the same parts twice.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

START = time.monotonic()
BUDGET_S = 12.0
MAX_RETRY_ATTEMPTS = 3
RUNNING_GRACE_S = 12
CLAIM_STALE_S = 30

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import (  # noqa: E402
    HOOK_OUTPUT_MAX_CHARS,
    cap_hook_output,
    digest_pending_path,
    get_session_id,
    read_digest_pending,
    read_stdin_json,
    resolve_cli,
    run_cli_bounded,
    state_dir,
    write_digest_pending,
)


def _release_claim(claimed_path: Path, pending_path: Path) -> None:
    try:
        os.replace(claimed_path, pending_path)
    except OSError:
        return


def _claim_marker(pending_path: Path, claimed_path: Path) -> bool:
    try:
        os.replace(pending_path, claimed_path)
    except OSError:
        return False
    try:
        os.utime(claimed_path, None)
    except OSError:
        pass
    return True


def _ordered_missing(missing: object) -> list[str]:
    if not isinstance(missing, list):
        return []
    return [part for part in ("bootstrap-digest", "session-digest") if part in missing]


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _claim_ready(pending_path: Path, claimed_path: Path) -> dict[str, object] | None:
    marker = read_digest_pending(pending_path)
    ready = marker is not None
    if marker is not None:
        running_since = marker.get("running_since")
        if (
            isinstance(running_since, (int, float))
            and time.time() - running_since < RUNNING_GRACE_S
        ):
            ready = False
    elif claimed_path.is_file():
        try:
            claimed_age = time.time() - claimed_path.stat().st_mtime
        except OSError:
            ready = False
        else:
            if claimed_age >= CLAIM_STALE_S:
                # shortcut: a compact SessionStart can recreate the marker in the same few
                # milliseconds; stale-claim recovery may overwrite that marker before it is
                # claimed below.
                try:
                    os.replace(claimed_path, pending_path)
                except OSError:
                    ready = False
                else:
                    marker = read_digest_pending(pending_path)
                    ready = marker is not None
            else:
                ready = False
    else:
        ready = False

    if not ready or not _claim_marker(pending_path, claimed_path):
        return None
    return read_digest_pending(claimed_path)


def _fetch_parts(cli: str, missing: list[str]) -> tuple[list[str], list[str]]:
    fetched: list[str] = []
    output: list[str] = []
    for part in missing:
        result = run_cli_bounded(cli, (part,), BUDGET_S - (time.monotonic() - START))
        if not result:
            break
        fetched.append(part)
        output.append(result.strip())
    return fetched, output


def _finish_claim(
    pending_path: Path,
    claimed_path: Path,
    marker: dict[str, object],
    missing: list[str],
    fetched: list[str],
) -> None:
    remaining = [part for part in missing if part not in fetched]
    if remaining:
        attempts = marker.get("attempts", 0)
        if not isinstance(attempts, int):
            attempts = 0
        next_attempts = attempts + 1
        if next_attempts < MAX_RETRY_ATTEMPTS:
            write_digest_pending(
                pending_path, remaining, attempts=next_attempts, running_since=None
            )
    _unlink(claimed_path)


def main() -> None:
    payload = read_stdin_json()
    _ = state_dir()
    session_id = get_session_id(payload)
    if not session_id:
        return

    pending_path = digest_pending_path(session_id)
    claimed_path = Path(f"{pending_path}.claimed")
    marker = _claim_ready(pending_path, claimed_path)
    if marker is None:
        return

    # Resolve only after a successful claim: the no-marker and losing-race hot paths do no CLI work.
    cli = resolve_cli()
    if not cli:
        _release_claim(claimed_path, pending_path)
        return

    missing = _ordered_missing(marker.get("missing"))
    if not missing:
        _unlink(claimed_path)
        return

    fetched, output = _fetch_parts(cli, missing)
    if output:
        text = cap_hook_output("\n\n".join(output), cli)
        sys.stdout.write(text)
        if not text.endswith("\n") and len(text) < HOOK_OUTPUT_MAX_CHARS:
            print()
        sys.stdout.flush()
    _finish_claim(pending_path, claimed_path, marker, missing, fetched)


if __name__ == "__main__":
    main()
