#!/usr/bin/env python3
"""SALTMDB Copilot Trace-Capture Hook Script
Lifecycle events: agentStop and sessionEnd (Copilot CLI)

Copilot has no native ``mcp_tool`` hook entry, so unlike Claude Code and Codex (see README.md
"Conversation trace provenance") its capture is this standalone script. It re-reads the whole
session transcript (``transcriptPath``, or ``~/.copilot/session-state/<sessionId>/events.jsonl``
for ``sessionEnd``, which carries none) and sends every interaction to the daemon over loopback
RPC. Stateless on purpose: capture_trace_start/_memory_link/_complete are idempotent per turn.
# shortcut: re-sends every interaction at each stop; add a per-session cursor if hook latency on
# very long sessions becomes noticeable.

Identity: the daemon keys a trace on the adapter's ``agent_session_id``, which the MCP adapter
publishes in ``~/.saltmdb/copilot_adapter_<copilot pid>.json`` (only when SALTMDB_TRACE_CAPTURE_
ENABLED is set in the adapter's env). No such file means capture is off, and the hook does nothing.
The turn key is Copilot's ``interactionId`` (one per user prompt); ``turnId`` counts model steps.
Never blocks Copilot: always exits 0 and logs failures to ``~/.saltmdb/hooks/trace-capture.log``.
"""

import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _saltmdb_hook_common import read_stdin_json, read_transcript  # noqa: E402

# mcpToolName -> key of the written entity id in that tool's result data (mirrors the adapter's
# _WRITTEN_ID_KEY in src/saltmdb/mcp/tools.py; keep in sync).
WRITTEN_ID_KEY = {
    "store_memory": "memory_id",
    "revise_memory": "successor_memory_id",
    "supersede_memory": "successor_memory_id",
}
RPC_TIMEOUT_SECS = 5
_SALTMDB_DIR = ".saltmdb"


def _new_interaction(interaction_id):
    return {"interaction_id": interaction_id, "messages": [], "final": None, "writes": []}


def _written_ids(tool_name, content):
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(obj, dict) or obj.get("status") not in (None, "ok"):
        return []
    data = obj["data"] if isinstance(obj.get("data"), dict) else obj
    if tool_name == "consolidate_memories":
        return [
            item["memory_id"]
            for item in data.get("items", [])
            if isinstance(item, dict) and item.get("memory_id")
        ]
    key = WRITTEN_ID_KEY.get(tool_name)
    value = data.get(key) if key else None
    return [value] if value else []


def _write_of(tool_name, complete_data):
    """Return ordered ``(tool, entity_id)`` pairs for a finished memory-write call."""
    if tool_name not in WRITTEN_ID_KEY and tool_name != "consolidate_memories":
        return []
    entity_ids = _written_ids(tool_name, (complete_data.get("result") or {}).get("content"))
    return [(tool_name, entity_id) for entity_id in entity_ids]


def _get_interaction(iid, order, by_id):
    if iid not in by_id:
        by_id[iid] = _new_interaction(iid)
        order.append(iid)
    return by_id[iid]


def _record_message(data, kind, iid, order, by_id):
    if kind == "user.message" and iid:
        _get_interaction(iid, order, by_id)["messages"].append(data.get("content", ""))
    elif kind == "assistant.message" and iid:
        entry = _get_interaction(iid, order, by_id)
        if data.get("content") and not data.get("toolRequests"):
            entry["final"] = data["content"]


def _record_tool(data, kind, iid, order, by_id, started):
    if kind == "tool.execution_start":
        started[data.get("toolCallId")] = data.get("mcpToolName")
    elif kind == "tool.execution_complete" and iid and data.get("success"):
        writes = _write_of(started.get(data.get("toolCallId")), data)
        if writes:
            _get_interaction(iid, order, by_id)["writes"].extend(writes)


def _record_event(event, order, by_id, started):
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return
    kind, iid = event.get("type"), data.get("interactionId")
    if kind in ("user.message", "assistant.message"):
        _record_message(data, kind, iid, order, by_id)
    elif kind in ("tool.execution_start", "tool.execution_complete"):
        _record_tool(data, kind, iid, order, by_id, started)


def parse_interactions(lines):
    """Group a Copilot events.jsonl into one dict per user prompt (``interactionId``)."""
    order, by_id, started = [], {}, {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        _record_event(event, order, by_id, started)
    return [by_id[i] for i in order if by_id[i]["messages"]]


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("daemon closed the connection")
        buf += chunk
    return buf


def call_tool(port, token, tool, kwargs):
    """One daemon RPC tool_call (4-byte big-endian length + JSON frames, daemon/protocol.py)."""
    request = {
        "id": str(uuid.uuid4()),
        "token": token,
        "method": "tool_call",
        "params": {"tool": tool, "kwargs": kwargs},
    }
    body = json.dumps(request).encode("utf-8")
    with socket.create_connection(("127.0.0.1", port), timeout=RPC_TIMEOUT_SECS) as sock:
        sock.settimeout(RPC_TIMEOUT_SECS)
        sock.sendall(struct.pack(">I", len(body)) + body)
        (length,) = struct.unpack(">I", _recv_exact(sock, 4))
        response = json.loads(_recv_exact(sock, length))
    if not response.get("ok"):
        raise RuntimeError(f"{tool}: {response.get('error')}")
    return response.get("result")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log(home, message):
    try:
        path = home / _SALTMDB_DIR / "hooks" / "trace-capture.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass  # logging must never break the hook; there is nowhere left to report to


def _adapter(home, environ, ppid):
    loader = environ.get("COPILOT_LOADER_PID", "")
    keys = ([loader] if loader.isdigit() else []) + [str(ppid)]
    for key in keys:
        info = _read_json(home / _SALTMDB_DIR / f"copilot_adapter_{key}.json")
        if info and info.get("agent_session_id") and info.get("db_path"):
            return info
    return None


def _daemon(home, db_path):
    # Same key formula and discovery validation rules as daemon/discovery.py; a hook cannot import
    # saltmdb, so the checks are repeated here.
    key = hashlib.sha256(os.path.realpath(db_path).encode("utf-8")).hexdigest()[:16]
    path = home / _SALTMDB_DIR / f"daemon_{key}.json"
    try:
        with path.open("rb") as handle:
            st = os.fstat(handle.fileno())
            if sys.platform != "win32" and (st.st_uid != os.getuid() or st.st_mode & 0o022):
                return None
            info = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    port = info.get("service_port")
    token = info.get("auth_token")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 256
        or any(not "\x21" <= char <= "\x7e" for char in token)
    ):
        return None
    return port, token


def _capture(port, token, identity, session_id, interactions):
    for it in interactions:
        turn = {**identity, "harness_turn_id": it["interaction_id"]}
        for message in it["messages"]:
            call_tool(
                port,
                token,
                "capture_trace_start",
                {
                    **turn,
                    "harness": "copilot",
                    "harness_session_id": session_id,
                    "user_prompt": message,
                },
            )
        for tool_name, entity_id in it["writes"]:
            call_tool(
                port,
                token,
                "capture_trace_memory_link",
                {**turn, "entity_id": entity_id, "just_run_tool_name": tool_name},
            )
        if it["final"]:
            call_tool(
                port,
                token,
                "capture_trace_complete",
                {**turn, "final_assistant_message": it["final"]},
            )


INVALID_SESSION_LABEL = "<invalid>"
TRANSCRIPT_DENIED = "transcript path denied or unreadable"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def _valid_session_id(session_id):
    return (
        isinstance(session_id, str)
        and bool(SESSION_ID_PATTERN.fullmatch(session_id))
        and session_id not in (".", "..")
    )


def _session_log_label(payload):
    session_id = payload.get("sessionId") or ""
    return session_id if _valid_session_id(session_id) else INVALID_SESSION_LABEL


def _resolve_transcript(payload, home):
    session_id = payload.get("sessionId") or ""
    explicit = payload.get("transcriptPath")
    if explicit:
        return explicit
    if not _valid_session_id(session_id):
        raise RuntimeError(TRANSCRIPT_DENIED)
    return str(home / ".copilot" / "session-state" / session_id / "events.jsonl")


def _read_transcript_or_raise(transcript):
    text = read_transcript(transcript)
    if text is None:
        raise RuntimeError(TRANSCRIPT_DENIED)
    return text


def run(payload, home, environ, ppid):
    log_session = _session_log_label(payload)
    try:
        adapter = _adapter(home, environ, ppid)
        if adapter is None:
            return 0  # capture not enabled for this Copilot process
        transcript = _resolve_transcript(payload, home)
        daemon = _daemon(home, adapter["db_path"])
        if daemon is None:
            raise RuntimeError("no daemon discovery file")
        interactions = parse_interactions(_read_transcript_or_raise(transcript).splitlines())
        identity = {
            "agent_id": adapter.get("agent_id", "copilot"),
            "agent_session_id": adapter["agent_session_id"],
        }
        _capture(*daemon, identity, log_session, interactions)
    except Exception as exc:  # noqa: BLE001 -- a capture failure must never break Copilot
        _log(home, f"session {log_session}: {type(exc).__name__}: {exc}")
    return 0


def main():
    run(read_stdin_json(), Path.home(), os.environ, os.getppid())
    sys.exit(0)


if __name__ == "__main__":
    main()
