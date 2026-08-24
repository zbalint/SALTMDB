import uuid
import json
import logging
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)


def log_event(
    agent_id: str = "system",
    type: str = "event",
    content: str = "",
    error_code: str = None,
    agent_session_id: str = None,
    context_id: str = None,
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    _in_transaction: bool = False,
) -> str:
    """Appends an event to the append-only events ledger.

    _in_transaction=True skips the internal write_transaction_retrying wrapper (and defers
    the trigger_librarian call to the caller) -- used when a caller (e.g. store_memory's
    supersession_candidate logging) already holds an open write transaction on the same
    connection. SQLite raises "cannot start a transaction within a transaction" on a nested
    BEGIN IMMEDIATE against the same connection, so this must not open its own transaction
    in that case.
    """
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    event_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    # error_code/context_id are Optional params; redact_secrets returns non-str input unchanged
    redacted_error_code = redact_secrets(error_code)  # type: ignore[arg-type]
    redacted_context_id = redact_secrets(context_id)  # type: ignore[arg-type]
    now = datetime.now(UTC).isoformat()
    try:

        def _do_insert():
            conn.execute(
                """
                INSERT INTO events (id, timestamp, agent_id, type, content, error_code, agent_session_id, context_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    event_id,
                    now,
                    agent_id,
                    type,
                    redacted_content,
                    redacted_error_code,
                    agent_session_id,
                    redacted_context_id,
                ),
            )

        if _in_transaction:
            _do_insert()
        else:

            def _write(c):
                _do_insert()

            write_transaction_retrying(conn, _write)

        if not _in_transaction:
            from saltmdb.domain.services.librarian_service import trigger_librarian

            trigger_librarian(db_path=db_path, coordinator=coordinator)
        return f"Event logged successfully with ID: {event_id}"
    except Exception as e:
        logger.error("Error logging event: %s", e)
        return f"Error logging event: {e}"
    finally:
        if should_close:
            close_connection(conn)


def get_recent_events(
    context_id: str = None,
    agent_id: str = None,
    type_filter: str = None,
    agent_session_id: str = None,
    order: str = "newest_first",
    limit: int = 20,
    offset: int = 0,
    db_connection=None,
    db_path: str = None,
) -> list:
    """Retrieves events from the append-only events ledger (agent API redesign plan §5.7,
    Phase 6 item 23).

    `context_id` is the headline filter (§3.3 fixed: previously stored on every row but
    unreachable from any agent-facing path). Every filter is a plain equality clause,
    including `agent_session_id` -- there is no more forced "session mode" (§3.4's `mode='session'`
    is gone) and no more dismissed-event/entity-status derivation loop (§3.5's `status_filter`
    is gone): this is now exactly one `SELECT ... LIMIT ? OFFSET ?`, ordered by `timestamp`
    ascending ("oldest_first") or descending ("newest_first", default).
    """
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        where_clauses = []
        params: list = []
        if context_id:
            where_clauses.append("context_id = ?")
            params.append(context_id)
        if agent_id:
            where_clauses.append("agent_id = ?")
            params.append(agent_id)
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter)
        if agent_session_id:
            where_clauses.append("agent_session_id = ?")
            params.append(agent_session_id)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        direction = "ASC" if order == "oldest_first" else "DESC"

        cursor = conn.execute(
            f"""
            SELECT id, timestamp, agent_id, type, content, error_code, agent_session_id, context_id
            FROM events
            {where_sql}
            ORDER BY timestamp {direction}
            LIMIT ? OFFSET ?
        """,
            params + [limit, offset],
        )

        results = []
        for r in cursor.fetchall():
            eid, etime, eagent, etype, econtent, ecode, esess, ectx = r
            display_content = econtent[:1000] + " [TRUNCATED]" if len(econtent) > 1000 else econtent
            results.append(
                {
                    "id": eid,
                    "timestamp": etime,
                    "agent_id": eagent,
                    "type": etype,
                    "content": display_content,
                    "error_code": ecode,
                    "agent_session_id": esess,
                    "context_id": ectx,
                }
            )
        return results
    except Exception as e:
        logger.error("Error fetching recent events: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def dismiss_events(  # noqa: C901
    event_ids: str | list[str],
    reason: str,
    agent_id: str = "system",
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Dismisses review events to prevent them from remaining pending.

    _in_transaction=True skips the internal write_transaction_retrying wrapper and runs the write
    directly against the caller's already-open transaction -- same shape as log_event's own
    _in_transaction branch. Used by db/schema.py's Track A migration sweep (see
    scratch/plans/track_a_disposition_detailed.md §5), which calls this from inside init_db's own
    write transaction, where a nested BEGIN would raise "cannot start a transaction within a
    transaction".
    """
    if isinstance(event_ids, str):
        event_ids = [event_ids]

    # Deduplicate event IDs, preserving order
    seen: set[str] = set()
    unique_ids = []
    for x in event_ids:
        if x not in seen:
            unique_ids.append(x)
            seen.add(x)
    event_ids = unique_ids

    if not reason or not reason.strip():
        raise ValueError("Dismissal reason cannot be empty")

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:

        def _write(c):
            # 1. Fetch existing events
            ph = ",".join("?" for _ in event_ids)
            cursor = c.execute(f"SELECT id, type FROM events WHERE id IN ({ph})", event_ids)
            found = {row[0]: row[1] for row in cursor.fetchall()}

            missing = [eid for eid in event_ids if eid not in found]
            if missing:
                raise ValueError(f"Events not found: {missing}")

            # Dismissible: any `consolidation_request` (covers the vector_cluster/
            # supersession_candidate/tag/general `content.target` flavors) and the
            # top-level `supersession_candidate` EVENT TYPE (the live signal fired by
            # store_memory's dedup path) -- per the approved feature contract.
            invalid_types = [
                eid
                for eid, etype in found.items()
                if etype not in ("consolidation_request", "supersession_candidate")
            ]
            if invalid_types:
                raise ValueError(f"Events are not dismissible types: {invalid_types}")

            # 2. Check which are already dismissed
            dismiss_cursor = c.execute(
                f"SELECT json_extract(content, '$.target_event_id') FROM events WHERE type='event_dismissed' AND json_extract(content, '$.target_event_id') IN ({ph})",
                event_ids,
            )
            already_dismissed = {row[0] for row in dismiss_cursor.fetchall() if row[0]}

            to_dismiss = [eid for eid in event_ids if eid not in already_dismissed]
            if not to_dismiss:
                return

            # 3. Insert dismissals
            now = datetime.now(UTC).isoformat()
            for eid in to_dismiss:
                dismissal_id = str(uuid.uuid4())
                content_json = json.dumps(
                    {
                        "target_event_id": eid,
                        "reason": reason,
                        "target_type": found[eid],
                        "dismissed_by": agent_id,
                    }
                )

                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (dismissal_id, now, agent_id, "event_dismissed", content_json),
                )

        if _in_transaction:
            _write(conn)
        else:
            write_transaction_retrying(conn, _write)
        return "Events dismissed successfully"
    finally:
        if should_close:
            close_connection(conn)
