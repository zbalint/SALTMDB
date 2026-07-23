import uuid
import json
import logging
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)

def log_event(
    agent_id: str = "system",
    type: str = "event",
    content: str = "",
    error_code: str = None,
    session_id: str = None,
    context_id: str = None,
    db_connection = None,
    db_path: str = None
) -> str:
    """Appends an event to the append-only events ledger."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    event_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content, error_code, session_id, context_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (event_id, now, agent_id, type, redacted_content, error_code, session_id, context_id))
            
        from saltmdb.domain.services.librarian_service import trigger_librarian
        trigger_librarian(db_path=db_path)
        return f"Event logged successfully with ID: {event_id}"
    except Exception as e:
        logger.error("Error logging event: %s", e)
        return f"Error logging event: {e}"
    finally:
        if should_close:
            conn.close()

def get_recent_events(
    agent_id: str = None,
    type_filter: str = None,
    limit: int = 20,
    db_connection = None,
    db_path: str = None
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
        
        cursor = conn.execute(f"""
            SELECT id, timestamp, agent_id, type, content, error_code, session_id, context_id
            FROM events
            {where_sql}
            ORDER BY timestamp DESC
            LIMIT ?
        """, params + [limit])
        
        rows = cursor.fetchall()
        events = []
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
                "context_id": ectx
            }
            
            # Dynamic status check for consolidation_request events
            if etype == "consolidation_request":
                try:
                    data = json.loads(econtent)
                    raw_ids = data.get("entity_ids", [])
                    if raw_ids:
                        placeholders = ",".join("?" for _ in raw_ids)
                        st_cursor = conn.execute(f"SELECT COUNT(*) FROM entities WHERE id IN ({placeholders}) AND status = 'raw'", raw_ids)
                        unresolved_count = st_cursor.fetchone()[0]
                        item["status"] = "resolved" if unresolved_count == 0 else "pending"
                    else:
                        item["status"] = "resolved"
                except Exception:
                    item["status"] = "pending"
                    
            events.append(item)
        return events
    except Exception as e:
        logger.error("Error fetching recent events: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            conn.close()

def get_session_summary(session_id: str, db_connection = None, db_path: str = None) -> list:
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
        cursor = conn.execute("""
            SELECT id, timestamp, agent_id, type, content, error_code, context_id
            FROM events
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """, (session_id,))
        rows = cursor.fetchall()
        return [{
            "id": r[0],
            "timestamp": r[1],
            "agent_id": r[2],
            "type": r[3],
            "content": r[4],
            "error_code": r[5],
            "context_id": r[6]
        } for r in rows]
    except Exception as e:
        logger.error("Error fetching session summary: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            conn.close()
