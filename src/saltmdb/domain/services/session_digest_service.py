"""Session digest service: render the last session's memory summary for bootstrap context.

Enables cross-session memory context: given a working directory, looks up the most
recent prior agent session in that directory and renders a short, content-free index
(id + title + memory_type only) of the non-archived memories that session created
or touched. This index is injected into the session bootstrap to provide continuity
across agent restarts.
"""

import os


def render_last_session_digest(conn, cwd: str) -> str:
    """Look up the most recent prior agent session for this directory and render a short,
    content-free index (id + title + memory_type only, never full_content) of the non-archived
    memories that session created or touched. Empty envelope if no prior session exists for
    this cwd, or if that session touched zero surviving (non-archived) memories.

    The digest lists memories created BY that session (agent_session_id match) or touched by
    it (last_touched_session_id match), deliberately including both. It does NOT filter by
    owner_id -- a prior session may have worked with multiple owners, and all their memories
    are still relevant context.

    Walks backward through recent sessions for this cwd (newest first) until it finds one
    with surviving (non-archived) entities. This matters once more than one agent/process is
    concurrently active in the same directory: a sibling session's hello can register itself
    as the newest _agent_sessions row for this cwd before it has produced anything, which
    would otherwise shadow a genuinely prior, content-having session and render an empty
    digest for everyone querying that cwd (see SALTMDB memory 8402f500 for the live repro).
    """
    from saltmdb.db import agent_sessions
    from saltmdb.domain.services.core_governance_service import _escape_yaml_line

    normalized_cwd = os.path.realpath(cwd)
    candidates = agent_sessions.get_recent_sessions_for_cwd(conn, normalized_cwd)

    last = None
    rows = []
    for candidate in candidates:
        session_id = candidate["session_id"]
        candidate_rows = conn.execute(
            """
            SELECT id, title, memory_type FROM entities
            WHERE (agent_session_id = ? OR last_touched_session_id = ?)
              AND status != 'archived'
            ORDER BY updated_at DESC
            """,
            (session_id, session_id),
        ).fetchall()
        if candidate_rows:
            last = candidate
            rows = candidate_rows
            break

    if last is None:
        return "<saltmdb-last-session-digest>\n\n</saltmdb-last-session-digest>"

    session_id = last["session_id"]
    lines = [
        f'<saltmdb-last-session-digest session_id="{session_id}" started_at="{last["started_at"]}">',
        "",
    ]
    for row in rows:
        entity_id, title, memory_type = row[0], row[1], row[2]
        lines.append(f"- {entity_id} [{memory_type}] {_escape_yaml_line(title)}")
    lines.append("")
    lines.append("</saltmdb-last-session-digest>")
    return "\n".join(lines)
