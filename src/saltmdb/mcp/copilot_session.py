"""Adapter-published session file, so a Copilot CLI hook can join the adapter's trace identity.

Copilot has no native ``mcp_tool`` hook entry (see hooks/README.md), so its trace-capture hook is a
standalone process. The daemon trusts the ``agent_session_id`` on a capture call, and a trace is
keyed on it, so the hook must present the id of the adapter that performs the memory writes. The
adapter publishes that id here; the hook is a sibling of the adapter under the same ``copilot``
process, and finds the file by that process's pid (``COPILOT_LOADER_PID``, else the adapter's
parent pid).
"""

from __future__ import annotations

import json
import logging
import os

from saltmdb.daemon import discovery
from saltmdb.db.viewer_sessions import _pid_alive

logger = logging.getLogger(__name__)

_PREFIX = "copilot_adapter_"


def _key() -> str:
    loader = os.environ.get("COPILOT_LOADER_PID", "")
    return loader if loader.isdigit() else str(os.getppid())


def _sweep(directory: str) -> None:
    for name in os.listdir(directory):
        if not (name.startswith(_PREFIX) and name.endswith(".json")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as f:
                pid = json.load(f).get("adapter_pid")
        except (OSError, ValueError):
            continue  # unreadable/foreign file: leave it, never guess it is stale
        if isinstance(pid, int) and not _pid_alive(pid):
            remove(path)


def publish(
    agent_id: str, agent_session_id: str, db_path: str, directory: str | None = None
) -> str:
    """Atomically write this adapter's identity file (mode 0600) and return its path."""
    directory = directory or discovery._discovery_dir()
    _sweep(directory)
    final = os.path.join(directory, f"{_PREFIX}{_key()}.json")
    tmp = f"{final}.{os.getpid()}.tmp"
    payload = {
        "agent_id": agent_id,
        "agent_session_id": agent_session_id,
        "db_path": db_path,
        "adapter_pid": os.getpid(),
    }
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, final)
    return final


def remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
