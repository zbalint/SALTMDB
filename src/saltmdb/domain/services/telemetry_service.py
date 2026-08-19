"""Usage telemetry sink (agent API redesign plan §5.9).

Separate from the `events` ledger by design: `events` is an agent-facing semantic scratchpad
agents read back (get_events); telemetry is machine-written diagnostic metadata that would
drown what agents read if it shared a table. Written automatically by the daemon's dispatch
layer (daemon/dispatch.py's dispatch_tool) on every tool call -- never called by an agent, and
deliberately not an MCP tool.

Hard constraint, enforced by this module's own signature: metadata only, never argument values.
record_call takes `param_names` (a list of strings), not the arguments themselves -- there is no
parameter anywhere in this module that could carry a raw value into the telemetry table, so a
future caller cannot accidentally leak one through it.
"""

import json
import logging
import time
import uuid
from datetime import datetime, UTC
from typing import Any

from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection

logger = logging.getLogger(__name__)


def classify_result(result: Any, raised: BaseException | None) -> tuple[str, str | None]:
    """Best-effort (status, error_code) classification from a tool call's outcome.

    Tool return shapes are heterogeneous pre-envelope (Phase 1): a plain string ("Error: ..." on
    failure), or a dict/list on success. This is deliberately loose and MUST be revisited once
    §4.2's envelope is actually wired into tool responses (Phase 2+) -- at that point a dict with
    `status: "rejected"` is unambiguous and this heuristic narrows to that one check. Telemetry
    is diagnostic-only; a misclassified row here never affects a real request's outcome.
    """
    if raised is not None:
        return "error", type(raised).__name__
    if isinstance(result, str) and result.startswith("Error"):
        return "error", None
    if isinstance(result, dict):
        status = result.get("status")
        if status == "rejected":
            return "rejected", (result.get("errors") or [{}])[0].get("code") if result.get(
                "errors"
            ) else None
        if isinstance(status, str) and status.upper() in {
            "REJECTED",
            "DAEMON_CONNECTION_LOST_DURING_WRITE",
        }:
            return "error", result.get("error_code") or status
        if result.get("error"):
            return "error", None
    return "ok", None


def record_call(
    tool_name: str,
    param_names: list[str],
    status: str,
    latency_ms: float,
    owner_id: str | None = None,
    error_code: str | None = None,
    db_connection=None,
    db_path: str = None,
) -> None:
    """Records one tool-call telemetry row. Best-effort: exceptions are logged, never raised --
    a telemetry write must never fail or block the real request it describes."""
    should_close = False
    conn = db_connection
    try:
        if not conn:
            db_path = db_path or get_db_path()
            conn = get_connection(db_path)
            should_close = True

        row = (
            str(uuid.uuid4()),
            datetime.now(UTC).isoformat(),
            tool_name,
            owner_id,
            json.dumps(sorted(param_names or [])),
            status,
            error_code,
            latency_ms,
        )

        def _write(c):
            c.execute(
                """
                INSERT INTO tool_call_telemetry
                    (id, timestamp, tool_name, owner_id, param_names, status, error_code, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )

        # write_transaction_retrying already detects an ambient coordinator transaction (the
        # `conn is _coordinator_connection.get()` check) and reuses it without a nested BEGIN,
        # so this one call is correct whether `conn` came from a coordinator.submit lambda or a
        # standalone connection opened just above.
        write_transaction_retrying(conn, _write)
    except Exception as e:  # noqa: BLE001 -- telemetry is best-effort by design, see docstring
        logger.warning("Telemetry write failed for tool '%s' (non-fatal): %s", tool_name, e)
    finally:
        if should_close:
            close_connection(conn)


class Timer:
    """Tiny monotonic stopwatch, used by dispatch_tool to measure call latency without
    depending on wall-clock time (which can jump backwards under NTP adjustment)."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000
