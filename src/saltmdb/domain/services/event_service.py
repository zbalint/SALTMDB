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
    session_id: str = None,
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
                INSERT INTO events (id, timestamp, agent_id, type, content, error_code, session_id, context_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    event_id,
                    now,
                    agent_id,
                    type,
                    redacted_content,
                    redacted_error_code,
                    session_id,
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


def get_recent_events(  # noqa: PLR0912, C901, PLR0915
    agent_id: str = None,
    type_filter: str = None,
    limit: int = 20,
    offset: int = 0,
    status_filter: str = None,
    db_connection=None,
    db_path: str = None,
) -> list:
    """Retrieves recent logged events from the events ledger."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        where_clauses = []
        params = []
        if agent_id:
            where_clauses.append("agent_id = ?")
            params.append(agent_id)
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # We need to fetch batches to handle status_filter and pagination correctly
        batch_size = max(100, limit + offset)
        sql_offset = 0
        filtered_results: list[dict] = []

        while len(filtered_results) < limit + offset:
            cursor = conn.execute(
                f"""
                SELECT id, timestamp, agent_id, type, content, error_code, session_id, context_id
                FROM events
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """,
                params + [batch_size, sql_offset],
            )

            rows = cursor.fetchall()
            if not rows:
                break

            sql_offset += batch_size

            # Pre-fetch dismissals for all relevant events in this batch
            review_event_ids = [
                r[0] for r in rows if r[3] in ("consolidation_request", "supersession_candidate")
            ]
            dismissed_event_ids = set()
            if review_event_ids:
                ph = ",".join("?" for _ in review_event_ids)
                dismiss_cursor = conn.execute(
                    f"SELECT json_extract(content, '$.target_event_id') FROM events WHERE type='event_dismissed' AND json_extract(content, '$.target_event_id') IN ({ph})",
                    review_event_ids,
                )
                dismissed_event_ids = {r[0] for r in dismiss_cursor.fetchall() if r[0]}

            # Pre-fetch entity statuses
            all_source_entity_ids = set()
            row_entities_map = {}
            for r in rows:
                etype = r[3]
                if etype in ("consolidation_request", "supersession_candidate"):
                    try:
                        data = json.loads(r[4])
                        source_ids: list[str] | None = []
                        if etype == "consolidation_request":

                            def _get_valid(lst):
                                # Reject the whole list if it's malformed (wrong container
                                # type, empty, or any member isn't a non-blank string)
                                # rather than silently sanitizing bad members out --
                                # a partially-typed payload must stay "pending", never
                                # be treated as if the bad members were never there.
                                if not isinstance(lst, list) or not lst:
                                    return None
                                valid = []
                                for x in lst:
                                    if not isinstance(x, str) or not x.strip():
                                        return None
                                    valid.append(x.strip())
                                return valid

                            valid_ids = _get_valid(data.get("entity_ids"))
                            if not valid_ids:
                                valid_ids = _get_valid(data.get("new_raw_entity_ids"))
                            source_ids = valid_ids
                        elif etype == "supersession_candidate":
                            new_entity = data.get("new_entity_id")
                            if isinstance(new_entity, str) and new_entity.strip():
                                source_ids = [new_entity.strip()]
                            else:
                                source_ids = None

                        row_entities_map[r[0]] = source_ids
                        if source_ids:
                            all_source_entity_ids.update(source_ids)
                    except Exception:
                        row_entities_map[r[0]] = None

            raw_entities = set()
            if all_source_entity_ids:
                ph = ",".join("?" for _ in all_source_entity_ids)
                raw_cursor = conn.execute(
                    f"SELECT id FROM entities WHERE id IN ({ph}) AND status = 'raw'",
                    list(all_source_entity_ids),
                )
                raw_entities = {r[0] for r in raw_cursor.fetchall()}

            for r in rows:
                eid, etime, eagent, etype, econtent, ecode, esess, ectx = r

                # Truncate content for non-consolidation_request events if longer than 1000 chars
                if etype != "consolidation_request" and len(econtent) > 1000:
                    display_content = econtent[:1000] + " [TRUNCATED]"
                else:
                    display_content = econtent

                item = {
                    "id": eid,
                    "timestamp": etime,
                    "agent_id": eagent,
                    "type": etype,
                    "content": display_content,
                    "error_code": ecode,
                    "session_id": esess,
                    "context_id": ectx,
                }

                # Dynamic status check
                if etype in ("consolidation_request", "supersession_candidate"):
                    if eid in dismissed_event_ids:
                        item["status"] = "dismissed"
                    else:
                        source_ids = row_entities_map.get(eid, [])
                        if not source_ids:
                            item["status"] = "pending"
                        else:
                            has_raw = any(sid in raw_entities for sid in source_ids)
                            item["status"] = "pending" if has_raw else "resolved"

                if status_filter and item.get("status") != status_filter:
                    continue

                filtered_results.append(item)

        return filtered_results[offset : offset + limit]
    except Exception as e:
        logger.error("Error fetching recent events: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def get_session_summary(session_id: str, db_connection=None, db_path: str = None) -> list:
    """Retrieves all event logs associated with a specific session ID."""
    if not session_id:
        return []
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        cursor = conn.execute(
            """
            SELECT id, timestamp, agent_id, type, content, error_code, context_id
            FROM events
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """,
            (session_id,),
        )
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "agent_id": r[2],
                "type": r[3],
                "content": r[4],
                "error_code": r[5],
                "context_id": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Error fetching session summary: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def dismiss_events(
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
