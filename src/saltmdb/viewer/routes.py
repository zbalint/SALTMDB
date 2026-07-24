import os
import sqlite3
import json
import http.server
import urllib.parse
import sys
import logging
from saltmdb.config import get_db_path

logger = logging.getLogger(__name__)
from saltmdb.viewer.templates import get_frontend_html

class SALTMDBHandler(http.server.BaseHTTPRequestHandler):
    """Zero-dependency HTTP Request Handler for the SALTMDB Dashboard Viewer."""
    
    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            self.close_connection = True
            logger.debug("Client connection aborted during request: %s", e)

    def send_json(self, data, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            headers = getattr(self, "headers", None)
            origin = headers.get("Origin", "") if headers else ""
            if origin and (origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected before JSON response was sent: %s", e)

    def send_html(self, html_content, status=200):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected before HTML response was sent: %s", e)

    def do_OPTIONS(self):
        try:
            self.send_response(200)
            headers = getattr(self, "headers", None)
            origin = headers.get("Origin", "") if headers else ""
            if origin and (origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1")):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug("Client disconnected during OPTIONS request: %s", e)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        
        if path == "/api/embeddings_stats":
            self.get_embeddings_stats()
        elif path in ("/api/entities", "/api/entity"):
            self.get_entities(query)
        elif path == "/api/events":
            self.get_events(query)
        elif path == "/api/tags":
            self.get_tags()
        elif path == "/api/locks":
            self.get_locks()
        elif path == "/api/relations":
            self.get_all_relations(query)
        elif path == "/api/relations/graph":
            self.get_relations_graph(query)
        elif path == "/api/stats":
            self.get_stats()
        elif path == "/api/search":
            self.get_search(query)
        elif path.startswith("/api/entities/") or path.startswith("/api/entity/"):
            prefix = "/api/entities/" if path.startswith("/api/entities/") else "/api/entity/"
            entity_id = path.split(prefix)[1]
            if "/lineage" in entity_id:
                eid = entity_id.replace("/lineage", "")
                self.get_lineage(eid)
            else:
                self.get_entity_detail(entity_id)
        elif path == "/" or path == "/index.html":
            self.send_html(get_frontend_html())
        else:
            self.send_json({"error": "Endpoint not found"}, 404)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path == "/api/embeddings/backfill":
            try:
                from saltmdb.domain.services.embedding_service import backfill_pending_embeddings
                count = backfill_pending_embeddings()
                self.send_json({"message": f"Queued {count} pending embeddings for background generation", "count": count})
            except Exception as e:
                logger.error("Error triggering backfill: %s", e, exc_info=True)
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_json({"error": "Endpoint not found"}, 404)


    def get_db_connection(self):
        db_path = None
        if "saltmdb_viewer" in sys.modules:
            db_path = getattr(sys.modules["saltmdb_viewer"], "DB_PATH", None)
        db_path = os.environ.get("SALTMDB_DB_PATH") or db_path or get_db_path()
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_entities(self, query):
        conn = None
        try:
            page = 1
            if "page" in query:
                try:
                    page = int(query["page"][0])
                except ValueError:
                    pass
            page = max(1, page)
            limit = 50
            offset = (page - 1) * limit

            owner_id_filter = query.get("owner_id", [None])[0]
            status_filter = query.get("status", [None])[0]
            context_id_filter = query.get("context_id", [None])[0]
            is_core_filter = query.get("is_core", [None])[0]
            tag_filter = query.get("tag", [None])[0]

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
            if is_core_filter:
                where_clauses.append("is_core = ?")
                params.append(1 if is_core_filter.lower() in ('true', '1', 'yes') else 0)
            if tag_filter:
                where_clauses.append(
                    "id IN (SELECT et.entity_id FROM entity_tags et "
                    "JOIN tags t ON et.tag_id = t.id WHERE t.name = ?)"
                )
                params.append(tag_filter)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            conn = self.get_db_connection()
            cursor = conn.execute(f"""
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, context_id, embedding_status
                FROM entities
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM entities {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            if rows:
                entity_ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in entity_ids)
                tag_cursor = conn.execute(f"""
                    SELECT et.entity_id, t.name
                    FROM entity_tags et
                    JOIN tags t ON et.tag_id = t.id
                    WHERE et.entity_id IN ({placeholders})
                """, entity_ids)
                tag_map = {}
                for eid, tname in tag_cursor.fetchall():
                    tag_map.setdefault(eid, []).append(tname)
            else:
                tag_map = {}

            entities = []
            for r in rows:
                entities.append({
                    "id": r[0],
                    "created_at": r[1],
                    "updated_at": r[2],
                    "last_accessed_at": r[3],
                    "owner_id": r[4],
                    "scope": r[5],
                    "is_core": bool(r[6]),
                    "weight": r[7],
                    "status": r[8],
                    "parent_ids": json.loads(r[9]) if r[9] else [],
                    "title": r[10],
                    "context_id": r[11],
                    "embedding_status": "archived" if r[8] == "archived" else (r[12] or "pending"),
                    "tags": tag_map.get(r[0], [])
                })

            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json({
                "page": page,
                "limit": limit,
                "total_count": total_count,
                "total_pages": total_pages,
                "pagination": {
                    "page": page,
                    "per_page": limit,
                    "total": total_count,
                    "total_pages": total_pages
                },
                "entities": entities
            })
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_events(self, query):
        conn = None
        try:
            page = 1
            if "page" in query:
                try:
                    page = int(query["page"][0])
                except ValueError:
                    pass
            page = max(1, page)
            limit = 50
            offset = (page - 1) * limit

            agent_filter = query.get("agent_id", [None])[0]
            type_filter = query.get("type", [None])[0]
            context_filter = query.get("context_id", [None])[0]

            where = []
            params = []
            if agent_filter:
                where.append("agent_id = ?")
                params.append(agent_filter)
            if type_filter:
                where.append("type = ?")
                params.append(type_filter)
            if context_filter:
                where.append("context_id = ?")
                params.append(context_filter)

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            conn = self.get_db_connection()
            cursor = conn.execute(f"""
                SELECT id, timestamp, agent_id, type, content, error_code, session_id, context_id
                FROM events
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM events {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            events = [{
                "id": r[0],
                "timestamp": r[1],
                "agent_id": r[2],
                "type": r[3],
                "content": r[4],
                "error_code": r[5],
                "session_id": r[6],
                "context_id": r[7]
            } for r in rows]

            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json({
                "page": page,
                "limit": limit,
                "total_count": total_count,
                "total_pages": total_pages,
                "pagination": {
                    "page": page,
                    "per_page": limit,
                    "total": total_count,
                    "total_pages": total_pages
                },
                "events": events
            })
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
            tags = [{
                "id": r[0],
                "name": r[1],
                "canonical_id": r[2],
                "usage_count": r[3]
            } for r in rows]
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
                cursor = conn.execute("SELECT task_name, locked_at, locked_by_pid, last_run_at FROM _system_locks")
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                try:
                    cursor = conn.execute("SELECT task_name, locked_at, locked_by_pid, last_run_at FROM task_locks")
                    rows = cursor.fetchall()
                except sqlite3.OperationalError:
                    pass
            locks = [{
                "task_name": r[0],
                "locked_at": r[1],
                "locked_by_pid": r[2],
                "last_run_at": r[3]
            } for r in rows]
            self.send_json({"locks": locks})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_relations_graph(self, query=None):
        """Returns relations graph (nodes+edges) with node degree, status filtering, and query parameters."""
        query = query or {}
        conn = None
        try:
            exclude_archived = query.get("exclude_archived", ["true"])[0].lower() in ("true", "1", "yes")
            predicate_filter = query.get("predicate", [None])[0]
            limit_str = query.get("limit", ["250"])[0]
            try:
                limit = int(limit_str)
            except ValueError:
                limit = 250

            where_clauses = []
            params = []

            if exclude_archived:
                where_clauses.append("(COALESCE(e1.status, 'raw') != 'archived' AND COALESCE(e2.status, 'raw') != 'archived')")
            if predicate_filter:
                where_clauses.append("r.predicate = ?")
                params.append(predicate_filter)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            conn = self.get_db_connection()
            cursor = conn.execute(f"""
                SELECT r.source_id, COALESCE(e1.title, r.source_id), COALESCE(e1.status, 'raw'),
                       r.target_id, COALESCE(e2.title, r.target_id), COALESCE(e2.status, 'raw'),
                       r.predicate
                FROM relations r
                LEFT JOIN entities e1 ON r.source_id = e1.id
                LEFT JOIN entities e2 ON r.target_id = e2.id
                {where_sql}
                ORDER BY r.created_at DESC
                LIMIT ?
            """, params + [limit])
            rows = cursor.fetchall()

            node_map = {}
            edges = []
            for src_id, src_title, src_status, tgt_id, tgt_title, tgt_status, pred in rows:
                if src_id not in node_map:
                    node_map[src_id] = {"id": src_id, "title": src_title or src_id, "status": src_status, "degree": 0}
                if tgt_id not in node_map:
                    node_map[tgt_id] = {"id": tgt_id, "title": tgt_title or tgt_id, "status": tgt_status, "degree": 0}

                node_map[src_id]["degree"] += 1
                node_map[tgt_id]["degree"] += 1
                edges.append({"source": src_id, "target": tgt_id, "predicate": pred})

            nodes = list(node_map.values())
            self.send_json({"nodes": nodes, "edges": edges, "total_edges": len(edges), "total_nodes": len(nodes)})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
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
            where_sql = "WHERE r.predicate = ?" if predicate_filter else ""
            params = [predicate_filter] if predicate_filter else []

            conn = self.get_db_connection()
            cursor = conn.execute(f"""
                SELECT r.id, r.source_id, COALESCE(e1.title, r.source_id), r.target_id, COALESCE(e2.title, r.target_id), r.predicate, r.created_at
                FROM relations r
                LEFT JOIN entities e1 ON r.source_id = e1.id
                LEFT JOIN entities e2 ON r.target_id = e2.id
                {where_sql}
                ORDER BY r.created_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            rows = cursor.fetchall()

            count_cursor = conn.execute(f"SELECT COUNT(*) FROM relations r {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            relations = [{
                "id": r[0],
                "source_id": r[1],
                "source_title": r[2] or "Unknown",
                "target_id": r[3],
                "target_title": r[4] or "Unknown",
                "predicate": r[5],
                "created_at": r[6]
            } for r in rows]

            total_pages = (total_count + limit - 1) // limit if limit > 0 else 0
            self.send_json({
                "page": page,
                "limit": limit,
                "total_count": total_count,
                "total_pages": total_pages,
                "pagination": {
                    "page": page,
                    "per_page": limit,
                    "total": total_count,
                    "total_pages": total_pages
                },
                "relations": relations
            })
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_stats(self):
        conn = None
        try:
            conn = self.get_db_connection()
            stats = {}
            for status in ['raw', 'consolidated', 'archived']:
                cur = conn.execute("SELECT COUNT(*) FROM entities WHERE status = ?", (status,))
                stats[f"{status}_count"] = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM entities")
            stats["total_entities"] = cur.fetchone()[0]
            stats["active_entities"] = stats["raw_count"] + stats["consolidated_count"]

            for scope in ['shared', 'private']:
                cur = conn.execute("SELECT COUNT(*) FROM entities WHERE scope = ? AND status != 'archived'", (scope,))
                stats[f"scope_{scope}"] = cur.fetchone()[0]

            cur = conn.execute("SELECT COUNT(*) FROM events")
            stats["total_events"] = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM relations")
            stats["total_relations"] = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM tags")
            stats["total_tags"] = cur.fetchone()[0]
            for emb_status in ['ready', 'pending', 'failed']:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE embedding_status = ? AND status != 'archived'",
                    (emb_status,)
                )
                stats[f"embeddings_{emb_status}"] = cur.fetchone()[0]

            # Database file size
            try:
                db_path = get_db_path()
                if os.path.exists(db_path):
                    stats["db_size_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 2)
                else:
                    stats["db_size_mb"] = 0.0
            except Exception:
                stats["db_size_mb"] = 0.0

            self.send_json(stats)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_search(self, query):
        conn = None
        try:
            q = query.get("q", [""])[0].strip()
            if not q:
                self.send_json({"results": []})
                return
            from saltmdb.domain.services.memory_service import search_memory
            results = search_memory(query_keywords=q, limit=20)
            self.send_json({"query": q, "results": results})
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)

    def get_embeddings_stats(self):
        conn = None
        try:
            conn = self.get_db_connection()
            counts = {}
            for emb_status in ['pending', 'ready', 'failed', 'archived']:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE embedding_status = ?",
                    (emb_status,)
                )
                counts[emb_status] = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE (embedding_status IS NULL OR embedding_status = '') AND status != 'archived'"
            )
            counts['null'] = cur.fetchone()[0]
            self.send_json(counts)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_lineage(self, entity_id):
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.execute("SELECT id, title, status FROM entities WHERE id = ? OR id LIKE ? OR title = ? OR title LIKE ? ORDER BY status ASC LIMIT 1", (entity_id, f"{entity_id}%", entity_id, f"%{entity_id}%"))
            row = cur.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return
            entity_id, root_title, root_status = row[0], row[1], row[2]

            cur = conn.execute("""
                WITH RECURSIVE lineage(entity_id, depth, path) AS (
                    SELECT ?, 0, ''
                    UNION ALL
                    SELECT r.target_id, l.depth + 1, l.path || '/' || l.entity_id
                    FROM relations r
                    JOIN lineage l ON r.source_id = l.entity_id
                    WHERE r.predicate = 'consolidated_from' AND l.depth < 10
                )
                SELECT DISTINCT l.entity_id, l.depth, e.title, e.status, e.owner_id, e.updated_at
                FROM lineage l
                JOIN entities e ON l.entity_id = e.id
                ORDER BY l.depth ASC
            """, (entity_id,))
            nodes = [{
                "id": r[0],
                "depth": r[1],
                "title": r[2],
                "status": r[3],
                "owner_id": r[4],
                "updated_at": r[5],
                "generation_depth": r[1]
            } for r in cur.fetchall()]

            cur = conn.execute("""
                SELECT r.source_id, r.target_id, r.predicate
                FROM relations r
                WHERE r.predicate = 'consolidated_from'
                AND (r.source_id IN (SELECT entity_id FROM (WITH RECURSIVE l(entity_id, depth) AS (
                    SELECT ?, 0
                    UNION ALL SELECT r2.target_id, l.depth+1 FROM relations r2 JOIN l ON r2.source_id = l.entity_id WHERE r2.predicate = 'consolidated_from' AND l.depth < 10
                ) SELECT entity_id FROM l))
                OR r.target_id IN (SELECT entity_id FROM (WITH RECURSIVE l(entity_id, depth) AS (
                    SELECT ?, 0
                    UNION ALL SELECT r2.target_id, l.depth+1 FROM relations r2 JOIN l ON r2.source_id = l.entity_id WHERE r2.predicate = 'consolidated_from' AND l.depth < 10
                ) SELECT entity_id FROM l)))
            """, (entity_id, entity_id))
            edges = [{
                "source": r[0],
                "target": r[1],
                "predicate": r[2]
            } for r in cur.fetchall()]

            self.send_json({
                "root_id": entity_id,
                "root_title": root_title,
                "root_status": root_status,
                "nodes": nodes,
                "edges": edges,
                "ancestry_tree": nodes
            })
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
            cursor = conn.execute("""
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id, embedding_status
                FROM entities WHERE id = ?
            """, (entity_id,))
            row = cursor.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return

            tag_cursor = conn.execute("""
                SELECT t.name FROM entity_tags et
                JOIN tags t ON et.tag_id = t.id
                WHERE et.entity_id = ?
            """, (entity_id,))
            tags = [r[0] for r in tag_cursor.fetchall()]

            rel_cursor = conn.execute("""
                SELECT r.id, r.source_id, e1.title, r.target_id, e2.title, r.predicate
                FROM relations r
                LEFT JOIN entities e1 ON r.source_id = e1.id
                LEFT JOIN entities e2 ON r.target_id = e2.id
                WHERE r.source_id = ? OR r.target_id = ?
            """, (entity_id, entity_id))
            
            outgoing = []
            incoming = []
            all_rels = []
            for r in rel_cursor.fetchall():
                item = {
                    "id": r[0],
                    "source_id": r[1],
                    "source_title": r[2] or "Unknown",
                    "target_id": r[3],
                    "target_title": r[4] or "Unknown",
                    "predicate": r[5]
                }
                all_rels.append(item)
                if r[1] == entity_id:
                    outgoing.append(item)
                if r[3] == entity_id:
                    incoming.append(item)

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
                "parent_ids": json.loads(row[9]) if row[9] else [],
                "title": row[10],
                "full_content": row[11],
                "valid_from": row[12],
                "valid_to": row[13],
                "metadata": json.loads(row[14]) if row[14] else None,
                "project_id": row[15],
                "context_id": row[16],
                "embedding_status": "archived" if row[8] == "archived" else (row[17] or "pending"),
                "tags": tags,
                "relations": {
                    "outgoing": outgoing,
                    "incoming": incoming,
                    "all": all_rels
                }
            }
            self.send_json(detail)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
