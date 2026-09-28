"""Conversation-trace browse endpoints: GET /api/traces (previews) and /api/traces/<id> (full).

Mirrors the ``search_traces``/``get_trace`` MCP split: the list carries bounded previews only,
the detail route is the one place full prompt/response text leaves the database. Like every
other Viewer route, visibility is deliberately cross-owner (the Viewer is a shared human
interface) and trace text is untrusted historical data -- the frontend renders it as text.
"""

import logging
from typing import TYPE_CHECKING

from saltmdb.viewer.routes._shared import MAX_TRACE_LIMIT, _bounded_query_int

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)

TRACE_SNIPPET_CHARS = 200


class TracesMixin(ViewerHandlerProtocol):
    """Provides get_traces(); mixed into SALTMDBHandler in routes/__init__.py."""

    def get_traces(self, query):
        conn = None
        try:
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 20, 1, MAX_TRACE_LIMIT)
            offset = (page - 1) * limit

            where = []
            params: list = []
            session_filter = query.get("agent_session_id", [None])[0]
            if session_filter:
                where.append("agent_session_id = ?")
                params.append(session_filter)
            entity_filter = query.get("entity_id", [None])[0]
            if entity_filter:
                where.append("id IN (SELECT trace_id FROM trace_memory_links WHERE entity_id = ?)")
                params.append(entity_filter)
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            conn = self.get_db_connection()
            rows = conn.execute(
                f"""
                SELECT id, agent_session_id, harness, status, created_at, completed_at,
                       substr(user_prompt, 1, ?), substr(final_assistant_message, 1, ?)
                FROM conversation_traces
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [TRACE_SNIPPET_CHARS, TRACE_SNIPPET_CHARS, *params, limit, offset],
            ).fetchall()
            total_count = conn.execute(
                f"SELECT COUNT(*) FROM conversation_traces {where_sql}", params
            ).fetchone()[0]

            self.send_json(
                {
                    "page": page,
                    "limit": limit,
                    "total_count": total_count,
                    "total_pages": (total_count + limit - 1) // limit,
                    "traces": [
                        {
                            "trace_id": r[0],
                            "agent_session_id": r[1],
                            "harness": r[2],
                            "status": r[3],
                            "created_at": r[4],
                            "completed_at": r[5],
                            "user_prompt_snippet": r[6] or "",
                            "final_assistant_message_snippet": r[7] or "",
                        }
                        for r in rows
                    ],
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

    def get_trace_detail(self, trace_id):
        conn = None
        try:
            conn = self.get_db_connection()
            row = conn.execute(
                """
                SELECT id, agent_session_id, owner_id, harness, status, user_prompt,
                       final_assistant_message, capture_error, created_at, completed_at
                FROM conversation_traces WHERE id = ?
                """,
                (trace_id,),
            ).fetchone()
            if row is None:
                self.send_json({"error": "Trace not found"}, 404)
                return
            links = conn.execute(
                """
                SELECT tml.entity_id, e.title, tml.write_operation
                FROM trace_memory_links tml
                LEFT JOIN entities e ON e.id = tml.entity_id
                WHERE tml.trace_id = ?
                ORDER BY tml.created_at, tml.id
                """,
                (trace_id,),
            ).fetchall()
            self.send_json(
                {
                    "trace_id": row[0],
                    "agent_session_id": row[1],
                    "owner_id": row[2],
                    "harness": row[3],
                    "status": row[4],
                    "user_prompt": row[5],
                    "final_assistant_message": row[6],
                    "capture_error": row[7],
                    "created_at": row[8],
                    "completed_at": row[9],
                    "linked_memories": [
                        {"entity_id": lk[0], "title": lk[1] or "Unknown", "write_operation": lk[2]}
                        for lk in links
                    ],
                }
            )
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
