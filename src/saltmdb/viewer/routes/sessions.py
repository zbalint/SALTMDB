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
from typing import TYPE_CHECKING

from saltmdb.viewer.routes._shared import MAX_SESSION_LIMIT, _bounded_query_int, _utc_day_bound

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)

_SESSION_SORTS = {
    "recent": ("last_seen", "DESC"),
    "oldest": ("last_seen", "ASC"),
    "started_desc": ("started_at", "DESC"),
    "started_asc": ("started_at", "ASC"),
}


def _daemon_liveness(server) -> tuple[set[str], bool]:
    """Return daemon-owned live IDs and whether that answer is authoritative.

    The viewer can also run as a standalone process, without a daemon state object.  In that
    case persisted rows remain useful, but no status may be inferred from their timestamps.
    """
    daemon_state = getattr(server, "daemon_state", None)
    if daemon_state is None:
        return set(), False
    try:
        snapshot = daemon_state.viewer_snapshot()
    except Exception:
        logger.warning("Could not read daemon liveness for viewer sessions", exc_info=True)
        return set(), False
    return set(snapshot.get("active_agent_session_ids", [])), True


def _merge_memory_rows(sessions: dict[str, dict], rows) -> None:
    for row in rows:
        sessions[row["sid"]] = {
            "session_id": row["sid"],
            "memory_count": row["memory_count"],
            "event_count": 0,
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }


def _merge_event_rows(sessions: dict[str, dict], rows) -> None:
    for row in rows:
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
        if first_event and (entry["first_seen"] is None or first_event < entry["first_seen"]):
            entry["first_seen"] = first_event
        if last_event and (entry["last_seen"] is None or last_event > entry["last_seen"]):
            entry["last_seen"] = last_event


def _liveness_for(row, sid: str, active_session_ids: set[str], liveness_known: bool) -> str:
    """active > lost > ended > unknown, in that priority order.

    "lost" (ended_reason='orphaned', via reconcile_orphaned_sessions) is deliberately
    distinct from a clean "ended" (ended_reason='goodbye', via close_session) -- the row
    still says a session is no longer active, but not that it ended the way it was meant
    to. A row that's disconnected from the *current* daemon but not yet reconciled by any
    daemon (ended_at still NULL) stays "unknown" -- genuinely ambiguous, since the same
    agent_session_id could still reconnect and reopen this exact row.
    """
    if sid in active_session_ids:
        return "active"
    if not liveness_known or not row["ended_at"]:
        return "unknown"
    return "lost" if row["ended_reason"] == "orphaned" else "ended"


def _merge_lifecycle_rows(
    sessions: dict[str, dict], rows, active_session_ids: set[str], liveness_known: bool
) -> None:
    for row in rows:
        sid = row["session_id"]
        entry = sessions.setdefault(
            sid,
            {
                "session_id": sid,
                "memory_count": 0,
                "event_count": 0,
                "first_seen": row["started_at"],
                "last_seen": row["last_activity_at"],
            },
        )
        entry.update(
            {
                "cwd": row["cwd"],
                "owner_id": row["owner_id"],
                "started_at": row["started_at"],
                "last_activity_at": row["last_activity_at"],
                "ended_at": row["ended_at"],
                "ended_reason": row["ended_reason"],
                "liveness": _liveness_for(row, sid, active_session_ids, liveness_known),
            }
        )
        if row["started_at"] and (
            entry["first_seen"] is None or row["started_at"] < entry["first_seen"]
        ):
            entry["first_seen"] = row["started_at"]
        if row["last_activity_at"] and (
            entry["last_seen"] is None or row["last_activity_at"] > entry["last_seen"]
        ):
            entry["last_seen"] = row["last_activity_at"]


def _load_sessions(conn, active_session_ids: set[str], liveness_known: bool) -> list[dict]:
    memory_rows = conn.execute(
        """
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
        """
    ).fetchall()
    event_rows = conn.execute(
        """
        SELECT agent_session_id AS sid, COUNT(*) AS event_count,
               MIN(timestamp) AS first_event, MAX(timestamp) AS last_event
        FROM events WHERE agent_session_id IS NOT NULL
        GROUP BY agent_session_id
        """
    ).fetchall()
    lifecycle_rows = conn.execute(
        "SELECT session_id, cwd, owner_id, started_at, last_activity_at, ended_at, ended_reason "
        "FROM _agent_sessions"
    ).fetchall()

    sessions: dict[str, dict] = {}
    _merge_memory_rows(sessions, memory_rows)
    _merge_event_rows(sessions, event_rows)
    _merge_lifecycle_rows(sessions, lifecycle_rows, active_session_ids, liveness_known)
    for entry in sessions.values():
        entry.setdefault("cwd", None)
        entry.setdefault("owner_id", None)
        entry.setdefault("started_at", None)
        entry.setdefault("last_activity_at", None)
        entry.setdefault("ended_at", None)
        entry.setdefault("ended_reason", None)
        entry.setdefault("liveness", "unknown")
    return list(sessions.values())


class SessionsMixin(ViewerHandlerProtocol):
    """Provides get_sessions(); mixed into the final SALTMDBHandler elsewhere."""

    def get_sessions(self, query):  # noqa: C901, PLR0912, PLR0915
        conn = None
        try:
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_SESSION_LIMIT)
            offset = (page - 1) * limit
            id_prefix = query.get("id_prefix", [None])[0]
            state = query.get("state", [None])[0]
            owner_id = query.get("owner_id", [None])[0]
            cwd = query.get("cwd", [None])[0]
            date_field = query.get("date_field", [None])[0]
            date_from = query.get("date_from", [None])[0]
            date_to = query.get("date_to", [None])[0]
            sort = query.get("sort", ["recent"])[0] or "recent"

            if state and state not in ("active", "lost", "ended", "unknown"):
                raise ValueError("state must be active, lost, ended, or unknown")
            if date_field and date_field not in ("started_at", "last_activity_at", "ended_at"):
                raise ValueError("date_field must be started_at, last_activity_at, or ended_at")
            if (date_from or date_to) and not date_field:
                raise ValueError("date_field is required when a date bound is supplied")
            if date_from and date_to and date_from > date_to:
                raise ValueError("date_from must not be after date_to")
            if sort not in _SESSION_SORTS:
                raise ValueError("sort must be recent, oldest, started_desc, or started_asc")

            conn = self.get_db_connection()
            active_session_ids, liveness_known = _daemon_liveness(self.server)
            all_sessions = _load_sessions(conn, active_session_ids, liveness_known)
            if id_prefix:
                all_sessions = [s for s in all_sessions if s["session_id"].startswith(id_prefix)]
            if state:
                all_sessions = [s for s in all_sessions if s["liveness"] == state]
            if owner_id:
                all_sessions = [
                    s for s in all_sessions if s["owner_id"] and owner_id in s["owner_id"]
                ]
            if cwd:
                all_sessions = [s for s in all_sessions if s["cwd"] and cwd in s["cwd"]]
            if date_from:
                bound = _utc_day_bound(date_from, end_exclusive=False)
                all_sessions = [s for s in all_sessions if s[date_field] and s[date_field] >= bound]
            if date_to:
                bound = _utc_day_bound(date_to, end_exclusive=True)
                all_sessions = [s for s in all_sessions if s[date_field] and s[date_field] < bound]

            field, direction = _SESSION_SORTS[sort]
            with_value = [s for s in all_sessions if s.get(field)]
            without_value = [s for s in all_sessions if not s.get(field)]
            with_value.sort(key=lambda s: s[field], reverse=(direction == "DESC"))
            all_sessions = with_value + without_value

            total_count = len(all_sessions)
            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            page_items = all_sessions[offset : offset + limit]

            self.send_json(
                {
                    "page": page,
                    "limit": limit,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "sort": sort,
                    "state": state,
                    "owner_id": owner_id,
                    "cwd": cwd,
                    "date_field": date_field,
                    "date_from": date_from,
                    "date_to": date_to,
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

    def get_session_detail(self, session_id):
        conn = None
        try:
            conn = self.get_db_connection()
            lifecycle_row = conn.execute(
                "SELECT cwd, owner_id, started_at, last_activity_at, ended_at, ended_reason "
                "FROM _agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            memory_count = conn.execute(
                """
                SELECT COUNT(DISTINCT id) FROM (
                    SELECT id FROM entities WHERE agent_session_id = ?
                    UNION
                    SELECT id FROM entities WHERE last_touched_session_id = ?
                )
                """,
                (session_id, session_id),
            ).fetchone()[0]

            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE agent_session_id = ?",
                (session_id,),
            ).fetchone()[0]

            if lifecycle_row is None and memory_count == 0 and event_count == 0:
                self.send_json({"error": "Session not found"}, 404)
                return

            active_session_ids, liveness_known = _daemon_liveness(self.server)
            if lifecycle_row is None:
                row_for_liveness = {"ended_at": None, "ended_reason": None}
                cwd = owner_id = started_at = last_activity_at = ended_at = ended_reason = None
            else:
                row_for_liveness = lifecycle_row
                cwd = lifecycle_row["cwd"]
                owner_id = lifecycle_row["owner_id"]
                started_at = lifecycle_row["started_at"]
                last_activity_at = lifecycle_row["last_activity_at"]
                ended_at = lifecycle_row["ended_at"]
                ended_reason = lifecycle_row["ended_reason"]

            liveness = _liveness_for(
                row_for_liveness, session_id, active_session_ids, liveness_known
            )

            self.send_json(
                {
                    "session_id": session_id,
                    "owner_id": owner_id,
                    "cwd": cwd,
                    "liveness": liveness,
                    "started_at": started_at,
                    "last_activity_at": last_activity_at,
                    "ended_at": ended_at,
                    "ended_reason": ended_reason,
                    "memory_count": memory_count,
                    "event_count": event_count,
                }
            )
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
