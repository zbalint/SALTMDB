"""Agent-session browse endpoint mixin: GET /api/sessions.

Aggregates the ``agent_session_id``/``last_touched_session_id`` columns on
``entities`` and the ``agent_session_id`` column on ``events`` into one distinct
-session listing. No endpoint enumerated this before (see handover memory
``1ce2f08b``) -- the sessions-browse page needs it built from scratch.

Naming note: this is *agent* sessions (an MCP-tool-call session id), a distinct
concept from ``saltmdb.db.viewer_sessions`` (PID-tracked browser/viewer process
connections). The frontend nav label is deliberately "Agent Sessions", not bare
"Sessions", to avoid that collision (flagged in the same handover memory).

A memory "belongs" to a session if its ``agent_session_id`` (created by) or its
``last_touched_session_id`` (touched by, via ``store_memory``'s entity_id-upsert
path only) matches -- the same OR semantics as the ``session_id`` filter added to
``EntitiesMixin.get_entities``.
"""

import logging

from saltmdb.viewer.routes._shared import MAX_SESSION_LIMIT, _bounded_query_int

logger = logging.getLogger(__name__)


class SessionsMixin:
    """Provides get_sessions(); mixed into the final SALTMDBHandler elsewhere."""

    def get_sessions(self, query):  # noqa: C901
        conn = None
        try:
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_SESSION_LIMIT)
            offset = (page - 1) * limit
            id_prefix = query.get("id_prefix", [None])[0]

            conn = self.get_db_connection()
            memory_rows = conn.execute("""
                SELECT sid, COUNT(DISTINCT id) AS memory_count,
                       MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen
                FROM (
                    SELECT agent_session_id AS sid, id, created_at AS first_seen,
                           updated_at AS last_seen
                    FROM entities WHERE agent_session_id IS NOT NULL
                    UNION ALL
                    SELECT last_touched_session_id AS sid, id, created_at AS first_seen,
                           updated_at AS last_seen
                    FROM entities WHERE last_touched_session_id IS NOT NULL
                )
                GROUP BY sid
            """).fetchall()
            event_rows = conn.execute("""
                SELECT agent_session_id AS sid, COUNT(*) AS event_count,
                       MIN(timestamp) AS first_event, MAX(timestamp) AS last_event
                FROM events WHERE agent_session_id IS NOT NULL
                GROUP BY agent_session_id
            """).fetchall()

            sessions: dict[str, dict] = {}
            for row in memory_rows:
                sessions[row["sid"]] = {
                    "session_id": row["sid"],
                    "memory_count": row["memory_count"],
                    "event_count": 0,
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                }
            for row in event_rows:
                sid = row["sid"]
                entry = sessions.setdefault(
                    sid,
                    {
                        "session_id": sid,
                        "memory_count": 0,
                        "event_count": 0,
                        "first_seen": None,
                        "last_seen": None,
                    },
                )
                entry["event_count"] = row["event_count"]
                first_event, last_event = row["first_event"], row["last_event"]
                if first_event and (
                    entry["first_seen"] is None or first_event < entry["first_seen"]
                ):
                    entry["first_seen"] = first_event
                if last_event and (entry["last_seen"] is None or last_event > entry["last_seen"]):
                    entry["last_seen"] = last_event

            all_sessions = list(sessions.values())
            if id_prefix:
                all_sessions = [s for s in all_sessions if s["session_id"].startswith(id_prefix)]
            all_sessions.sort(key=lambda s: s["last_seen"] or "", reverse=True)

            total_count = len(all_sessions)
            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            page_items = all_sessions[offset : offset + limit]

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
                    "sessions": page_items,
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
