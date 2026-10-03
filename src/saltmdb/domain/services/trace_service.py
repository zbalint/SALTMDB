"""Conversation-trace capture, linking, lifecycle, and read services."""

from __future__ import annotations

import logging
import sqlite3
import uuid6
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from saltmdb.config import get_db_path
from saltmdb.db.connection import close_connection, get_connection, write_transaction_retrying
from saltmdb.utils.envelope import error, ok, rejected, warning
from saltmdb.utils.text import compute_content_hash, extract_title_and_snippet
from saltmdb.utils.trace_labels import label_task_notification

logger = logging.getLogger(__name__)

Harness = Literal["codex", "claude_code", "omp", "copilot"]
LinkOperation = Literal[
    "store_memory",
    "revise_memory",
    "supersede_memory",
    "consolidate_memories",
]

_LINK_OPERATIONS = {
    "store_memory",
    "revise_memory",
    "supersede_memory",
    "consolidate_memories",
}


def _open_connection(db_connection, db_path: str | None):
    if db_connection is not None:
        return db_connection, False
    return get_connection(db_path or get_db_path()), True


def _sweep_abandoned_traces(
    conn,
    *,
    agent_session_id: str | None = None,
    agent_id: str | None = None,
    timeout_seconds: int = 3600,
) -> int:
    """Mark pending traces incomplete after session resolution or a safe timeout."""
    now = datetime.now(UTC).isoformat()
    cutoff = (datetime.now(UTC) - timedelta(seconds=timeout_seconds)).isoformat()
    scope_clauses = ["ct.status = 'pending'"]
    scope_params: list[Any] = []
    if agent_session_id is not None:
        scope_clauses.append("ct.agent_session_id = ?")
        scope_params.append(agent_session_id)
    if agent_id is not None:
        scope_clauses.append("ct.agent_id = ?")
        scope_params.append(agent_id)
    scope_sql = " AND ".join(scope_clauses)

    def _write(c) -> int:
        event_cursor = c.execute(
            f"""
            UPDATE conversation_traces AS ct
            SET status = 'incomplete', updated_at = ?
            WHERE {scope_sql}
              AND ct.agent_session_id IN (
                  SELECT session_id FROM _agent_sessions WHERE ended_at IS NOT NULL
              )
            """,
            [now, *scope_params],
        )
        fallback_cursor = c.execute(
            f"""
            UPDATE conversation_traces AS ct
            SET status = 'incomplete', updated_at = ?
            WHERE {scope_sql}
              AND ct.created_at < ?
            """,
            [now, *scope_params, cutoff],
        )
        return event_cursor.rowcount + fallback_cursor.rowcount

    return write_transaction_retrying(conn, _write)


def _append_turn_message(c, trace_id: str, message: str, message_hash: str, now: str) -> bool:
    """Attach a mid-turn user message to its turn's trace; False if this exact text is already
    attached (a hook retry)."""
    cursor = c.execute(
        """
        INSERT INTO trace_turn_messages (id, trace_id, seq, message, message_hash, created_at)
        SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?
        FROM trace_turn_messages WHERE trace_id = ?
        ON CONFLICT(trace_id, message_hash) DO NOTHING
        """,
        (str(uuid6.uuid7()), trace_id, message, message_hash, now, trace_id),
    )
    return cursor.rowcount == 1


def capture_trace_start(
    agent_session_id: str,
    agent_id: str,
    harness: Harness,
    harness_session_id: str,
    harness_turn_id: str,
    user_prompt: str,
    db_connection=None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Create an idempotent pending trace for one harness turn.

    A second call for the same turn with DIFFERENT text is a message the user sent mid-turn
    (the harness reuses the turn id for it); it is appended to that trace's turn messages
    rather than discarded. A repeat of the opening prompt, or of an already-attached message,
    is a no-op.
    """
    conn, should_close = _open_connection(db_connection, db_path)
    try:
        _sweep_abandoned_traces(conn, agent_session_id=agent_session_id)
        if harness == "claude_code":
            user_prompt = label_task_notification(user_prompt)
        trace_id = str(uuid6.uuid7())
        now = datetime.now(UTC).isoformat()
        prompt_hash = compute_content_hash(user_prompt)

        def _write(c):
            inserted = c.execute(
                """
                INSERT INTO conversation_traces
                    (id, agent_session_id, agent_id, harness, harness_session_id,
                     harness_turn_id, status, user_prompt, user_prompt_hash,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(agent_session_id, harness_turn_id) DO NOTHING
                """,
                (
                    trace_id,
                    agent_session_id,
                    agent_id,
                    harness,
                    harness_session_id,
                    harness_turn_id,
                    user_prompt,
                    prompt_hash,
                    now,
                    now,
                ),
            )
            row = c.execute(
                """
                SELECT id, user_prompt_hash FROM conversation_traces
                WHERE agent_session_id = ? AND harness_turn_id = ?
                """,
                (agent_session_id, harness_turn_id),
            ).fetchone()
            appended = False
            if inserted.rowcount == 0 and row is not None and row[1] != prompt_hash:
                appended = _append_turn_message(c, row[0], user_prompt, prompt_hash, now)
            return row, appended

        row, appended = write_transaction_retrying(conn, _write)
        if row is None:
            return rejected([error("TRACE_CAPTURE_FAILED", "The trace row could not be resolved.")])
        return ok({"id": row[0], "status": "pending", "message_appended": appended})
    except Exception as exc:
        logger.error("Error capturing trace start: %s", exc)
        return rejected([error("TRACE_CAPTURE_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def capture_trace_memory_link(
    agent_session_id: str,
    agent_id: str,
    harness_turn_id: str,
    entity_id: str,
    just_run_tool_name: LinkOperation,
    db_connection=None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Idempotently link the current entity revision to the current trace."""
    if just_run_tool_name not in _LINK_OPERATIONS:
        return rejected(
            [error("INVALID_WRITE_OPERATION", "The tool is not a supported trace-linked write.")]
        )

    conn, should_close = _open_connection(db_connection, db_path)
    try:

        def _write(c):
            trace_row = cast(
                tuple[str] | None,
                c.execute(
                    """
                    SELECT id FROM conversation_traces
                    WHERE agent_session_id = ? AND harness_turn_id = ? AND agent_id = ?
                    """,
                    (agent_session_id, harness_turn_id, agent_id),
                ).fetchone(),
            )
            if trace_row is None:
                return "unknown_trace", None

            entity_row = cast(
                tuple[str, str, str] | None,
                c.execute(
                    """
                    SELECT content_hash, created_at, updated_at
                    FROM entities WHERE id = ?
                    """,
                    (entity_id,),
                ).fetchone(),
            )
            if entity_row is None:
                return "unknown_entity", None

            content_hash, created_at, updated_at = entity_row
            if just_run_tool_name == "store_memory":
                operation = (
                    "store_memory_new" if created_at == updated_at else "store_memory_update"
                )
            else:
                operation = just_run_tool_name
            link_cursor = cast(
                sqlite3.Cursor,
                c.execute(
                    """
                    INSERT INTO trace_memory_links
                        (id, trace_id, entity_id, content_hash, write_operation, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trace_id, entity_id, content_hash) DO NOTHING
                    """,
                    (
                        str(uuid6.uuid7()),
                        trace_row[0],
                        entity_id,
                        content_hash,
                        operation,
                        datetime.now(UTC).isoformat(),
                    ),
                ),
            )
            return "linked", (trace_row[0], operation, link_cursor.rowcount > 0)

        outcome, data = write_transaction_retrying(conn, _write)
        if outcome == "unknown_trace":
            return rejected(
                [
                    error(
                        "UNKNOWN_TRACE",
                        "No trace matches this agent session, owner, and harness turn.",
                    )
                ]
            )
        if outcome == "unknown_entity":
            return rejected([error("UNKNOWN_ENTITY_ID", f"No entity matches '{entity_id}'.")])
        trace_id, operation, linked = cast(tuple[str, str, bool], data)
        return ok(
            {
                "trace_id": trace_id,
                "entity_id": entity_id,
                "write_operation": operation,
                "linked": linked,
            }
        )
    except Exception as exc:
        logger.error("Error linking memory to trace: %s", exc)
        return rejected([error("TRACE_CAPTURE_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def capture_trace_complete(
    agent_session_id: str,
    agent_id: str,
    harness_turn_id: str,
    final_assistant_message: str,
    db_connection=None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Transition a pending trace to completed, without overwriting terminal states."""
    conn, should_close = _open_connection(db_connection, db_path)
    try:
        now = datetime.now(UTC).isoformat()
        message_hash = compute_content_hash(final_assistant_message)

        def _write(c):
            trace_row = c.execute(
                """
                SELECT id, status FROM conversation_traces
                WHERE agent_session_id = ? AND harness_turn_id = ? AND agent_id = ?
                """,
                (agent_session_id, harness_turn_id, agent_id),
            ).fetchone()
            if trace_row is None:
                return None
            if trace_row[1] != "pending":
                return trace_row, False
            c.execute(
                """
                UPDATE conversation_traces
                SET status = 'completed', final_assistant_message = ?,
                    final_assistant_message_hash = ?, completed_at = ?, updated_at = ?
                WHERE agent_session_id = ? AND harness_turn_id = ?
                  AND agent_id = ? AND status = 'pending'
                """,
                (
                    final_assistant_message,
                    message_hash,
                    now,
                    now,
                    agent_session_id,
                    harness_turn_id,
                    agent_id,
                ),
            )
            completed_row = c.execute(
                """
                SELECT id, status FROM conversation_traces
                WHERE agent_session_id = ? AND harness_turn_id = ? AND agent_id = ?
                """,
                (agent_session_id, harness_turn_id, agent_id),
            ).fetchone()
            return completed_row, True

        result = write_transaction_retrying(conn, _write)
        if result is None:
            return ok(
                {"trace_id": None, "status": "unknown"},
                warnings=[
                    warning(
                        "UNKNOWN_TRACE",
                        "No trace matches this agent session, owner, and harness turn.",
                    )
                ],
            )
        row, transitioned = result
        if not transitioned:
            return ok(
                {"trace_id": row[0], "status": row[1]},
                warnings=[
                    warning(
                        "ALREADY_TERMINAL",
                        f"Trace '{row[0]}' is already terminal with status '{row[1]}'.",
                    )
                ],
            )
        return ok({"trace_id": row[0], "status": row[1]})
    except Exception as exc:
        logger.error("Error completing trace: %s", exc)
        return rejected([error("TRACE_CAPTURE_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def _run_read_sweep(conn, coordinator, tool_name: str) -> None:
    # Not owner-scoped: traces are readable across agents, so a read must not surface another
    # agent's abandoned pending trace as still "pending".
    if coordinator is not None:
        coordinator.submit(
            f"trace-sweep:{tool_name}",
            lambda writer_conn: _sweep_abandoned_traces(writer_conn),
            priority="foreground",
        )
    else:
        _sweep_abandoned_traces(conn)


def _parse_offset(cursor: str | None) -> int:
    if not cursor or not cursor.startswith("offset:"):
        return 0
    try:
        return max(0, int(cursor.split(":", 1)[1]))
    except (TypeError, ValueError):
        return 0


def search_traces(
    agent_session_id: str | None = None,
    entity_id: str | None = None,
    query_keywords: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    db_connection=None,
    db_path: str | None = None,
    coordinator=None,
) -> dict[str, Any]:
    """Return bounded trace previews (cross-agent; agent_id is attribution, not a filter)."""
    conn, should_close = _open_connection(db_connection, db_path)
    try:
        _run_read_sweep(conn, coordinator, "search_traces")
        page_size = 5 if limit is None else max(0, limit)
        offset = _parse_offset(cursor)
        where: list[str] = []
        params: list[Any] = []
        if agent_session_id is not None:
            where.append("ct.agent_session_id = ?")
            params.append(agent_session_id)
        if entity_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM trace_memory_links f "
                "WHERE f.trace_id = ct.id AND f.entity_id = ?)"
            )
            params.append(entity_id)
        rows = conn.execute(
            f"""
            SELECT ct.id, ct.harness, ct.status, ct.created_at, ct.completed_at,
                   ct.user_prompt, ct.final_assistant_message
            FROM conversation_traces AS ct
            WHERE {" AND ".join(where) or "1 = 1"}
            ORDER BY ct.created_at DESC, ct.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size + 1, offset],
        ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]

        links_by_trace: dict[str, list[str]] = {row[0]: [] for row in rows}
        if rows:
            placeholders = ",".join("?" for _ in rows)
            link_rows = conn.execute(
                f"""
                SELECT trace_id, entity_id
                FROM trace_memory_links
                WHERE trace_id IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                [row[0] for row in rows],
            ).fetchall()
            for trace_id, linked_entity_id in link_rows:
                values = links_by_trace[trace_id]
                if linked_entity_id not in values and len(values) < 10:
                    values.append(linked_entity_id)

        results = [
            {
                "trace_id": row[0],
                "harness": row[1],
                "status": row[2],
                "created_at": row[3],
                "completed_at": row[4],
                "user_prompt_snippet": extract_title_and_snippet(row[5])[1],
                "final_assistant_message_snippet": extract_title_and_snippet(row[6] or "")[1],
                "linked_entity_ids": links_by_trace[row[0]],
            }
            for row in rows
        ]
        warnings = []
        if query_keywords is not None:
            warnings.append(
                warning(
                    "TRACE_SEARCH_NOT_YET_SEMANTIC",
                    "query_keywords is accepted but has no ranking effect in Phase 1.",
                )
            )
        return ok(
            {
                "results": results,
                "next_cursor": f"offset:{offset + page_size}" if has_more else None,
            },
            warnings=warnings,
        )
    except Exception as exc:
        logger.error("Error searching traces: %s", exc)
        return rejected([error("TRACE_READ_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def get_trace(
    trace_id: str,
    db_connection=None,
    db_path: str | None = None,
    coordinator=None,
) -> dict[str, Any]:
    """Return one complete trace and its link metadata (cross-agent; agent_id is attribution)."""
    conn, should_close = _open_connection(db_connection, db_path)
    try:
        _run_read_sweep(conn, coordinator, "get_trace")
        row = conn.execute(
            """
            SELECT id, agent_session_id, agent_id, harness, harness_session_id,
                   harness_turn_id, status, user_prompt, user_prompt_hash,
                   final_assistant_message, final_assistant_message_hash,
                   capture_error, created_at, updated_at, completed_at
            FROM conversation_traces
            WHERE id = ?
            """,
            (trace_id,),
        ).fetchone()
        if row is None:
            return rejected([error("UNKNOWN_TRACE_ID", f"No trace matches trace_id '{trace_id}'.")])
        links = conn.execute(
            """
            SELECT entity_id, content_hash, write_operation, created_at
            FROM trace_memory_links
            WHERE trace_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (trace_id,),
        ).fetchall()
        turn_messages = conn.execute(
            """
            SELECT seq, message, created_at FROM trace_turn_messages
            WHERE trace_id = ? ORDER BY seq ASC
            """,
            (trace_id,),
        ).fetchall()
        return ok(
            {
                "id": row[0],
                "agent_session_id": row[1],
                "agent_id": row[2],
                "harness": row[3],
                "harness_session_id": row[4],
                "harness_turn_id": row[5],
                "status": row[6],
                "user_prompt": row[7],
                "user_prompt_hash": row[8],
                "final_assistant_message": row[9],
                "final_assistant_message_hash": row[10],
                "capture_error": row[11],
                "created_at": row[12],
                "updated_at": row[13],
                "completed_at": row[14],
                "mid_turn_messages": [
                    {"seq": m[0], "message": m[1], "created_at": m[2]} for m in turn_messages
                ],
                "trace_memory_links": [
                    {
                        "entity_id": link[0],
                        "content_hash": link[1],
                        "write_operation": link[2],
                        "created_at": link[3],
                    }
                    for link in links
                ],
                "content_is_untrusted_historical_data": True,
            }
        )
    except Exception as exc:
        logger.error("Error getting trace: %s", exc)
        return rejected([error("TRACE_READ_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def entity_trace_provenance(
    conn,
    entity_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return bounded metadata for traces linked to one entity the caller can already see."""
    rows = conn.execute(
        """
        SELECT ct.id, ct.harness, ct.created_at, ct.status
        FROM trace_memory_links AS tml
        JOIN conversation_traces AS ct ON tml.trace_id = ct.id
        WHERE tml.entity_id = ?
        ORDER BY ct.created_at DESC, ct.id DESC
        LIMIT ?
        """,
        (entity_id, limit),
    ).fetchall()
    return [
        {"trace_id": row[0], "harness": row[1], "created_at": row[2], "status": row[3]}
        for row in rows
    ]


__all__ = [
    "capture_trace_start",
    "capture_trace_memory_link",
    "capture_trace_complete",
    "search_traces",
    "get_trace",
    "entity_trace_provenance",
    "_sweep_abandoned_traces",
]
