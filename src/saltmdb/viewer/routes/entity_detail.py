"""Single-entity endpoints: GET /api/entities/{id} and GET /api/entities/{id}/lineage."""

import json
import logging
import urllib.parse
from typing import TYPE_CHECKING

from saltmdb.domain.services import relation_service

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


class EntityDetailMixin(ViewerHandlerProtocol):
    """Provides get_lineage() and get_entity_detail(); mixed into SALTMDBHandler elsewhere."""

    def get_lineage(self, entity_id):
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.execute(
                """
                SELECT id, title, status FROM entities
                WHERE id = ? OR id LIKE ? OR title = ? OR title LIKE ?
                ORDER BY
                    CASE WHEN id = ? THEN 0 WHEN title = ? THEN 1 ELSE 2 END ASC,
                    CASE status WHEN 'raw' THEN 0 WHEN 'consolidated' THEN 1 WHEN 'archived' THEN 2 ELSE 3 END ASC,
                    updated_at DESC
                LIMIT 1
            """,
                (entity_id, f"{entity_id}%", entity_id, f"%{entity_id}%", entity_id, entity_id),
            )
            row = cur.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return
            entity_id, root_title, root_status = row[0], row[1], row[2]

            # Phase 3 exposes a direction-specific graph operation.  Keep the
            # legacy call as a compatibility fallback for an in-process viewer
            # during rolling upgrades; both shapes are normalized for the panel.
            get_lineage = getattr(relation_service, "get_lineage", None)
            if get_lineage is not None:
                ancestor_result = get_lineage(
                    entity_id=entity_id,
                    direction="ancestors",
                    max_depth=10,
                    db_connection=conn,
                )
                if isinstance(ancestor_result, dict) and ancestor_result.get("error"):
                    self.send_json({"error": ancestor_result["error"]}, 404)
                    return
                raw_nodes: list[tuple[dict, str]] = []
                direction_nodes = ancestor_result.get("nodes", [])
                raw_nodes.extend(
                    (node, "ancestors") for node in direction_nodes if isinstance(node, dict)
                )
            else:
                lineage_result = relation_service.analyze_lineage(
                    entity_id=entity_id, db_connection=conn
                )
                if lineage_result.get("error"):
                    self.send_json({"error": lineage_result["error"]}, 404)
                    return
                raw_nodes = [(node, "ancestors") for node in lineage_result.get("ancestors", [])]

            nodes = []
            seen = set()
            for node, direction in raw_nodes:
                node_id = node.get("id")
                if not node_id or node_id in seen:
                    continue
                seen.add(node_id)
                depth = node.get("depth", node.get("generation_depth", 0))
                nodes.append(
                    {
                        "id": node_id,
                        "depth": depth,
                        "generation_depth": node.get("generation_depth", depth),
                        "title": node.get("title"),
                        "status": node.get("status"),
                        "owner_id": node.get("owner_id"),
                        "updated_at": node.get("updated_at"),
                        "direction": node.get("direction", direction),
                    }
                )
            self.send_json(
                {
                    "root_id": entity_id,
                    "root_title": root_title,
                    "root_status": root_status,
                    "nodes": nodes,
                }
            )
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_entity_detail(self, entity_id):
        conn = None
        try:
            conn = self.get_db_connection()
            cursor = conn.execute(
                """
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id, embedding_status, memory_type, quality_score, quality_status, quality_flags
                FROM entities WHERE id = ?
            """,
                (entity_id,),
            )
            row = cursor.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return

            tag_cursor = conn.execute(
                """
                SELECT t.name FROM entity_tags et
                JOIN tags t ON et.tag_id = t.id
                WHERE et.entity_id = ?
            """,
                (entity_id,),
            )
            tags = [r[0] for r in tag_cursor.fetchall()]

            relation_counts = conn.execute(
                """SELECT SUM(CASE WHEN source_id = ? THEN 1 ELSE 0 END),
                          SUM(CASE WHEN target_id = ? THEN 1 ELSE 0 END)
                   FROM relations WHERE source_id = ? OR target_id = ?""",
                (entity_id, entity_id, entity_id, entity_id),
            ).fetchone()

            def relation_preview(direction):
                column = "source_id" if direction == "outgoing" else "target_id"
                rows = conn.execute(
                    f"""SELECT r.id, r.source_id, e1.title, r.target_id, e2.title, r.predicate
                        FROM relations r LEFT JOIN entities e1 ON r.source_id = e1.id
                        LEFT JOIN entities e2 ON r.target_id = e2.id WHERE r.{column} = ?
                        ORDER BY r.created_at DESC, r.id DESC LIMIT 10""",
                    (entity_id,),
                ).fetchall()
                return [
                    {
                        "id": r[0],
                        "source_id": r[1],
                        "source_title": r[2] or "Unknown",
                        "target_id": r[3],
                        "target_title": r[4] or "Unknown",
                        "predicate": r[5],
                    }
                    for r in rows
                ]

            outgoing = relation_preview("outgoing")
            incoming = relation_preview("incoming")
            all_rels = outgoing + incoming

            detail = {
                "id": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "last_accessed_at": row[3],
                "owner_id": row[4],
                "scope": row[5],
                "is_core": bool(row[6]),
                "weight": row[7],
                "status": row[8],
                "parent_ids": json.loads(row["parent_ids"]) if row["parent_ids"] else [],
                "title": row[10],
                "full_content": row[11],
                "valid_from": row[12],
                "valid_to": row[13],
                "metadata": json.loads(row[14]) if row[14] else None,
                "project_id": row[15],
                "context_id": row[16],
                "embedding_status": "archived" if row[8] == "archived" else (row[17] or "pending"),
                "memory_type": row[18] or "fact",
                "quality_score": row[19],
                "quality_status": row[20],
                "quality_flags": json.loads(row[21]) if row[21] else [],
                "tags": tags,
                "relations": {
                    "outgoing": outgoing,
                    "incoming": incoming,
                    "all": all_rels,
                    "outgoing_count": relation_counts[0] or 0,
                    "incoming_count": relation_counts[1] or 0,
                    "list_url": f"/api/entities/{urllib.parse.quote(entity_id, safe='')}/relations",
                },
            }
            self.send_json(detail)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
