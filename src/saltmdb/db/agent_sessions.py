"""Agent session tracking for the last session in each working directory.

Enables cross-session memory context: when an agent starts in a directory where
a prior session recently ran, that prior session's non-archived memories can be
injected as bootstrap context (see session_digest_service).
"""

import sqlite3


def record_session(
    conn: sqlite3.Connection,
    session_id: str,
    cwd: str | None,
    started_at: str,
    agent_id: str | None = None,
) -> None:
    """Register idempotently, enriching a legacy row without inventing old values.

    A daemon restart may re-send hello for an adapter that already registered,
    so this must not fail or duplicate on the second call with the same session_id.
    """
    conn.execute(
        """
        INSERT INTO _agent_sessions
            (session_id, cwd, started_at, agent_id, last_activity_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            cwd = COALESCE(_agent_sessions.cwd, excluded.cwd),
            agent_id = COALESCE(_agent_sessions.agent_id, excluded.agent_id),
            last_activity_at = CASE
                WHEN _agent_sessions.last_activity_at IS NULL
                  OR _agent_sessions.last_activity_at < excluded.last_activity_at
                THEN excluded.last_activity_at
                ELSE _agent_sessions.last_activity_at
            END,
            ended_at = NULL,
            ended_reason = NULL
        """,
        (session_id, cwd, started_at, agent_id, started_at),
    )


def touch_session(conn: sqlite3.Connection, session_id: str, received_at: str) -> None:
    """Advance activity monotonically; delayed background jobs never move it backwards."""
    conn.execute(
        """UPDATE _agent_sessions SET last_activity_at = ?
           WHERE session_id = ?
             AND (last_activity_at IS NULL OR last_activity_at < ?)""",
        (received_at, session_id, received_at),
    )


def close_session(conn: sqlite3.Connection, session_id: str, ended_at: str) -> None:
    """Record a definitive normal goodbye, tagged ended_reason='goodbye'.

    This is the only path that ever sets 'goodbye' -- a raw disconnect (socket dropped,
    no goodbye) leaves ended_at/ended_reason untouched here; see reconcile_orphaned_sessions
    for how those eventually get closed out, tagged 'orphaned' instead, so the viewer can
    distinguish a clean end from one a later daemon incarnation had to infer.
    """
    conn.execute(
        "UPDATE _agent_sessions SET ended_at = ?, ended_reason = 'goodbye' "
        "WHERE session_id = ? AND ended_at IS NULL",
        (ended_at, session_id),
    )


def reconcile_orphaned_sessions(conn: sqlite3.Connection) -> int:
    """Close out every session an unclean prior daemon death left dangling, tagged 'orphaned'.

    A freshly started daemon has zero live sessions by definition -- nothing has
    hello'd to *this* process instance yet -- so any row with ended_at IS NULL at
    this exact moment is guaranteed orphaned from a dead prior incarnation (crashed,
    OOM-killed, or hard-killed by an OS mechanism such as a Windows Job Object none
    of which give close_session a chance to run). Backdates ended_at to the row's
    own last_activity_at (falling back to started_at, which is never NULL, if
    last_activity_at is itself still NULL) rather than "now", so a session doesn't
    appear to have lived on well past whatever it actually last did.

    ended_reason is set to 'orphaned', not 'goodbye' -- this deliberately does not claim
    a specific cause (crash vs. hard-kill vs. the *daemon* dying under a still-healthy
    session, which just reopens the row via record_session's ON CONFLICT once it
    reconnects). It only asserts what's actually known: no goodbye happened, and the
    daemon incarnation that could have said more about why is itself gone. The viewer
    surfaces this as a distinct "lost" liveness state, separate from a clean "ended".

    Call once, early in daemon startup, before any adapter can re-register a
    session_id via record_session -- otherwise a genuinely-reconnecting adapter's
    freshly-touched row could race this and get closed out from under it.

    Returns the number of rows closed, for startup logging.
    """
    cursor = conn.execute(
        """
        UPDATE _agent_sessions
        SET ended_at = COALESCE(last_activity_at, started_at), ended_reason = 'orphaned'
        WHERE ended_at IS NULL
        """
    )
    return cursor.rowcount


def get_last_session_for_cwd(conn: sqlite3.Connection, cwd: str) -> dict | None:
    """Look up the most recent _agent_sessions row for this exact cwd string.

    Returns a dict with keys "session_id" and "started_at", or None if no prior
    session exists for this cwd.
    """
    cursor = conn.execute(
        """
        SELECT session_id, started_at FROM _agent_sessions
        WHERE cwd = ?
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (cwd,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"session_id": row[0], "started_at": row[1]}


_CONTENT_PREDICATES: dict[str, str] = {
    "memories": """
        (
            EXISTS (
                SELECT 1
                FROM entities e
                WHERE e.agent_session_id = s.session_id
                  AND e.status != 'archived'
            )
            OR EXISTS (
                SELECT 1
                FROM entities e
                WHERE e.last_touched_session_id = s.session_id
                  AND e.status != 'archived'
            )
        )
    """,
    "traces": """
        EXISTS (
            SELECT 1
            FROM conversation_traces t
            WHERE t.agent_session_id = s.session_id
        )
    """,
}


def get_recent_sessions_for_cwd(
    conn: sqlite3.Connection,
    cwd: str,
    limit: int = 10,
    *,
    with_content: str | None = None,
    ended: bool | None = None,
) -> list[dict]:
    """Return up to ``limit`` recent sessions for this cwd.

    ``ended=None`` orders all matching sessions by ``started_at`` descending.
    ``ended=True`` selects ended sessions and orders by ``ended_at`` descending, then
    ``session_id`` descending. ``ended=False`` selects running sessions and orders by
    ``COALESCE(last_activity_at, started_at)`` descending, then ``session_id`` descending.

    ``with_content`` may be ``"memories"`` to select sessions that created or touched
    a non-archived memory, or ``"traces"`` to select sessions with a conversation
    trace. ``None`` returns all sessions. The memory-filtered lookup is used by
    ``session_digest_service.render_last_session_digest``.
    """
    if with_content is not None and with_content not in _CONTENT_PREDICATES:
        raise ValueError("with_content must be one of: memories, traces")

    where = "WHERE s.cwd = ?"
    params: list[str | int] = [cwd]
    if with_content is not None:
        where += f" AND {_CONTENT_PREDICATES[with_content]}"
    if ended is True:
        where += " AND s.ended_at IS NOT NULL"
        order_by = "s.ended_at DESC, s.session_id DESC"
    elif ended is False:
        where += " AND s.ended_at IS NULL"
        order_by = "COALESCE(s.last_activity_at, s.started_at) DESC, s.session_id DESC"
    else:
        order_by = "s.started_at DESC"
    params.append(limit)

    cursor = conn.execute(
        f"""
        SELECT s.session_id, s.started_at, s.agent_id, s.ended_at, s.ended_reason,
               s.last_activity_at
        FROM _agent_sessions AS s
        {where}
        ORDER BY {order_by}
        LIMIT ?
        """,
        tuple(params),
    )
    return [
        {
            "session_id": row[0],
            "started_at": row[1],
            "agent_id": row[2],
            "ended_at": row[3],
            "ended_reason": row[4],
            "last_activity_at": row[5],
        }
        for row in cursor.fetchall()
    ]
