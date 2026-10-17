"""Entity list/browse endpoint mixin: GET /api/entities (and legacy /api/entity)."""

import json
import logging
from typing import TYPE_CHECKING, Any

from saltmdb.viewer.routes._shared import (
    MAX_ENTITY_LIMIT,
    _ENTITY_SORTS,
    _bounded_query_int,
    _utc_day_bound,
)

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


def _entity_options(query):
    page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
    limit = _bounded_query_int(query, "limit", 50, 1, MAX_ENTITY_LIMIT)
    options = {
        "page": page,
        "limit": limit,
        "offset": (page - 1) * limit,
        "agent_id": query.get("agent_id", [None])[0],
        "status": query.get("status", [None])[0],
        "context_id": query.get("context_id", [None])[0],
        "is_core": query.get("is_core", [None])[0],
        "tag": query.get("tag", [None])[0],
        "q": query.get("q", [None])[0],
        "id_prefix": query.get("id_prefix", [None])[0],
        "memory_type": query.get("memory_type", [None])[0],
        "embedding_status": query.get("embedding_status", [None])[0],
        "quality_status": query.get("quality_status", [None])[0],
        "session_id": query.get("session_id", [None])[0],
        "sort": query.get("sort", ["updated_desc"])[0] or "updated_desc",
        "date_field": query.get("date_field", [None])[0],
        "date_from": query.get("date_from", [None])[0],
        "date_to": query.get("date_to", [None])[0],
    }
    _validate_entity_options(options)
    return options


def _validate_entity_options(options):
    sort = options["sort"]
    date_field = options["date_field"]
    date_from = options["date_from"]
    date_to = options["date_to"]
    if sort not in _ENTITY_SORTS:
        raise ValueError("sort must be updated_desc, updated_asc, created_desc, or created_asc")
    if date_field and date_field not in ("created", "updated"):
        raise ValueError("date_field must be created or updated")
    if (date_from or date_to) and not date_field:
        raise ValueError("date_field is required when a date bound is supplied")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must not be after date_to")


def _entity_filters(options):
    where_clauses = []
    params = []
    if options["agent_id"]:
        where_clauses.append("agent_id = ?")
        params.append(options["agent_id"])
    if options["status"]:
        where_clauses.append("status = ?")
        params.append(options["status"])
    if options["context_id"]:
        where_clauses.append("(context_id = ? OR project_id = ?)")
        params.extend([options["context_id"], options["context_id"]])
    _entity_add_core_filter(options, where_clauses, params)
    if options["tag"]:
        where_clauses.append(
            "id IN (SELECT et.entity_id FROM entity_tags et "
            "JOIN tags t ON et.tag_id = t.id WHERE t.name = ?)"
        )
        params.append(options["tag"])
    if options["q"]:
        where_clauses.append("(title LIKE ? OR full_content LIKE ?)")
        params.extend([f"%{options['q']}%", f"%{options['q']}%"])
    if options["id_prefix"]:
        where_clauses.append("id LIKE ?")
        params.append(f"{options['id_prefix']}%")
    for key, clause in (
        ("memory_type", "memory_type = ?"),
        ("embedding_status", "embedding_status = ?"),
        ("quality_status", "quality_status = ?"),
    ):
        if options[key]:
            where_clauses.append(clause)
            params.append(options[key])
    if options["session_id"]:
        where_clauses.append("(agent_session_id = ? OR last_touched_session_id = ?)")
        params.extend([options["session_id"], options["session_id"]])
    _entity_add_date_filters(options, where_clauses, params)
    return where_clauses, params


def _entity_add_core_filter(options, where_clauses, params):
    is_core_filter = options["is_core"]
    if is_core_filter is None:
        return
    normalized_is_core = str(is_core_filter).strip().lower()
    if normalized_is_core in ("true", "1", "yes"):
        is_core_value = 1
    elif normalized_is_core in ("false", "0", "no"):
        is_core_value = 0
    elif normalized_is_core:
        raise ValueError("is_core must be one of true, 1, yes, false, 0, no")
    else:
        is_core_value = None
    if is_core_value is not None:
        where_clauses.append("is_core = ?")
        params.append(is_core_value)


def _entity_add_date_filters(options, where_clauses, params):
    if options["date_from"]:
        where_clauses.append(f"{options['date_field']}_at >= ?")
        params.append(_utc_day_bound(options["date_from"], end_exclusive=False))
    if options["date_to"]:
        where_clauses.append(f"{options['date_field']}_at < ?")
        params.append(_utc_day_bound(options["date_to"], end_exclusive=True))


def _entity_query(conn, options, where_sql, params):
    order_field, order_direction = _ENTITY_SORTS[options["sort"]]
    cursor = conn.execute(
        f"""
        SELECT id, created_at, updated_at, last_accessed_at, agent_id, scope, is_core, weight, status, parent_ids, title, context_id, embedding_status, memory_type, quality_score, quality_status, agent_session_id, last_touched_session_id
        FROM entities
        {where_sql}
        ORDER BY {order_field} {order_direction}, id ASC
        LIMIT ? OFFSET ?
    """,
        params + [options["limit"], options["offset"]],
    )
    rows = cursor.fetchall()
    total_count = conn.execute(f"SELECT COUNT(*) FROM entities {where_sql}", params).fetchone()[0]
    return rows, total_count


def _entity_tags(conn, rows):
    if not rows:
        return {}
    entity_ids = [r[0] for r in rows]
    placeholders = ",".join("?" for _ in entity_ids)
    tag_cursor = conn.execute(
        f"""
        SELECT et.entity_id, t.name
        FROM entity_tags et
        JOIN tags t ON et.tag_id = t.id
        WHERE et.entity_id IN ({placeholders})
    """,
        entity_ids,
    )
    tag_map: dict[Any, list[Any]] = {}
    for eid, tname in tag_cursor.fetchall():
        tag_map.setdefault(eid, []).append(tname)
    return tag_map


def _entity_payload(rows, tag_map):
    return [
        {
            "id": r[0],
            "created_at": r[1],
            "updated_at": r[2],
            "last_accessed_at": r[3],
            "agent_id": r[4],
            "scope": r[5],
            "is_core": bool(r[6]),
            "weight": r[7],
            "status": r[8],
            "parent_ids": json.loads(r["parent_ids"]) if r["parent_ids"] else [],
            "title": r[10],
            "context_id": r[11],
            "embedding_status": "archived" if r[8] == "archived" else (r[12] or "pending"),
            "memory_type": r[13] or "fact",
            "quality_score": r[14],
            "quality_status": r[15],
            "agent_session_id": r[16],
            "last_touched_session_id": r[17],
            "tags": tag_map.get(r[0], []),
        }
        for r in rows
    ]


class EntitiesMixin(ViewerHandlerProtocol):
    """Provides get_entities(); mixed into the final SALTMDBHandler elsewhere."""

    def get_entities(self, query):
        conn = None
        try:
            options = _entity_options(query)
            where_clauses, params = _entity_filters(options)
            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            conn = self.get_db_connection()
            rows, total_count = _entity_query(conn, options, where_sql, params)
            entities = _entity_payload(rows, _entity_tags(conn, rows))
            limit = int(options["limit"])
            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json(
                {
                    "page": options["page"],
                    "limit": limit,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "sort": options["sort"],
                    "date_field": options["date_field"],
                    "date_from": options["date_from"],
                    "date_to": options["date_to"],
                    "pagination": {
                        "page": options["page"],
                        "per_page": limit,
                        "total": total_count,
                        "total_pages": total_pages,
                    },
                    "entities": entities,
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
