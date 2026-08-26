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
    owner_id: str | None = None,
) -> None:
    """Register idempotently, enriching a legacy row without inventing old values.

    A daemon restart may re-send hello for an adapter that already registered,
    so this must not fail or duplicate on the second call with the same session_id.
    """
    conn.execute(
        """
        INSERT INTO _agent_sessions
            (session_id, cwd, started_at, owner_id, last_activity_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            cwd = COALESCE(_agent_sessions.cwd, excluded.cwd),
            owner_id = COALESCE(_agent_sessions.owner_id, excluded.owner_id),
            last_activity_at = CASE
                WHEN _agent_sessions.last_activity_at IS NULL
                  OR _agent_sessions.last_activity_at < excluded.last_activity_at
                THEN excluded.last_activity_at
                ELSE _agent_sessions.last_activity_at
            END,
            ended_at = NULL,
            ended_reason = NULL
        """,
        (session_id, cwd, started_at, owner_id, started_at),
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


def get_recent_sessions_for_cwd(conn: sqlite3.Connection, cwd: str, limit: int = 10) -> list[dict]:
    """Return up to ``limit`` most recent _agent_sessions rows for this exact cwd, newest first.

    Unlike get_last_session_for_cwd (which always returns just the single latest row,
    regardless of whether that session ever produced anything), this lets a caller walk
    backward past sessions that turn out to be content-free -- see
    session_digest_service.render_last_session_digest, which needs that fallback so a
    concurrently-started sibling session (own or another agent's, freshly registered via
    hello but with zero entities yet) doesn't shadow a genuinely prior session that has
    real content.
    """
    cursor = conn.execute(
        """
        SELECT session_id, started_at FROM _agent_sessions
        WHERE cwd = ?
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (cwd, limit),
    )
    return [{"session_id": row[0], "started_at": row[1]} for row in cursor.fetchall()]
