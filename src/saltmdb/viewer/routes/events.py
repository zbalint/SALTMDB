"""Event/tag browse endpoints, plus the retired get_locks handler kept for parity.

GET /api/events, GET /api/tags. get_locks() is dead code (the /api/locks route in
do_GET now returns a static 410 without calling it) but is moved here verbatim
rather than deleted, since deleting it is a separate decision from this refactor.
"""

import logging
import sqlite3
from typing import TYPE_CHECKING

from saltmdb.viewer.routes._shared import MAX_EVENT_LIMIT, _bounded_query_int

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


class EventsMixin(ViewerHandlerProtocol):
    """Provides get_events(), get_tags(), get_locks(); mixed into SALTMDBHandler elsewhere."""

    def get_events(self, query):
        conn = None
        try:
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_EVENT_LIMIT)
            offset = (page - 1) * limit

            agent_filter = query.get("agent_id", [None])[0]
            type_filter = query.get("type", [None])[0]
            context_filter = query.get("context_id", [None])[0]
            q_filter = query.get("q", [None])[0]
            session_filter = query.get("agent_session_id", [None])[0]

            where = []
            params = []
            if agent_filter:
                where.append("agent_id = ?")
                params.append(agent_filter)
            if session_filter:
                where.append("agent_session_id = ?")
                params.append(session_filter)
            if type_filter:
                where.append("type = ?")
                params.append(type_filter)
            if context_filter:
                where.append("context_id = ?")
                params.append(context_filter)
            if q_filter:
                where.append("content LIKE ?")
                params.append(f"%{q_filter}%")

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            conn = self.get_db_connection()
            cursor = conn.execute(
                f"""
                SELECT id, timestamp, agent_id, type, content, error_code, agent_session_id, context_id
                FROM events
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """,
                params + [limit, offset],
            )
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM events {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            events = [
                {
                    "id": r[0],
                    "timestamp": r[1],
                    "agent_id": r[2],
                    "type": r[3],
                    "content": r[4],
                    "error_code": r[5],
                    "agent_session_id": r[6],
                    "context_id": r[7],
                }
                for r in rows
            ]

            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json(
                {
                    "page": page,
                    "limit": limit,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "pagination": {
                        "page": page,
                        "per_page": limit,
                        "total": total_count,
                        "total_pages": total_pages,
                    },
                    "events": events,
                }
            )
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_tags(self):
        conn = None
        try:
            conn = self.get_db_connection()
            cursor = conn.execute("""
                SELECT t.id, t.name, t.canonical_id, COUNT(et.entity_id) as usage_count
                FROM tags t
                LEFT JOIN entity_tags et ON t.id = et.tag_id
                GROUP BY t.id, t.name, t.canonical_id
                ORDER BY usage_count DESC, t.name ASC
            """)
            rows = cursor.fetchall()
            tags = [
                {"id": r[0], "name": r[1], "canonical_id": r[2], "usage_count": r[3]} for r in rows
            ]
            self.send_json({"tags": tags})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_locks(self):
        conn = None
        try:
            conn = self.get_db_connection()
            rows = []
            try:
                cursor = conn.execute(
                    "SELECT task_name, locked_at, locked_by_pid, last_run_at FROM _system_locks"
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                try:
                    cursor = conn.execute(
                        "SELECT task_name, locked_at, locked_by_pid, last_run_at FROM task_locks"
                    )
                    rows = cursor.fetchall()
                except sqlite3.OperationalError:
                    pass
            locks = [
                {"task_name": r[0], "locked_at": r[1], "locked_by_pid": r[2], "last_run_at": r[3]}
                for r in rows
            ]
            self.send_json({"locks": locks})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
