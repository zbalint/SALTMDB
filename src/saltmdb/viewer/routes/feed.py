"""Live-feed endpoint: GET /api/feed.

One time-ordered stream over memories, events and conversation traces, so a viewer can watch
what running agent sessions are doing without opening three separate pages.

Timestamps are compared as raw strings, never through ``datetime()``: every writer stores
``datetime.now(UTC).isoformat()``, which sorts lexicographically, and ``datetime()`` would drop
the sub-second part the cursor needs to be exact.
"""

import logging
from typing import TYPE_CHECKING

from saltmdb.viewer.routes._shared import MAX_FEED_LIMIT, _bounded_query_int
from saltmdb.viewer.routes.sessions import _daemon_liveness

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)

FEED_PREVIEW_CHARS = 200

# A memory appears at its creation time; archived rows are superseded history shadows of a
# memory that is already listed, so they would duplicate it. A trace appears at its last update
# so a completing trace resurfaces as the same item (same kind+id) with its new status.
_FEED_SQL = """
    SELECT kind, id, ts, sid, actor, title, preview, status FROM (
        SELECT 'memory' AS kind, id, created_at AS ts, agent_session_id AS sid,
               agent_id AS actor, title, substr(full_content, 1, ?) AS preview, status,
               last_touched_session_id AS touched_sid
        FROM entities WHERE status != 'archived'
        UNION ALL
        SELECT 'event', id, timestamp, agent_session_id, agent_id, type,
               substr(content, 1, ?), error_code, NULL
        FROM events
        UNION ALL
        SELECT 'trace', id, updated_at, agent_session_id, agent_id, harness,
               substr(user_prompt, 1, ?), status, NULL
        FROM conversation_traces
    )
    {where}
    ORDER BY ts {direction}, kind {direction}, id {direction}
    LIMIT ?
"""


def _parse_cursor(raw: str) -> tuple[str, str, str]:
    """Split ``<timestamp>|<kind>|<id>``; the id is the remainder so it may contain ``|``."""
    parts = raw.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError("since must look like <timestamp>|<kind>|<id>")
    return parts[0], parts[1], parts[2]


def _cursor_of(item: dict) -> str:
    return f"{item['timestamp']}|{item['kind']}|{item['id']}"


class FeedMixin(ViewerHandlerProtocol):
    """Provides get_feed(); mixed into SALTMDBHandler in routes/__init__.py."""

    def get_feed(self, query):
        conn = None
        try:
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_FEED_LIMIT)
            since = query.get("since", [None])[0]
            params: list = [FEED_PREVIEW_CHARS] * 3
            conditions: list[str] = []
            direction = "DESC"
            session = query.get("session", [None])[0]
            if session:
                # Same created-or-touched rule as the Agent Sessions page for memories.
                conditions.append("(sid = ? OR touched_sid = ?)")
                params.extend([session, session])
            active_ids, liveness_known = _daemon_liveness(self.server)
            if query.get("active_only", [None])[0] and liveness_known:
                # No active sessions must yield an empty feed, not an unfiltered one.
                marks = ", ".join("?" * len(active_ids)) or "NULL"
                conditions.append(f"(sid IN ({marks}) OR touched_sid IN ({marks}))")
                params.extend([*active_ids, *active_ids])
            if since:
                # Catch-up reads oldest-first from the cursor so a burst larger than ``limit``
                # is consumed in order across polls without skipping rows.
                conditions.append("(ts, kind, id) > (?, ?, ?)")
                direction = "ASC"
                params.extend(_parse_cursor(since))
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            conn = self.get_db_connection()
            rows = conn.execute(
                _FEED_SQL.format(where=where, direction=direction), [*params, limit + 1]
            ).fetchall()
            has_more = len(rows) > limit
            items = [
                {
                    "kind": r[0],
                    "id": r[1],
                    "timestamp": r[2],
                    "session_id": r[3],
                    "actor": r[4],
                    "title": r[5],
                    "preview": r[6],
                    "status": r[7],
                }
                for r in rows[:limit]
            ]
            if since:
                items.reverse()
            cursor = _cursor_of(items[0]) if items else since
            self.send_json(
                {
                    "items": items,
                    "cursor": cursor,
                    "has_more": has_more,
                    "liveness_known": liveness_known,
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
