"""Relation graph/list endpoints.

GET /api/relations, GET /api/relations/graph, GET /api/relations/neighborhood,
and GET /api/entities/{id}/relations (single-entity relation paging).
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from saltmdb.viewer.routes._shared import (
    MAX_RELATION_LIMIT,
    _bounded_query_int,
    active_relation_filter,
)

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)
_PREDICATE_FILTER_SQL = "r.predicate = ?"
_VIEWER_HANDLER_ERROR_LOG = "SALTMDB Viewer handler error: %s"
_INTERNAL_SERVER_ERROR = "Internal server error. Check viewer logs for details."


def _neighborhood_options(query):
    root_id = query.get("entity_id", [""])[0]
    if not root_id or len(root_id) > 512:
        raise ValueError("entity_id is required and must be at most 512 characters")
    depth = _bounded_query_int(query, "depth", 1, 1, 2)
    max_nodes = _bounded_query_int(query, "max_nodes", 50, 1, 50)
    max_edges = _bounded_query_int(query, "max_edges", 100, 1, 100)
    include_archived = query.get("exclude_archived", ["true"])[0].lower() not in (
        "true",
        "1",
        "yes",
    )
    predicate = query.get("predicate", [None])[0]
    raw_as_of = query.get("as_of", [None])[0]
    if raw_as_of:
        try:
            as_of = datetime.fromisoformat(raw_as_of.replace("Z", "+00:00")).isoformat()
        except ValueError as exc:
            raise ValueError("as_of must be an ISO-8601 timestamp") from exc
    else:
        as_of = datetime.now(UTC).isoformat()
    return root_id, depth, max_nodes, max_edges, include_archived, predicate, as_of


def _neighborhood_fetch_rows(
    conn, frontier, max_edges, current_edges, include_archived, predicate, as_of
):
    placeholders = ",".join("?" for _ in frontier)
    clauses = [f"(r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders}))"]
    params = list(frontier) * 2
    if predicate:
        clauses.append(_PREDICATE_FILTER_SQL)
        params.append(predicate)
    validity_sql, validity_params = active_relation_filter(as_of)
    clauses.append(validity_sql)
    params.extend(validity_params)
    if not include_archived:
        clauses.append(
            "COALESCE(s.status, 'raw') != 'archived' AND COALESCE(t.status, 'raw') != 'archived'"
        )
    remaining_edges = max_edges - len(current_edges)
    return conn.execute(
        f"""
        SELECT r.id, r.source_id, r.target_id, r.predicate, r.created_at,
               s.title AS source_title, s.status AS source_status,
               t.title AS target_title, t.status AS target_status
        FROM relations r
        LEFT JOIN entities s ON s.id = r.source_id
        LEFT JOIN entities t ON t.id = r.target_id
        WHERE {" AND ".join(clauses)}
        ORDER BY r.created_at ASC, r.id ASC
        LIMIT ?
        """,
        params + [remaining_edges + 1],
    ).fetchall()


def _neighborhood_row_payload(row):
    return {
        "id": row[0],
        "source": row[1],
        "target": row[2],
        "predicate": row[3],
        "created_at": row[4],
        "source_title": row[5] or row[1],
        "source_status": row[6] or "raw",
        "target_title": row[7] or row[2],
        "target_status": row[8] or "raw",
    }


def _neighborhood_add_row(row, frontier, seen, edges, emitted_ids, max_nodes, max_edges):
    if len(edges) >= max_edges or row[0] in emitted_ids:
        return None
    other = row[2] if row[1] in frontier else row[1]
    if other not in seen and len(seen) >= max_nodes:
        return None
    edges.append(_neighborhood_row_payload(row))
    emitted_ids.add(row[0])
    if other not in seen:
        seen.add(other)
        return other
    return None


def _neighborhood_add_rows(rows, frontier, seen, edges, emitted_ids, max_nodes, max_edges):
    next_frontier = set()
    for row in rows:
        other = _neighborhood_add_row(row, frontier, seen, edges, emitted_ids, max_nodes, max_edges)
        if other is not None:
            next_frontier.add(other)
    return next_frontier


def _neighborhood_edges(
    conn, root_id, depth, max_nodes, max_edges, include_archived, predicate, as_of
):
    seen = {root_id}
    frontier = {root_id}
    edges: list[dict[str, Any]] = []
    emitted_ids: set[str] = set()
    for _ in range(depth):
        if not frontier or len(edges) >= max_edges or len(seen) >= max_nodes:
            break
        rows = _neighborhood_fetch_rows(
            conn, frontier, max_edges, edges, include_archived, predicate, as_of
        )
        frontier = _neighborhood_add_rows(
            rows, frontier, seen, edges, emitted_ids, max_nodes, max_edges
        )
    return seen, edges


def _neighborhood_count(conn, root_id, depth, include_archived, predicate, as_of):
    count_validity_sql, count_params = active_relation_filter(as_of)
    count_filters = [count_validity_sql]
    if predicate:
        count_filters.append(_PREDICATE_FILTER_SQL)
        count_params.append(predicate)
    if not include_archived:
        count_filters.append(
            "COALESCE(s.status, 'raw') != 'archived' AND COALESCE(t.status, 'raw') != 'archived'"
        )
    filter_sql = " AND ".join(count_filters)
    return conn.execute(
        f"""WITH RECURSIVE frontier(id, depth) AS (
                SELECT ?, 0
                UNION
                SELECT CASE WHEN r.source_id = frontier.id THEN r.target_id ELSE r.source_id END,
                       frontier.depth + 1
                FROM relations r JOIN frontier ON r.source_id = frontier.id OR r.target_id = frontier.id
                LEFT JOIN entities s ON s.id = r.source_id LEFT JOIN entities t ON t.id = r.target_id
                WHERE frontier.depth < ? AND {filter_sql}
            )
            SELECT COUNT(DISTINCT r.id) FROM relations r JOIN frontier ON r.source_id = frontier.id OR r.target_id = frontier.id
            LEFT JOIN entities s ON s.id = r.source_id LEFT JOIN entities t ON t.id = r.target_id
            WHERE frontier.depth < ? AND {filter_sql}""",
        [root_id, depth] + count_params + [depth] + count_params,
    ).fetchone()[0]


class RelationsMixin(ViewerHandlerProtocol):
    """Provides get_relations_graph/get_all_relations/get_relations_neighborhood/get_entity_relations."""

    def get_relations_graph(self, query=None):
        """Returns relations graph (nodes+edges) with node degree, status filtering, and query parameters."""
        query = query or {}
        conn = None
        try:
            exclude_archived = query.get("exclude_archived", ["true"])[0].lower() in (
                "true",
                "1",
                "yes",
            )
            predicate_filter = query.get("predicate", [None])[0]
            limit_str = query.get("limit", ["250"])[0]
            try:
                limit = int(limit_str)
            except ValueError:
                limit = 250

            where_clauses = []
            params = []

            if exclude_archived:
                where_clauses.append(
                    "(COALESCE(e1.status, 'raw') != 'archived' AND COALESCE(e2.status, 'raw') != 'archived')"
                )
            if predicate_filter:
                where_clauses.append(_PREDICATE_FILTER_SQL)
                params.append(predicate_filter)
            validity_sql, validity_params = active_relation_filter()
            where_clauses.append(validity_sql)
            params.extend(validity_params)

            where_sql = "WHERE " + " AND ".join(where_clauses)

            conn = self.get_db_connection()
            cursor = conn.execute(
                f"""
                SELECT r.source_id, COALESCE(e1.title, r.source_id), COALESCE(e1.status, 'raw'),
                       r.target_id, COALESCE(e2.title, r.target_id), COALESCE(e2.status, 'raw'),
                       r.predicate
                FROM relations r
                LEFT JOIN entities e1 ON r.source_id = e1.id
                LEFT JOIN entities e2 ON r.target_id = e2.id
                {where_sql}
                ORDER BY r.created_at DESC
                LIMIT ?
            """,
                params + [limit],
            )
            rows = cursor.fetchall()

            node_map = {}
            edges: list[dict[str, Any]] = []
            for src_id, src_title, src_status, tgt_id, tgt_title, tgt_status, pred in rows:
                if src_id not in node_map:
                    node_map[src_id] = {
                        "id": src_id,
                        "title": src_title or src_id,
                        "status": src_status,
                        "degree": 0,
                    }
                if tgt_id not in node_map:
                    node_map[tgt_id] = {
                        "id": tgt_id,
                        "title": tgt_title or tgt_id,
                        "status": tgt_status,
                        "degree": 0,
                    }

                node_map[src_id]["degree"] += 1
                node_map[tgt_id]["degree"] += 1
                edges.append({"source": src_id, "target": tgt_id, "predicate": pred})

            nodes = list(node_map.values())
            self.send_json(
                {
                    "nodes": nodes,
                    "edges": edges,
                    "total_edges": len(edges),
                    "total_nodes": len(nodes),
                }
            )
        except Exception as e:
            logger.error(_VIEWER_HANDLER_ERROR_LOG, e, exc_info=True)
            self.send_json({"error": _INTERNAL_SERVER_ERROR}, 500)
        finally:
            if conn:
                conn.close()

    def get_all_relations(self, query):
        conn = None
        try:
            page = 1
            if "page" in query:
                try:
                    page = int(query["page"][0])
                except ValueError:
                    pass
            page = max(1, page)
            limit = 200
            offset = (page - 1) * limit

            predicate_filter = query.get("predicate", [None])[0]
            validity_sql, validity_params = active_relation_filter()
            where_sql = "WHERE " + (
                "r.predicate = ? AND " + validity_sql if predicate_filter else validity_sql
            )
            params = ([predicate_filter] if predicate_filter else []) + validity_params

            conn = self.get_db_connection()
            cursor = conn.execute(
                f"""
                SELECT r.id, r.source_id, COALESCE(e1.title, r.source_id), r.target_id, COALESCE(e2.title, r.target_id), r.predicate, r.created_at
                FROM relations r
                LEFT JOIN entities e1 ON r.source_id = e1.id
                LEFT JOIN entities e2 ON r.target_id = e2.id
                {where_sql}
                ORDER BY r.created_at DESC
                LIMIT ? OFFSET ?
            """,
                params + [limit, offset],
            )
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM relations r {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            relations = [
                {
                    "id": r[0],
                    "source_id": r[1],
                    "source_title": r[2] or "Unknown",
                    "target_id": r[3],
                    "target_title": r[4] or "Unknown",
                    "predicate": r[5],
                    "created_at": r[6],
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
                    "relations": relations,
                }
            )
        except Exception as e:
            logger.error(_VIEWER_HANDLER_ERROR_LOG, e, exc_info=True)
            self.send_json({"error": _INTERNAL_SERVER_ERROR}, 500)
        finally:
            if conn:
                conn.close()

    def get_relations_neighborhood(self, query):
        """Return a bounded, deterministic local relation neighborhood."""
        conn = None
        try:
            options = _neighborhood_options(query)
            root_id, depth, max_nodes, max_edges, include_archived, predicate, as_of = options
            conn = self.get_db_connection()
            root = conn.execute(
                "SELECT id, title, status FROM entities WHERE id = ?", (root_id,)
            ).fetchone()
            if not root:
                self.send_json({"error": "Entity not found"}, 404)
                return
            seen, edges = _neighborhood_edges(
                conn, root_id, depth, max_nodes, max_edges, include_archived, predicate, as_of
            )
            node_rows = conn.execute(
                f"SELECT id, title, status FROM entities WHERE id IN ({','.join('?' for _ in seen)}) ORDER BY title, id",
                list(seen),
            ).fetchall()
            total_matching_edges = _neighborhood_count(
                conn, root_id, depth, include_archived, predicate, as_of
            )
            self.send_json(
                {
                    "root_id": root_id,
                    "nodes": [
                        {"id": row[0], "title": row[1], "status": row[2]} for row in node_rows
                    ],
                    "edges": edges,
                    "returned_edges": len(edges),
                    "total_matching_edges": total_matching_edges,
                    "truncated": total_matching_edges > len(edges),
                    "omitted_edge_count": max(0, total_matching_edges - len(edges)),
                    "has_more": total_matching_edges > len(edges),
                }
            )
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error(_VIEWER_HANDLER_ERROR_LOG, e, exc_info=True)
            self.send_json({"error": _INTERNAL_SERVER_ERROR}, 500)
        finally:
            if conn:
                conn.close()

    def get_entity_relations(self, entity_id, query):
        """Return one bounded page of an entity's incoming/outgoing relations."""
        conn = None
        try:
            direction = query.get("direction", ["all"])[0]
            if direction not in {"all", "incoming", "outgoing"}:
                self.send_json({"error": "direction must be all, incoming, or outgoing"}, 400)
                return
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_RELATION_LIMIT)
            conn = self.get_db_connection()
            where = "r.source_id = ? OR r.target_id = ?"
            params = [entity_id, entity_id]
            if direction == "incoming":
                where, params = "r.target_id = ?", [entity_id]
            elif direction == "outgoing":
                where, params = "r.source_id = ?", [entity_id]
            validity_sql, validity_params = active_relation_filter()
            where = f"({where}) AND {validity_sql}"
            params = params + validity_params
            total = conn.execute(
                f"SELECT COUNT(*) FROM relations r WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT r.id, r.source_id, COALESCE(s.title, r.source_id), r.target_id,
                           COALESCE(t.title, r.target_id), r.predicate, r.created_at
                    FROM relations r LEFT JOIN entities s ON s.id = r.source_id
                    LEFT JOIN entities t ON t.id = r.target_id WHERE {where}
                    ORDER BY r.created_at DESC, r.id DESC LIMIT ? OFFSET ?""",
                params + [limit, (page - 1) * limit],
            ).fetchall()
            self.send_json(
                {
                    "entity_id": entity_id,
                    "direction": direction,
                    "page": page,
                    "limit": limit,
                    "total_count": total,
                    "total_pages": (total + limit - 1) // limit,
                    "relations": [
                        {
                            "id": r[0],
                            "source_id": r[1],
                            "source_title": r[2],
                            "target_id": r[3],
                            "target_title": r[4],
                            "predicate": r[5],
                            "created_at": r[6],
                        }
                        for r in rows
                    ],
                }
            )
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error(_VIEWER_HANDLER_ERROR_LOG, e, exc_info=True)
            self.send_json({"error": _INTERNAL_SERVER_ERROR}, 500)
        finally:
            if conn:
                conn.close()
