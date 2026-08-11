"""RPC wire protocol: length-prefixed JSON framing over loopback TCP, plus the tool
classification the crash/in-flight-write contract (client.py) keys off of.

See scratch/plans/track_b_daemon_detailed.md §4/§12 for the full design and review trail.
"""

import json
import logging
import socket
import struct
import uuid
from typing import Any

from saltmdb.config import DAEMON_RPC_MAX_MESSAGE_BYTES

logger = logging.getLogger(__name__)

_LENGTH_STRUCT = struct.Struct(">I")  # 4-byte unsigned big-endian length prefix

# Tool classification for the crash/in-flight-write contract (§12). ephemeral_memory is
# deliberately in NEITHER set -- it never goes over RPC at all (mcp/tools.py's exemption).
WRITE_TOOLS = frozenset(
    {
        "store_memory",
        "log_event",
        "merge_tags",
        "archive_memory",
        "manage_relation",
        "commit_consolidation",
        "dismiss_event",
    }
)
READ_TOOLS = frozenset(
    {
        "search_memory",
        "get_canonical_tags",
        "get_canonical_predicates",
        "inspect_graph",
        "get_events",
    }
)

# Error codes (§4).
AUTH_FAILED = "AUTH_FAILED"
UNKNOWN_METHOD = "UNKNOWN_METHOD"
UNKNOWN_TOOL = "UNKNOWN_TOOL"
MALFORMED_REQUEST = "MALFORMED_REQUEST"
INTERNAL_ERROR = "INTERNAL_ERROR"
DAEMON_SHUTTING_DOWN = "DAEMON_SHUTTING_DOWN"


class FrameError(Exception):
    """Raised on any framing-level failure (oversized length prefix, truncated frame, non-JSON
    body, socket closed mid-read). Callers treat this the same as a mid-recv connection failure
    (§12) -- never a successful response."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise FrameError("connection closed before a complete frame was received")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    if len(body) > DAEMON_RPC_MAX_MESSAGE_BYTES:
        raise FrameError(f"outgoing frame ({len(body)} bytes) exceeds DAEMON_RPC_MAX_MESSAGE_BYTES")
    sock.sendall(_LENGTH_STRUCT.pack(len(body)) + body)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    """Reads one length-prefixed JSON frame. Raises FrameError on any malformed input --
    an oversized length prefix closes the read immediately with no attempt to consume the body."""
    header = _recv_exact(sock, _LENGTH_STRUCT.size)
    (length,) = _LENGTH_STRUCT.unpack(header)
    if length > DAEMON_RPC_MAX_MESSAGE_BYTES:
        raise FrameError(f"incoming frame length {length} exceeds DAEMON_RPC_MAX_MESSAGE_BYTES")
    body = _recv_exact(sock, length)
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FrameError(f"malformed JSON frame body: {e}") from e
    if not isinstance(obj, dict):
        raise FrameError("frame body is not a JSON object")
    return obj


def build_request(method: str, params: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "token": token, "method": method, "params": params}


def build_ok_response(request_id: str | None, result: Any) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result}


def build_error_response(request_id: str | None, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}
