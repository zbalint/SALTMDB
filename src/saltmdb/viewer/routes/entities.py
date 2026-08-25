"""Entity list/browse endpoint mixin: GET /api/entities (and legacy /api/entity)."""

import json
import logging
from typing import Any

from saltmdb.viewer.routes._shared import (
    MAX_ENTITY_LIMIT,
    _ENTITY_SORTS,
    _bounded_query_int,
    _utc_day_bound,
)

logger = logging.getLogger(__name__)


class EntitiesMixin:
    """Provides get_entities(); mixed into the final SALTMDBHandler elsewhere."""

    def get_entities(self, query):  # noqa: C901, PLR0912, PLR0915
        conn = None
        try:
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_ENTITY_LIMIT)
            offset = (page - 1) * limit

            owner_id_filter = query.get("owner_id", [None])[0]
            status_filter = query.get("status", [None])[0]
            context_id_filter = query.get("context_id", [None])[0]
            is_core_filter = query.get("is_core", [None])[0]
            tag_filter = query.get("tag", [None])[0]
            q_filter = query.get("q", [None])[0]
            id_prefix = query.get("id_prefix", [None])[0]
            memory_type_filter = query.get("memory_type", [None])[0]
            embedding_filter = query.get("embedding_status", [None])[0]
            quality_filter = query.get("quality_status", [None])[0]
            session_id_filter = query.get("session_id", [None])[0]
            sort = query.get("sort", ["updated_desc"])[0] or "updated_desc"
            date_field = query.get("date_field", [None])[0]
            date_from = query.get("date_from", [None])[0]
            date_to = query.get("date_to", [None])[0]

            if sort not in _ENTITY_SORTS:
                raise ValueError(
                    "sort must be updated_desc, updated_asc, created_desc, or created_asc"
                )
            if date_field and date_field not in ("created", "updated"):
                raise ValueError("date_field must be created or updated")
            if (date_from or date_to) and not date_field:
                raise ValueError("date_field is required when a date bound is supplied")
            if date_from and date_to and date_from > date_to:
                raise ValueError("date_from must not be after date_to")

            where_clauses = []
            params = []
            if owner_id_filter:
                where_clauses.append("owner_id = ?")
                params.append(owner_id_filter)
            if status_filter:
                where_clauses.append("status = ?")
                params.append(status_filter)
            if context_id_filter:
                where_clauses.append("(context_id = ? OR project_id = ?)")
                params.extend([context_id_filter, context_id_filter])
            if is_core_filter is not None:
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
            if tag_filter:
                where_clauses.append(
                    "id IN (SELECT et.entity_id FROM entity_tags et "
                    "JOIN tags t ON et.tag_id = t.id WHERE t.name = ?)"
                )
                params.append(tag_filter)
            if q_filter:
                where_clauses.append("(title LIKE ? OR full_content LIKE ?)")
                params.extend([f"%{q_filter}%", f"%{q_filter}%"])
            if id_prefix:
                where_clauses.append("id LIKE ?")
                params.append(f"{id_prefix}%")
            if memory_type_filter:
                where_clauses.append("memory_type = ?")
                params.append(memory_type_filter)
            if embedding_filter:
                where_clauses.append("embedding_status = ?")
                params.append(embedding_filter)
            if quality_filter:
                where_clauses.append("quality_status = ?")
                params.append(quality_filter)
            if session_id_filter:
                where_clauses.append("(agent_session_id = ? OR last_touched_session_id = ?)")
                params.extend([session_id_filter, session_id_filter])
            if date_from:
                where_clauses.append(f"{date_field}_at >= ?")
                params.append(_utc_day_bound(date_from, end_exclusive=False))
            if date_to:
                where_clauses.append(f"{date_field}_at < ?")
                params.append(_utc_day_bound(date_to, end_exclusive=True))

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
            order_field, order_direction = _ENTITY_SORTS[sort]

            conn = self.get_db_connection()
            cursor = conn.execute(
                f"""
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, context_id, embedding_status, memory_type, quality_score, quality_status, agent_session_id, last_touched_session_id
                FROM entities
                {where_sql}
                ORDER BY {order_field} {order_direction}, id ASC
                LIMIT ? OFFSET ?
            """,
                params + [limit, offset],
            )
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM entities {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            if rows:
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
            else:
                tag_map = {}

            entities = []
            for r in rows:
                entities.append(
                    {
                        "id": r[0],
                        "created_at": r[1],
                        "updated_at": r[2],
                        "last_accessed_at": r[3],
                        "owner_id": r[4],
                        "scope": r[5],
                        "is_core": bool(r[6]),
                        "weight": r[7],
                        "status": r[8],
                        "parent_ids": json.loads(r["parent_ids"]) if r["parent_ids"] else [],
                        "title": r[10],
                        "context_id": r[11],
                        "embedding_status": "archived"
                        if r[8] == "archived"
                        else (r[12] or "pending"),
                        "memory_type": r[13] or "fact",
                        "quality_score": r[14],
                        "quality_status": r[15],
                        "agent_session_id": r[16],
                        "last_touched_session_id": r[17],
                        "tags": tag_map.get(r[0], []),
                    }
                )

            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json(
                {
                    "page": page,
                    "limit": limit,
                    "total_count": total_count,
                    "total_pages": total_pages,
                    "sort": sort,
                    "date_field": date_field,
                    "date_from": date_from,
                    "date_to": date_to,
                    "pagination": {
                        "page": page,
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
