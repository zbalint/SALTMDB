"""Agent session tracking for the last session in each working directory.

Enables cross-session memory context: when an agent starts in a directory where
a prior session recently ran, that prior session's non-archived memories can be
injected as bootstrap context (see session_digest_service).
"""

import sqlite3


def record_session(conn: sqlite3.Connection, session_id: str, cwd: str, started_at: str) -> None:
    """INSERT OR IGNORE into _agent_sessions -- idempotent per session_id.

    A daemon restart may re-send hello for an adapter that already registered,
    so this must not fail or duplicate on the second call with the same session_id.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO _agent_sessions (session_id, cwd, started_at)
        VALUES (?, ?, ?)
        """,
        (session_id, cwd, started_at),
    )


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
