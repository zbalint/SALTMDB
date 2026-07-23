import sqlite3
import os
import json
import http.server
import socketserver
import urllib.parse
from datetime import datetime

# Resolve central database path
default_dir = os.path.expanduser("~/.saltmdb")
DB_PATH = os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))
__version__ = "0.1.0-alpha.25"

import sys

PORT = int(os.environ.get("SALTMDB_VIEWER_PORT", 8080))
for idx, arg in enumerate(sys.argv):
    if arg == "--port" and idx + 1 < len(sys.argv):
        try:
            PORT = int(sys.argv[idx + 1])
        except ValueError:
            pass

class SALTMDBHandler(http.server.BaseHTTPRequestHandler):
    # Disable logging to stdout to keep terminal clean
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def send_html(self, html_content, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        
        # API Endpoints
        if path == "/api/entities":
            self.get_entities(query)
        elif path == "/api/events":
            self.get_events(query)
        elif path == "/api/tags":
            self.get_tags()
        elif path == "/api/locks":
            self.get_locks()
        elif path == "/api/relations":
            self.get_all_relations(query)
        elif path == "/api/stats":
            self.get_stats()
        elif path == "/api/search":
            self.get_search(query)
        elif path.startswith("/api/lineage/"):
            entity_id = path[len("/api/lineage/"):]
            self.get_lineage(entity_id)
        elif path.startswith("/api/entity/"):
            entity_id = path.split("/")[-1]
            self.get_entity_detail(entity_id)
        # Frontend Landing SPA
        elif path == "/" or path == "/index.html":
            self.serve_frontend()
        else:
            self.send_response(404)
            self.end_headers()

    def get_db_connection(self):
        return sqlite3.connect(DB_PATH, timeout=5.0)

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

            # Server-side filters
            owner_id_filter = query.get("owner_id", [None])[0]
            status_filter = query.get("status", [None])[0]
            context_id_filter = query.get("context_id", [None])[0]
            is_core_filter = query.get("is_core", [None])[0]

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
                where_clauses.append("is_core = ?")
                params.append(1 if is_core_filter.lower() in ('true', '1', 'yes') else 0)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            conn = self.get_db_connection()
            cursor = conn.execute(f"""
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, context_id
                FROM entities
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            rows = cursor.fetchall()

            # Fetch total count for pagination
            count_cursor = conn.execute(f"SELECT COUNT(*) FROM entities {where_sql}", params)
            total_count = count_cursor.fetchone()[0]

            # Fetch tags in one batch query
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
                    "tags": tag_map.get(r[0], [])
                })
            self.send_json({
                "entities": entities,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total_count,
                    "pages": (total_count + limit - 1) // limit
                },
                "filters": {
                    "owner_id": owner_id_filter,
                    "status": status_filter,
                    "context_id": context_id_filter,
                    "is_core": is_core_filter
                }
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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
            limit = 100
            offset = (page - 1) * limit

            # Server-side filters
            type_filter = query.get("type", [None])[0]
            agent_id_filter = query.get("agent_id", [None])[0]
            session_id_filter = query.get("session_id", [None])[0]

            where_clauses = []
            params = []
            if type_filter:
                where_clauses.append("type = ?")
                params.append(type_filter)
            if agent_id_filter:
                where_clauses.append("agent_id = ?")
                params.append(agent_id_filter)
            if session_id_filter:
                where_clauses.append("session_id = ?")
                params.append(session_id_filter)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

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
            self.send_json({
                "events": events,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total_count,
                    "pages": (total_count + limit - 1) // limit
                }
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_tags(self):
        conn = None
        try:
            conn = self.get_db_connection()
            # Fix N+1: use single aggregated JOIN query
            cursor = conn.execute("""
                SELECT t.id, t.name, t.canonical_id, p.name as canonical_name,
                       COUNT(et.entity_id) as entity_count
                FROM tags t
                LEFT JOIN tags p ON t.canonical_id = p.id
                LEFT JOIN entity_tags et ON et.tag_id = t.id
                GROUP BY t.id, t.name, t.canonical_id, p.name
                ORDER BY entity_count DESC, t.name ASC
            """)
            rows = cursor.fetchall()

            tags = [{
                "id": r[0],
                "name": r[1],
                "canonical_id": r[2],
                "canonical_name": r[3],
                "count": r[4]
            } for r in rows]
            self.send_json(tags)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_locks(self):
        conn = None
        try:
            conn = self.get_db_connection()
            cursor = conn.execute("SELECT task_name, locked_at, locked_by_pid, last_run_at FROM _system_locks")
            rows = cursor.fetchall()
            
            locks = [{
                "task_name": r[0],
                "locked_at": r[1],
                "locked_by_pid": r[2],
                "last_run_at": r[3]
            } for r in rows]
            self.send_json(locks)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_entity_detail(self, entity_id):
        conn = None
        try:
            conn = self.get_db_connection()
            # 1. Fetch all entity fields
            cursor = conn.execute("""
                SELECT id, full_content, title, owner_id, scope, is_core, weight, status,
                       parent_ids, created_at, updated_at, last_accessed_at,
                       valid_from, valid_to, metadata, project_id, context_id
                FROM entities WHERE id = ?
            """, (entity_id,))
            row = cursor.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return

            eid, full_content, title, owner_id, scope, is_core, weight, status, \
                parent_ids, created_at, updated_at, last_accessed_at, \
                valid_from, valid_to, metadata, project_id, context_id = row

            # 2. Fetch tags
            tag_cursor = conn.execute("""
                SELECT t.name FROM tags t
                JOIN entity_tags et ON et.tag_id = t.id
                WHERE et.entity_id = ?
            """, (entity_id,))
            tags = [r[0] for r in tag_cursor.fetchall()]

            # 3. Fetch outgoing relations (this entity is the source)
            cursor = conn.execute("""
                SELECT r.id, r.predicate, r.target_id, COALESCE(e.title, r.target_id), r.valid_from, r.valid_to
                FROM relations r
                LEFT JOIN entities e ON r.target_id = e.id
                WHERE r.source_id = ?
            """, (entity_id,))
            outgoing = [{
                "relation_id": r[0],
                "predicate": r[1],
                "target_id": r[2],
                "target_title": r[3],
                "valid_from": r[4],
                "valid_to": r[5]
            } for r in cursor.fetchall()]

            # 4. Fetch incoming relations (this entity is the target)
            cursor = conn.execute("""
                SELECT r.id, r.predicate, r.source_id, COALESCE(e.title, r.source_id), r.valid_from, r.valid_to
                FROM relations r
                LEFT JOIN entities e ON r.source_id = e.id
                WHERE r.target_id = ?
            """, (entity_id,))
            incoming = [{
                "relation_id": r[0],
                "predicate": r[1],
                "source_id": r[2],
                "source_title": r[3],
                "valid_from": r[4],
                "valid_to": r[5]
            } for r in cursor.fetchall()]

            # Parse metadata safely
            try:
                metadata_parsed = json.loads(metadata) if metadata else None
            except Exception:
                metadata_parsed = None

            self.send_json({
                "id": eid,
                "title": title,
                "full_content": full_content,
                "owner_id": owner_id,
                "scope": scope,
                "is_core": bool(is_core),
                "weight": weight,
                "status": status,
                "parent_ids": json.loads(parent_ids) if parent_ids else [],
                "created_at": created_at,
                "updated_at": updated_at,
                "last_accessed_at": last_accessed_at,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "metadata": metadata_parsed,
                "project_id": project_id,
                "context_id": context_id,
                "tags": tags,
                "relations": {
                    "outgoing": outgoing,
                    "incoming": incoming
                }
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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
                SELECT r.id, r.predicate, r.source_id, COALESCE(e1.title, r.source_id), r.target_id, COALESCE(e2.title, r.target_id), r.valid_from, r.valid_to
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
                "predicate": r[1],
                "source_id": r[2],
                "source_title": r[3],
                "target_id": r[4],
                "target_title": r[5],
                "valid_from": r[6],
                "valid_to": r[7]
            } for r in rows]
            self.send_json({
                "relations": relations,
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total_count,
                    "pages": (total_count + limit - 1) // limit
                }
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_stats(self):
        """Return summary statistics for the dashboard header."""
        conn = None
        try:
            conn = self.get_db_connection()
            # Entity counts by status
            cur = conn.execute("""
                SELECT status, COUNT(*) FROM entities GROUP BY status
            """)
            entity_counts = {r[0]: r[1] for r in cur.fetchall()}

            # Event counts by type
            cur = conn.execute("""
                SELECT type, COUNT(*) FROM events GROUP BY type
            """)
            event_counts = {r[0]: r[1] for r in cur.fetchall()}

            # Total relations
            cur = conn.execute("SELECT COUNT(*) FROM relations")
            relation_count = cur.fetchone()[0]

            # Total tags
            cur = conn.execute("SELECT COUNT(*) FROM tags WHERE canonical_id IS NULL")
            tag_count = cur.fetchone()[0]

            # Total events
            cur = conn.execute("SELECT COUNT(*) FROM events")
            total_events = cur.fetchone()[0]

            # Unique owners
            cur = conn.execute("SELECT COUNT(DISTINCT owner_id) FROM entities WHERE owner_id IS NOT NULL AND status != 'archived'")
            owner_count = cur.fetchone()[0]

            # Unique contexts
            cur = conn.execute("SELECT COUNT(DISTINCT COALESCE(context_id, project_id)) FROM entities WHERE COALESCE(context_id, project_id) IS NOT NULL AND status != 'archived'")
            context_count = cur.fetchone()[0]

            self.send_json({
                "entities": entity_counts,
                "events": event_counts,
                "relations": relation_count,
                "tags": tag_count,
                "total_events": total_events,
                "owners": owner_count,
                "contexts": context_count
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_search(self, query):
        """FTS5-backed full-text search across entities."""
        conn = None
        try:
            q = query.get("q", [None])[0] or query.get("query", [None])[0] or ""
            owner_id = query.get("owner_id", [None])[0]
            status = query.get("status", [None])[0]
            context_id = query.get("context_id", [None])[0]
            limit = 25
            try:
                limit = max(1, min(50, int(query.get("limit", ["25"])[0])))
            except (ValueError, TypeError):
                pass

            if not q:
                self.send_json({"results": [], "query": "", "total": 0})
                return

            conn = self.get_db_connection()

            # Sanitize FTS query
            import re as _re
            safe_q = " ".join(_re.sub(r'[\-+<>:/*\\?^$|#@`~!%&(){}[\]]', ' ', q).split())

            extra_clauses = []
            params = [safe_q]
            if owner_id:
                extra_clauses.append("e.owner_id = ?")
                params.append(owner_id)
            if status:
                extra_clauses.append("e.status = ?")
                params.append(status)
            if context_id:
                extra_clauses.append("(e.context_id = ? OR e.project_id = ?)")
                params.extend([context_id, context_id])

            extra_sql = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
            params.append(limit)

            try:
                cur = conn.execute(f"""
                    SELECT e.id, e.title, e.owner_id, e.status, e.weight, e.updated_at, e.context_id,
                           snippet(entities_fts, 0, '<mark>', '</mark>', '...', 12) as snippet
                    FROM entities_fts
                    JOIN entities e ON entities_fts.rowid = e.rowid
                    WHERE entities_fts MATCH ? AND e.status != 'archived'
                    {extra_sql}
                    ORDER BY rank
                    LIMIT ?
                """, params)
                rows = cur.fetchall()
            except Exception:
                # Fallback to LIKE search if FTS unavailable
                like_q = f"%{q}%"
                params2 = [like_q, like_q]
                if owner_id:
                    params2.append(owner_id)
                if status:
                    params2.append(status)
                cur = conn.execute(f"""
                    SELECT id, title, owner_id, status, weight, updated_at, context_id, '' as snippet
                    FROM entities
                    WHERE (title LIKE ? OR full_content LIKE ?) AND status != 'archived'
                    {extra_sql.replace('e.', '')}
                    LIMIT ?
                """, params2 + [limit])
                rows = cur.fetchall()

            results = [{
                "id": r[0],
                "title": r[1],
                "owner_id": r[2],
                "status": r[3],
                "weight": r[4],
                "updated_at": r[5],
                "context_id": r[6],
                "snippet": r[7]
            } for r in rows]

            self.send_json({"results": results, "query": q, "total": len(results)})
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_lineage(self, entity_id):
        """Return lineage tree for an entity using recursive CTE (consolidated_from edges)."""
        conn = None
        try:
            conn = self.get_db_connection()

            # Check entity exists
            cur = conn.execute("SELECT title, status FROM entities WHERE id = ?", (entity_id,))
            row = cur.fetchone()
            if not row:
                self.send_json({"error": "Entity not found"}, 404)
                return
            root_title, root_status = row

            # Recursive CTE walking consolidated_from edges (source=child, target=parent)
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
                "updated_at": r[5]
            } for r in cur.fetchall()]

            # Fetch the lineage edges
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
                "edges": edges
            })
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def serve_frontend(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SALTMDB Database Viewer</title>
    <meta name="description" content="SALTMDB: Short and Long-Term Memory Database Explorer">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0b0f19;
            --bg-surface: #151c2c;
            --bg-surface-elevated: #1e293b;
            --accent-primary: hsl(220, 85%, 57%);
            --accent-primary-glow: rgba(37, 99, 235, 0.2);
            --accent-success: hsl(142, 70%, 45%);
            --accent-warning: hsl(38, 92%, 50%);
            --accent-error: hsl(0, 84%, 60%);
            --accent-purple: hsl(270, 80%, 68%);
            --accent-cyan: hsl(190, 80%, 55%);
            --text-primary: #f3f4f6;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --border-color: rgba(255, 255, 255, 0.08);
            --transition-smooth: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            line-height: 1.5;
            padding: 2rem;
            min-height: 100vh;
        }
        h1, h2, h3, h4 { font-family: 'Outfit', sans-serif; font-weight: 600; }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
        }
        .brand { display: flex; flex-direction: column; }
        .brand h1 {
            font-size: 2.2rem;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #fff 0%, #94a3b8 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .brand span { font-size: 0.85rem; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 2px; margin-top: 0.2rem; }
        .db-path { font-size: 0.85rem; background-color: rgba(255,255,255,0.04); border: 1px solid var(--border-color); padding: 0.5rem 1rem; border-radius: 99px; color: var(--text-secondary); font-family: monospace; }

        /* Stats Bar */
        .stats-bar {
            display: flex;
            gap: 1rem;
            flex-wrap: wrap;
            margin-bottom: 1.5rem;
            padding: 1rem 1.5rem;
            background: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 12px;
        }
        .stat-item {
            display: flex;
            flex-direction: column;
            gap: 0.15rem;
            flex: 1;
            min-width: 80px;
        }
        .stat-value { font-size: 1.5rem; font-weight: 700; font-family: 'Outfit', sans-serif; }
        .stat-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .stat-raw { color: var(--accent-primary); }
        .stat-consolidated { color: var(--accent-success); }
        .stat-archived { color: var(--text-muted); }
        .stat-events { color: var(--accent-warning); }
        .stat-relations { color: var(--accent-purple); }
        .stat-tags { color: var(--accent-cyan); }

        /* Tabs Navigation */
        .tabs { display: flex; gap: 0.5rem; background-color: rgba(255,255,255,0.02); padding: 0.35rem; border-radius: 12px; border: 1px solid var(--border-color); margin-bottom: 2rem; width: fit-content; flex-wrap: wrap; }
        .tab-btn { background: none; border: none; color: var(--text-secondary); padding: 0.65rem 1.25rem; font-size: 0.9rem; font-weight: 500; border-radius: 8px; cursor: pointer; transition: var(--transition-smooth); font-family: inherit; }
        .tab-btn:hover { color: var(--text-primary); background-color: rgba(255,255,255,0.04); }
        .tab-btn.active { color: #fff; background-color: var(--accent-primary); box-shadow: 0 4px 12px var(--accent-primary-glow); }

        /* Main View Container */
        .view-content { display: none; animation: fadeIn 0.3s ease-in-out forwards; }
        .view-content.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

        /* Controls Row */
        .controls-row { display: flex; flex-wrap: wrap; align-items: center; margin-bottom: 1.5rem; gap: 0.75rem; }
        .search-input {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 0.7rem 1.1rem;
            color: var(--text-primary);
            font-size: 0.95rem;
            min-width: 260px;
            flex: 1;
            max-width: 400px;
            transition: var(--transition-smooth);
            font-family: inherit;
        }
        .search-input:focus { outline: none; border-color: var(--accent-primary); box-shadow: 0 0 0 3px var(--accent-primary-glow); }
        .filter-select {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 0.7rem 1rem;
            color: var(--text-secondary);
            font-size: 0.875rem;
            cursor: pointer;
            transition: var(--transition-smooth);
            font-family: inherit;
        }
        .filter-select:focus { outline: none; border-color: var(--accent-primary); }
        .btn {
            background-color: var(--accent-primary);
            border: none;
            color: #fff;
            padding: 0.7rem 1.25rem;
            border-radius: 10px;
            cursor: pointer;
            font-size: 0.875rem;
            font-weight: 600;
            font-family: inherit;
            transition: var(--transition-smooth);
        }
        .btn:hover { opacity: 0.85; transform: translateY(-1px); }
        .btn-ghost {
            background-color: transparent;
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
        }
        .btn-ghost:hover { border-color: var(--accent-primary); color: var(--text-primary); }

        /* Search Results Panel */
        .search-results-panel { margin-bottom: 1.5rem; }
        .search-result-item {
            background: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 1rem 1.25rem;
            margin-bottom: 0.75rem;
            cursor: pointer;
            transition: var(--transition-smooth);
        }
        .search-result-item:hover { border-color: var(--accent-primary); transform: translateX(2px); }
        .search-result-title { font-weight: 600; color: #fff; margin-bottom: 0.3rem; }
        .search-result-snippet { font-size: 0.875rem; color: var(--text-secondary); line-height: 1.5; }
        .search-result-snippet mark { background: rgba(37, 99, 235, 0.3); color: #93c5fd; border-radius: 2px; padding: 0 2px; }
        .search-result-meta { display: flex; gap: 0.75rem; margin-top: 0.5rem; font-size: 0.8rem; color: var(--text-muted); }

        /* Entities Grid */
        .entities-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 1.5rem; }
        .entity-card {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1.5rem;
            cursor: pointer;
            transition: var(--transition-smooth);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: relative;
            overflow: hidden;
        }
        .entity-card::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 3px; background: transparent; transition: var(--transition-smooth); }
        .entity-card:hover { transform: translateY(-4px); border-color: rgba(255,255,255,0.15); box-shadow: 0 12px 24px rgba(0,0,0,0.4); }
        .entity-card.raw::before { background: var(--accent-primary); }
        .entity-card.consolidated::before { background: var(--accent-success); }
        .entity-card.archived::before { background: var(--text-muted); }
        .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; flex-wrap: wrap; gap: 0.4rem; }
        .badges { display: flex; gap: 0.4rem; flex-wrap: wrap; }
        .status-badge { font-size: 0.72rem; font-weight: 600; text-transform: uppercase; padding: 0.2rem 0.55rem; border-radius: 6px; letter-spacing: 0.5px; }
        .status-badge.raw { background-color: rgba(37,99,235,0.15); color: #3b82f6; }
        .status-badge.consolidated { background-color: rgba(16,185,129,0.15); color: #10b981; }
        .status-badge.archived { background-color: rgba(148,163,184,0.15); color: #94a3b8; }
        .core-badge { font-size: 0.72rem; font-weight: 600; text-transform: uppercase; padding: 0.2rem 0.55rem; border-radius: 6px; background-color: rgba(245,158,11,0.15); color: #f59e0b; }
        .owner-badge { font-size: 0.72rem; font-weight: 500; padding: 0.2rem 0.55rem; border-radius: 6px; background-color: rgba(168,85,247,0.12); color: #c084fc; max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .context-badge { font-size: 0.72rem; font-weight: 500; padding: 0.2rem 0.55rem; border-radius: 6px; background-color: rgba(34,211,238,0.1); color: #22d3ee; max-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .card-title { font-size: 1.1rem; color: #fff; margin-bottom: 0.75rem; line-height: 1.4; }
        .card-meta { font-size: 0.8rem; color: var(--text-secondary); display: flex; flex-direction: column; gap: 0.35rem; margin-bottom: 0.85rem; }
        .meta-item { display: flex; align-items: center; gap: 0.4rem; }
        .meta-label { color: var(--text-muted); min-width: 80px; }
        .card-tags { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: auto; }
        .tag-pill { font-size: 0.72rem; background-color: rgba(255,255,255,0.05); border: 1px solid var(--border-color); padding: 0.18rem 0.45rem; border-radius: 5px; color: var(--text-secondary); }

        /* Events Table */
        .table-container { background-color: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 14px; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.2); }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th { background-color: rgba(255,255,255,0.02); padding: 1rem 1.25rem; font-size: 0.83rem; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border-color); }
        td { padding: 0.85rem 1.25rem; font-size: 0.875rem; border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--text-primary); }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background-color: rgba(255,255,255,0.01); }
        .event-type { font-weight: 600; font-size: 0.78rem; text-transform: uppercase; padding: 0.18rem 0.5rem; border-radius: 4px; width: fit-content; }
        .event-type.issue { background-color: rgba(239,68,68,0.1); color: var(--accent-error); }
        .event-type.attempt { background-color: rgba(245,158,11,0.1); color: var(--accent-warning); }
        .event-type.fix { background-color: rgba(16,185,129,0.1); color: var(--accent-success); }
        .event-type.decision { background-color: rgba(37,99,235,0.1); color: var(--accent-primary); }
        .event-type.consolidation_request { background-color: rgba(168,85,247,0.1); color: var(--accent-purple); }
        .event-type.event { background-color: rgba(148,163,184,0.08); color: var(--text-muted); }
        .code-snippet { font-family: monospace; background-color: rgba(0,0,0,0.2); padding: 0.18rem 0.4rem; border-radius: 4px; border: 1px solid var(--border-color); font-size: 0.8rem; }

        /* Tags View */
        .tags-container { display: flex; flex-wrap: wrap; gap: 1rem; }
        .tag-card { background-color: var(--bg-surface); border: 1px solid var(--border-color); padding: 1rem 1.5rem; border-radius: 12px; display: flex; align-items: center; gap: 1rem; transition: var(--transition-smooth); }
        .tag-card:hover { border-color: rgba(255,255,255,0.15); background-color: var(--bg-surface-elevated); }
        .tag-name { font-weight: 500; color: #fff; }
        .tag-badge-count { background-color: var(--accent-primary); color: #fff; font-size: 0.8rem; font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 99px; }
        .tag-alias { font-size: 0.8rem; color: var(--text-muted); }

        /* Locks */
        .locks-row { display: flex; gap: 1.5rem; flex-wrap: wrap; }
        .lock-status-card { background-color: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 14px; padding: 1.5rem; width: 100%; max-width: 450px; }
        .lock-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem; }
        .lock-indicator { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; font-weight: 600; }
        .indicator-dot { width: 10px; height: 10px; border-radius: 99px; }
        .indicator-dot.active { background-color: var(--accent-warning); box-shadow: 0 0 10px var(--accent-warning); }
        .indicator-dot.inactive { background-color: var(--accent-success); box-shadow: 0 0 10px var(--accent-success); }

        /* Detail Modal */
        .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background-color: rgba(11,15,25,0.85); backdrop-filter: blur(8px); display: none; justify-content: center; align-items: center; z-index: 1000; padding: 2rem; }
        .modal-card { background-color: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 16px; width: 100%; max-width: 860px; max-height: 88vh; display: flex; flex-direction: column; box-shadow: 0 24px 48px rgba(0,0,0,0.5); animation: modalSlide 0.3s cubic-bezier(0.4,0,0.2,1) forwards; }
        @keyframes modalSlide { from { transform: scale(0.95); opacity: 0; } to { transform: scale(1); opacity: 1; } }
        .modal-header { padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
        .modal-header-left { display: flex; flex-direction: column; gap: 0.4rem; min-width: 0; }
        .modal-title-text { font-size: 1.1rem; color: #fff; font-family: 'Outfit', sans-serif; font-weight: 600; }
        .modal-header-badges { display: flex; flex-wrap: wrap; gap: 0.35rem; }
        .modal-actions { display: flex; align-items: center; gap: 0.75rem; }
        .modal-close { background: none; border: none; color: var(--text-secondary); font-size: 1.4rem; cursor: pointer; transition: var(--transition-smooth); padding: 0.25rem; }
        .modal-close:hover { color: #fff; }
        .modal-body { padding: 1.5rem 2rem; overflow-y: auto; flex-grow: 1; }
        .modal-meta-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.75rem; margin-bottom: 1.5rem; }
        .modal-meta-item { background: rgba(255,255,255,0.03); border: 1px solid var(--border-color); border-radius: 8px; padding: 0.6rem 0.85rem; }
        .modal-meta-label { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.2rem; }
        .modal-meta-value { font-size: 0.875rem; color: var(--text-primary); word-break: break-all; font-family: monospace; }
        .modal-section-title { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.75rem; }
        .copy-btn { background: rgba(255,255,255,0.06); border: 1px solid var(--border-color); color: var(--text-secondary); padding: 0.3rem 0.7rem; border-radius: 6px; cursor: pointer; font-size: 0.78rem; font-family: inherit; transition: var(--transition-smooth); }
        .copy-btn:hover { background: rgba(255,255,255,0.1); color: var(--text-primary); }
        .markdown-render { font-family: inherit; color: var(--text-primary); }
        .markdown-render pre { background-color: rgba(0,0,0,0.3); border: 1px solid var(--border-color); padding: 1rem; border-radius: 8px; overflow-x: auto; font-family: monospace; margin: 1rem 0; font-size: 0.875rem; }
        .markdown-render h1, .markdown-render h2, .markdown-render h3 { margin-top: 1.5rem; margin-bottom: 0.75rem; color: #fff; }
        .markdown-render p { margin-bottom: 1rem; color: var(--text-secondary); line-height: 1.6; }
        .markdown-render ul, .markdown-render ol { margin-left: 1.5rem; margin-bottom: 1rem; color: var(--text-secondary); }
        .markdown-render li { margin-bottom: 0.4rem; }
        .markdown-render blockquote { border-left: 4px solid var(--accent-primary); padding-left: 1rem; margin: 1rem 0; color: var(--text-muted); font-style: italic; }
        .markdown-render code { font-family: monospace; background: rgba(0,0,0,0.2); padding: 0.1rem 0.35rem; border-radius: 3px; font-size: 0.875em; }
        .section-divider { border: none; border-top: 1px solid var(--border-color); margin: 1.5rem 0; }

        /* Pagination */
        .pagination-controls { display: flex; justify-content: center; align-items: center; gap: 1.5rem; margin-top: 2rem; padding: 1rem 0; }
        .pagination-btn { background-color: var(--bg-surface); border: 1px solid var(--border-color); color: var(--text-primary); padding: 0.5rem 1.25rem; border-radius: 8px; cursor: pointer; font-family: inherit; font-size: 0.875rem; font-weight: 600; transition: var(--transition-smooth); }
        .pagination-btn:hover:not(:disabled) { background-color: var(--bg-surface-elevated); border-color: var(--accent-primary); box-shadow: 0 0 10px var(--accent-primary-glow); }
        .pagination-btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .pagination-info { color: var(--text-secondary); font-size: 0.875rem; font-weight: 500; }

        /* Graph */
        .graph-container { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; position: relative; overflow: hidden; height: 600px; }
        .graph-toolbar { position: absolute; top: 1rem; left: 1rem; z-index: 10; display: flex; gap: 0.5rem; }
        .graph-btn { background: rgba(15,23,42,0.9); border: 1px solid #334155; color: #94a3b8; padding: 0.4rem 0.7rem; border-radius: 6px; font-size: 0.8rem; cursor: pointer; font-family: inherit; transition: var(--transition-smooth); }
        .graph-btn:hover { border-color: #3b82f6; color: #f3f4f6; }
        .graph-legend { position: absolute; bottom: 1rem; right: 1rem; background: rgba(15,23,42,0.9); border: 1px solid #1e293b; padding: 0.75rem 1rem; border-radius: 8px; font-size: 0.78rem; }
        .legend-item { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.3rem; color: #94a3b8; }
        .legend-dot { width: 10px; height: 10px; border-radius: 99px; flex-shrink: 0; }
        .legend-line { width: 24px; height: 3px; border-radius: 2px; flex-shrink: 0; }

        /* Lineage Tab */
        .lineage-layout { display: grid; grid-template-columns: 320px 1fr; gap: 1.5rem; }
        .lineage-panel { background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 12px; padding: 1.5rem; }
        .lineage-tree-item { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.75rem; border-radius: 8px; cursor: pointer; transition: var(--transition-smooth); margin-bottom: 0.4rem; }
        .lineage-tree-item:hover { background: rgba(255,255,255,0.03); }
        .lineage-depth-indicator { display: flex; align-items: center; gap: 0.25rem; color: var(--text-muted); font-size: 0.8rem; min-width: 24px; }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <h1>SALTMDB</h1>
            <span>Short and Long-Term Memory Explorer</span>
        </div>
        <div class="db-path">""" + DB_PATH + """</div>
    </header>

    <!-- Stats Bar -->
    <div class="stats-bar" id="stats-bar">
        <div class="stat-item"><span class="stat-value stat-raw" id="stat-raw">—</span><span class="stat-label">Raw</span></div>
        <div class="stat-item"><span class="stat-value stat-consolidated" id="stat-consolidated">—</span><span class="stat-label">Consolidated</span></div>
        <div class="stat-item"><span class="stat-value stat-archived" id="stat-archived">—</span><span class="stat-label">Archived</span></div>
        <div class="stat-item"><span class="stat-value stat-events" id="stat-events">—</span><span class="stat-label">Events</span></div>
        <div class="stat-item"><span class="stat-value stat-relations" id="stat-relations">—</span><span class="stat-label">Relations</span></div>
        <div class="stat-item"><span class="stat-value stat-tags" id="stat-tags">—</span><span class="stat-label">Tags</span></div>
        <div class="stat-item"><span class="stat-value" style="color:#f1f5f9" id="stat-owners">—</span><span class="stat-label">Owners</span></div>
        <div class="stat-item"><span class="stat-value" style="color:#f1f5f9" id="stat-contexts">—</span><span class="stat-label">Contexts</span></div>
    </div>

    <nav class="tabs" id="main-tabs">
        <button class="tab-btn active" data-tab="entities" onclick="switchTab('entities')">Entities (Long-Term)</button>
        <button class="tab-btn" data-tab="events" onclick="switchTab('events')">Events (Short-Term)</button>
        <button class="tab-btn" data-tab="tags" onclick="switchTab('tags')">Tags folksonomy</button>
        <button class="tab-btn" data-tab="relations" onclick="switchTab('relations')">Relations Topology</button>
        <button class="tab-btn" data-tab="lineage" onclick="switchTab('lineage')">Lineage</button>
        <button class="tab-btn" data-tab="locks" onclick="switchTab('locks')">System Locks</button>
    </nav>

    <!-- Entities Tab -->
    <div id="tab-entities" class="view-content active">
        <!-- FTS Search -->
        <div class="controls-row">
            <input type="text" class="search-input" id="entity-search" placeholder="Full-text search memories..." oninput="debounceFtsSearch()">
            <select class="filter-select" id="entity-status-filter" onchange="applyEntityFilters()">
                <option value="">All Statuses</option>
                <option value="raw">Raw</option>
                <option value="consolidated">Consolidated</option>
                <option value="archived">Archived</option>
            </select>
            <select class="filter-select" id="entity-owner-filter" onchange="applyEntityFilters()">
                <option value="">All Owners</option>
            </select>
            <select class="filter-select" id="entity-core-filter" onchange="applyEntityFilters()">
                <option value="">All Memories</option>
                <option value="true">Core Only</option>
            </select>
            <button class="btn btn-ghost" onclick="clearEntityFilters()">Clear</button>
        </div>
        <!-- FTS Results (shown when search active) -->
        <div id="fts-results-panel" class="search-results-panel" style="display:none;">
            <p style="color:var(--text-muted); font-size:0.85rem; margin-bottom:0.75rem;" id="fts-results-label"></p>
            <div id="fts-results-list"></div>
        </div>
        <!-- Regular Browsing Grid -->
        <div id="entity-browse-panel">
            <div class="entities-grid" id="entities-list"></div>
            <div class="pagination-controls" id="entities-pagination"></div>
        </div>
    </div>

    <!-- Events Tab -->
    <div id="tab-events" class="view-content">
        <div class="controls-row">
            <input type="text" class="search-input" id="event-search" placeholder="Filter by content or agent..." oninput="filterEventsLocal()">
            <select class="filter-select" id="event-type-filter" onchange="applyEventFilters()">
                <option value="">All Types</option>
                <option value="issue">Issue</option>
                <option value="attempt">Attempt</option>
                <option value="fix">Fix</option>
                <option value="decision">Decision</option>
                <option value="consolidation_request">Consolidation Request</option>
                <option value="event">Event</option>
            </select>
            <button class="btn btn-ghost" onclick="clearEventFilters()">Clear</button>
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width:165px;">Timestamp</th>
                        <th style="width:115px;">Agent</th>
                        <th style="width:90px;">Type</th>
                        <th>Content</th>
                        <th style="width:90px;">Error</th>
                        <th style="width:110px;">Session</th>
                    </tr>
                </thead>
                <tbody id="events-list"></tbody>
            </table>
        </div>
        <div class="pagination-controls" id="events-pagination"></div>
    </div>

    <!-- Tags Tab -->
    <div id="tab-tags" class="view-content">
        <div class="controls-row">
            <input type="text" class="search-input" id="tag-search" placeholder="Filter tags..." oninput="filterTagsLocal()">
        </div>
        <div class="tags-container" id="tags-list"></div>
    </div>

    <!-- Relations Tab -->
    <div id="tab-relations" class="view-content">
        <div style="display:grid; grid-template-columns: 1fr 310px; gap: 2rem; margin-top: 1rem;">
            <div class="graph-container">
                <div class="graph-toolbar">
                    <button class="graph-btn" onclick="resetGraphView()">Reset View</button>
                    <select class="filter-select" id="predicate-filter" onchange="loadRelationsTab()" style="background:rgba(15,23,42,0.9); font-size:0.8rem; padding: 0.4rem 0.7rem; border-radius:6px; border:1px solid #334155; color:#94a3b8;">
                        <option value="">All predicates</option>
                    </select>
                </div>
                <svg id="relations-svg" width="100%" height="100%" style="cursor:grab;"></svg>
                <div class="graph-legend" id="graph-legend"></div>
            </div>
            <div style="background:#0f172a; border:1px solid #1e293b; border-radius:12px; padding:1.5rem; overflow-y:auto; height:600px; display:flex; flex-direction:column;">
                <h3 style="color:#f3f4f6; margin-bottom:1rem; font-family:'Outfit',sans-serif; font-size:1rem;">All Relationships</h3>
                <div id="relations-sidebar-list" style="display:flex; flex-direction:column; gap:0.75rem; overflow-y:auto; flex:1;"></div>
                <div class="pagination-controls" id="relations-pagination" style="margin-top:1rem; padding:0.5rem 0;"></div>
            </div>
        </div>
    </div>

    <!-- Lineage Tab -->
    <div id="tab-lineage" class="view-content">
        <div class="controls-row" style="margin-bottom:1rem;">
            <input type="text" class="search-input" id="lineage-search" placeholder="Entity ID or title to inspect lineage..." style="max-width:500px;">
            <button class="btn" onclick="loadLineage()">Show Lineage</button>
        </div>
        <div class="lineage-layout" id="lineage-layout" style="display:none;">
            <div class="lineage-panel">
                <h3 style="font-size:0.9rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:1rem;">Ancestor Chain</h3>
                <div id="lineage-nodes-list"></div>
            </div>
            <div class="graph-container" style="height:500px;">
                <svg id="lineage-svg" width="100%" height="100%"></svg>
            </div>
        </div>
        <p id="lineage-empty" style="color:var(--text-muted); margin-top:2rem; text-align:center;">Enter an entity ID or title above to explore its consolidation lineage.</p>
    </div>

    <!-- Locks Tab -->
    <div id="tab-locks" class="view-content">
        <div class="locks-row" id="locks-list"></div>
        <p style="margin-top:2.5rem; color:var(--text-muted); font-size:0.875rem; text-align:center; font-style:italic;">
            Note: Ephemeral memories are stored in RAM by the active MCP process and are not visible in this dashboard.
        </p>
    </div>

    <!-- Entity Detail Modal -->
    <div class="modal-overlay" id="detail-modal" onclick="closeModal(event)">
        <div class="modal-card" onclick="event.stopPropagation()">
            <div class="modal-header">
                <div class="modal-header-left">
                    <div class="modal-title-text" id="modal-title">Memory Detail</div>
                    <div class="modal-header-badges" id="modal-badges"></div>
                </div>
                <div class="modal-actions">
                    <button class="copy-btn" id="modal-copy-btn" onclick="copyEntityId()">Copy ID</button>
                    <button class="modal-close" onclick="closeModal()">&times;</button>
                </div>
            </div>
            <div class="modal-body" id="modal-body">
                <div class="modal-meta-grid" id="modal-meta-grid"></div>
                <hr class="section-divider">
                <div class="modal-section-title">Content</div>
                <div class="markdown-render" id="modal-content"></div>
                <div id="modal-relations-section"></div>
            </div>
        </div>
    </div>

    <script>
        let allEntities = [];
        let allEvents = [];
        let allTags = [];
        let currentEntitiesPage = 1;
        let currentEventsPage = 1;
        let currentRelationsPage = 1;
        let activeEntityId = null;
        let entityFilters = {};
        let eventFilters = {};
        let ftsDebounceTimer = null;
        let graphViewTransform = { x: 0, y: 0, scale: 1 };
        let graphIsDragging = false;
        let graphDragStart = { x: 0, y: 0 };
        let graphDraggedNode = null;
        let graphNodes = [];
        let graphLinks = [];
        let graphNodeMap = {};
        let knownOwners = new Set();
        let knownPredicates = new Set();

        // ============================================================
        // Tab Switching — use data-tab attributes (bug fix)
        // ============================================================
        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.tab === tabId);
            });
            document.querySelectorAll('.view-content').forEach(view => view.classList.remove('active'));
            document.getElementById('tab-' + tabId).classList.add('active');
            loadTabData(tabId);
        }

        // ============================================================
        // Stats Bar
        // ============================================================
        async function loadStats() {
            try {
                const res = await fetch('/api/stats');
                const d = await res.json();
                if (d.error) return;
                document.getElementById('stat-raw').textContent = d.entities.raw || 0;
                document.getElementById('stat-consolidated').textContent = d.entities.consolidated || 0;
                document.getElementById('stat-archived').textContent = d.entities.archived || 0;
                document.getElementById('stat-events').textContent = d.total_events || 0;
                document.getElementById('stat-relations').textContent = d.relations || 0;
                document.getElementById('stat-tags').textContent = d.tags || 0;
                document.getElementById('stat-owners').textContent = d.owners || 0;
                document.getElementById('stat-contexts').textContent = d.contexts || 0;
            } catch (e) {}
        }

        // ============================================================
        // Tab Data Loading
        // ============================================================
        async function loadTabData(tabId) {
            try {
                if (tabId === 'entities') {
                    await fetchEntities();
                } else if (tabId === 'events') {
                    await fetchEvents();
                } else if (tabId === 'tags') {
                    const res = await fetch('/api/tags');
                    const tags = await res.json();
                    if (tags.error) { renderError('tags', tags.error); return; }
                    allTags = tags;
                    renderTags(tags);
                } else if (tabId === 'relations') {
                    await loadRelationsTab();
                } else if (tabId === 'lineage') {
                    // Nothing to auto-load, user-driven
                } else if (tabId === 'locks') {
                    const res = await fetch('/api/locks');
                    const locks = await res.json();
                    if (locks.error) { renderError('locks', locks.error); return; }
                    renderLocks(locks);
                }
            } catch (err) {
                renderError(tabId, err.message || String(err));
            }
        }

        // ============================================================
        // Entities Browsing (server-side filtered)
        // ============================================================
        async function fetchEntities() {
            const params = new URLSearchParams({ page: currentEntitiesPage });
            if (entityFilters.status) params.set('status', entityFilters.status);
            if (entityFilters.owner_id) params.set('owner_id', entityFilters.owner_id);
            if (entityFilters.is_core) params.set('is_core', entityFilters.is_core);

            const res = await fetch('/api/entities?' + params);
            const data = await res.json();
            if (data.error) { renderError('entities', data.error); return; }

            allEntities = data.entities;

            // Populate owner filter dropdown from discovered owners
            data.entities.forEach(e => { if (e.owner_id) knownOwners.add(e.owner_id); });
            populateOwnerDropdown();

            renderEntities(allEntities);
            renderPagination('entities', data.pagination);
        }

        function applyEntityFilters() {
            entityFilters.status = document.getElementById('entity-status-filter').value || null;
            entityFilters.owner_id = document.getElementById('entity-owner-filter').value || null;
            entityFilters.is_core = document.getElementById('entity-core-filter').value || null;
            currentEntitiesPage = 1;
            // Hide FTS panel when using browse mode
            document.getElementById('fts-results-panel').style.display = 'none';
            document.getElementById('entity-browse-panel').style.display = 'block';
            fetchEntities();
        }

        function clearEntityFilters() {
            document.getElementById('entity-search').value = '';
            document.getElementById('entity-status-filter').value = '';
            document.getElementById('entity-owner-filter').value = '';
            document.getElementById('entity-core-filter').value = '';
            entityFilters = {};
            currentEntitiesPage = 1;
            document.getElementById('fts-results-panel').style.display = 'none';
            document.getElementById('entity-browse-panel').style.display = 'block';
            fetchEntities();
        }

        function populateOwnerDropdown() {
            const sel = document.getElementById('entity-owner-filter');
            const current = sel.value;
            sel.innerHTML = '<option value="">All Owners</option>';
            [...knownOwners].sort().forEach(o => {
                const opt = document.createElement('option');
                opt.value = o;
                opt.textContent = o;
                if (o === current) opt.selected = true;
                sel.appendChild(opt);
            });
        }

        // ============================================================
        // FTS Search (backend)
        // ============================================================
        function debounceFtsSearch() {
            clearTimeout(ftsDebounceTimer);
            const q = document.getElementById('entity-search').value.trim();
            if (!q) {
                document.getElementById('fts-results-panel').style.display = 'none';
                document.getElementById('entity-browse-panel').style.display = 'block';
                return;
            }
            ftsDebounceTimer = setTimeout(() => runFtsSearch(q), 400);
        }

        async function runFtsSearch(q) {
            try {
                const params = new URLSearchParams({ q });
                if (entityFilters.status) params.set('status', entityFilters.status);
                if (entityFilters.owner_id) params.set('owner_id', entityFilters.owner_id);
                const res = await fetch('/api/search?' + params);
                const data = await res.json();

                document.getElementById('fts-results-panel').style.display = 'block';
                document.getElementById('entity-browse-panel').style.display = 'none';

                const label = document.getElementById('fts-results-label');
                const list = document.getElementById('fts-results-list');
                list.innerHTML = '';

                if (data.error) {
                    label.textContent = 'Search error: ' + data.error;
                    return;
                }

                label.textContent = data.total + ' result' + (data.total !== 1 ? 's' : '') + ' for "' + escapeHtml(q) + '"';

                if (data.results.length === 0) {
                    list.innerHTML = '<p style="color:var(--text-muted); padding:2rem; text-align:center;">No results found.</p>';
                    return;
                }

                data.results.forEach(r => {
                    const item = document.createElement('div');
                    item.className = 'search-result-item';
                    item.onclick = () => showEntityDetail(r.id, r.title);
                    item.innerHTML = `
                        <div class="search-result-title">${escapeHtml(r.title)}</div>
                        <div class="search-result-snippet">${r.snippet || ''}</div>
                        <div class="search-result-meta">
                            <span>${escapeHtml(r.owner_id || 'unknown')}</span>
                            <span class="status-badge ${r.status}">${r.status}</span>
                            ${r.context_id ? '<span style="color:var(--accent-cyan);">ctx: ' + escapeHtml(r.context_id) + '</span>' : ''}
                        </div>
                    `;
                    list.appendChild(item);
                });
            } catch (e) {
                document.getElementById('fts-results-panel').style.display = 'block';
                document.getElementById('fts-results-label').textContent = 'Search failed: ' + e.message;
            }
        }

        // ============================================================
        // Entity Cards Rendering
        // ============================================================
        function renderEntities(entities) {
            const list = document.getElementById('entities-list');
            list.innerHTML = '';

            if (entities.length === 0) {
                list.innerHTML = '<p style="grid-column:1/-1; color:var(--text-muted); text-align:center; padding:2rem;">No long-term memories found.</p>';
                return;
            }

            entities.forEach(e => {
                const card = document.createElement('div');
                card.className = 'entity-card ' + e.status;
                card.onclick = () => showEntityDetail(e.id, e.title);

                const tagsHtml = (e.tags || []).map(t => '<span class="tag-pill">' + escapeHtml(t) + '</span>').join('');
                const ownerBadge = e.owner_id ? '<span class="owner-badge" title="' + escapeHtml(e.owner_id) + '">' + escapeHtml(e.owner_id) + '</span>' : '';
                const ctxBadge = e.context_id ? '<span class="context-badge" title="' + escapeHtml(e.context_id) + '">' + escapeHtml(e.context_id) + '</span>' : '';
                const coreBadge = e.is_core ? '<span class="core-badge">core</span>' : '';

                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <div class="badges">
                                <span class="status-badge ${e.status}">${e.status}</span>
                                ${coreBadge}
                            </div>
                            <div class="badges">
                                ${ownerBadge}${ctxBadge}
                            </div>
                        </div>
                        <h3 class="card-title">${escapeHtml(e.title)}</h3>
                        <div class="card-meta">
                            <div class="meta-item"><span class="meta-label">ID:</span><span style="font-family:monospace; font-size:0.78rem;">${e.id.substring(0,12)}…</span></div>
                            <div class="meta-item"><span class="meta-label">Weight:</span><span>${e.weight}</span></div>
                            <div class="meta-item"><span class="meta-label">Updated:</span><span>${formatDate(e.updated_at)}</span></div>
                        </div>
                    </div>
                    <div class="card-tags">${tagsHtml}</div>
                `;
                list.appendChild(card);
            });
        }

        // ============================================================
        // Events Rendering
        // ============================================================
        async function fetchEvents() {
            const params = new URLSearchParams({ page: currentEventsPage });
            if (eventFilters.type) params.set('type', eventFilters.type);

            const res = await fetch('/api/events?' + params);
            const data = await res.json();
            if (data.error) { renderError('events', data.error); return; }

            allEvents = data.events;
            renderEvents(allEvents);
            renderPagination('events', data.pagination);
        }

        function applyEventFilters() {
            eventFilters.type = document.getElementById('event-type-filter').value || null;
            currentEventsPage = 1;
            fetchEvents();
        }

        function clearEventFilters() {
            document.getElementById('event-search').value = '';
            document.getElementById('event-type-filter').value = '';
            eventFilters = {};
            currentEventsPage = 1;
            fetchEvents();
        }

        function filterEventsLocal() {
            const query = document.getElementById('event-search').value.toLowerCase();
            const filtered = allEvents.filter(ev =>
                ev.content.toLowerCase().includes(query) ||
                (ev.agent_id || '').toLowerCase().includes(query) ||
                (ev.type || '').toLowerCase().includes(query)
            );
            renderEvents(filtered);
        }

        function renderEvents(events) {
            const list = document.getElementById('events-list');
            list.innerHTML = '';

            if (events.length === 0) {
                list.innerHTML = '<tr><td colspan="6" style="color:var(--text-muted); text-align:center; padding:2rem;">No events found.</td></tr>';
                return;
            }

            events.forEach(ev => {
                const row = document.createElement('tr');
                const typeClass = (ev.type || '').toLowerCase().replace(/_/g, '_');
                const sessionDisplay = ev.session_id ? '<span title="' + escapeHtml(ev.session_id) + '" style="font-family:monospace; font-size:0.75rem;">' + ev.session_id.substring(0, 8) + '…</span>' : '<span style="color:var(--text-muted);">—</span>';
                row.innerHTML = `
                    <td style="color:var(--text-secondary); font-size:0.82rem;">${formatDate(ev.timestamp)}</td>
                    <td style="font-weight:500; font-size:0.85rem;">${escapeHtml(ev.agent_id || '')}</td>
                    <td><span class="event-type ${typeClass}">${escapeHtml(ev.type || '')}</span></td>
                    <td style="word-break:break-word; max-width:350px;">${escapeHtml(ev.content || '').substring(0, 200)}${(ev.content || '').length > 200 ? '…' : ''}</td>
                    <td>${ev.error_code ? '<span class="code-snippet">' + escapeHtml(ev.error_code) + '</span>' : '<span style="color:var(--text-muted);">—</span>'}</td>
                    <td>${sessionDisplay}</td>
                `;
                list.appendChild(row);
            });
        }

        // ============================================================
        // Tags Rendering
        // ============================================================
        function renderTags(tags) {
            const list = document.getElementById('tags-list');
            list.innerHTML = '';

            if (tags.length === 0) {
                list.innerHTML = '<p style="color:var(--text-muted); padding:2rem;">No folksonomy tags found.</p>';
                return;
            }

            tags.forEach(t => {
                const card = document.createElement('div');
                card.className = 'tag-card';
                card.innerHTML = `
                    <div>
                        <div class="tag-name">${escapeHtml(t.name)}</div>
                        ${t.canonical_id ? '<div class="tag-alias">alias of: <strong>' + escapeHtml(t.canonical_name) + '</strong></div>' : ''}
                    </div>
                    <span class="tag-badge-count">${t.count}</span>
                `;
                list.appendChild(card);
            });
        }

        function filterTagsLocal() {
            const q = document.getElementById('tag-search').value.toLowerCase();
            const filtered = allTags.filter(t => t.name.toLowerCase().includes(q));
            renderTags(filtered);
        }

        // ============================================================
        // Locks Rendering
        // ============================================================
        function renderLocks(locks) {
            const list = document.getElementById('locks-list');
            list.innerHTML = '';

            locks.forEach(l => {
                const active = l.locked_at !== null;
                const card = document.createElement('div');
                card.className = 'lock-status-card';
                card.innerHTML = `
                    <div class="lock-header">
                        <h3 style="color:#fff;">${escapeHtml(l.task_name)}</h3>
                        <div class="lock-indicator">
                            <span class="indicator-dot ${active ? 'active' : 'inactive'}"></span>
                            <span>${active ? 'Locked' : 'Unlocked'}</span>
                        </div>
                    </div>
                    <div class="card-meta">
                        <div class="meta-item"><span class="meta-label">Locked At:</span><span>${l.locked_at ? formatDate(l.locked_at) : 'N/A'}</span></div>
                        <div class="meta-item"><span class="meta-label">PID:</span><span>${l.locked_by_pid || 'N/A'}</span></div>
                        <div class="meta-item"><span class="meta-label">Last Run:</span><span>${l.last_run_at ? formatDate(l.last_run_at) : 'Never'}</span></div>
                    </div>
                `;
                list.appendChild(card);
            });
        }

        // ============================================================
        // Entity Detail Modal (full fields)
        // ============================================================
        async function showEntityDetail(id, title) {
            activeEntityId = id;
            document.getElementById('modal-title').textContent = title || 'Memory Detail';
            document.getElementById('modal-copy-btn').textContent = 'Copy ID';
            document.getElementById('detail-modal').style.display = 'flex';
            document.getElementById('modal-body').innerHTML = '<p style="color:var(--text-muted); text-align:center; padding:2rem;">Loading…</p>';

            try {
                const res = await fetch('/api/entity/' + id);
                const data = await res.json();
                if (data.error) {
                    document.getElementById('modal-body').innerHTML = '<p style="color:var(--accent-error);">' + escapeHtml(data.error) + '</p>';
                    return;
                }

                // Badges
                const badgesHtml = [
                    '<span class="status-badge ' + data.status + '">' + data.status + '</span>',
                    data.is_core ? '<span class="core-badge">core</span>' : '',
                    data.owner_id ? '<span class="owner-badge">' + escapeHtml(data.owner_id) + '</span>' : '',
                    data.context_id ? '<span class="context-badge">' + escapeHtml(data.context_id) + '</span>' : ''
                ].join('');
                document.getElementById('modal-badges').innerHTML = badgesHtml;

                // Meta grid
                const metaFields = [
                    ['ID', id],
                    ['Weight', data.weight],
                    ['Scope', data.scope],
                    ['Owner', data.owner_id || '—'],
                    ['Context', data.context_id || data.project_id || '—'],
                    ['Created', formatDate(data.created_at)],
                    ['Updated', formatDate(data.updated_at)],
                    ['Last Accessed', formatDate(data.last_accessed_at)],
                    ['Valid From', formatDate(data.valid_from)],
                    ['Valid To', data.valid_to ? formatDate(data.valid_to) : '—'],
                ];
                const metaHtml = metaFields.map(([k, v]) => `
                    <div class="modal-meta-item">
                        <div class="modal-meta-label">${k}</div>
                        <div class="modal-meta-value">${escapeHtml(String(v))}</div>
                    </div>
                `).join('');

                // Tags
                const tagsHtml = data.tags && data.tags.length > 0
                    ? '<div style="display:flex; flex-wrap:wrap; gap:0.4rem; margin-bottom:1.5rem;">' + data.tags.map(t => '<span class="tag-pill">' + escapeHtml(t) + '</span>').join('') + '</div>'
                    : '';

                // Relations
                let relHtml = '';
                if (data.relations && (data.relations.outgoing.length > 0 || data.relations.incoming.length > 0)) {
                    relHtml += '<hr class="section-divider"><div class="modal-section-title">Connected Relationships</div>';
                    if (data.relations.outgoing.length > 0) {
                        relHtml += '<div style="margin-bottom:1rem;"><h4 style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; margin-bottom:0.5rem;">Outgoing (This node is Subject)</h4>';
                        data.relations.outgoing.forEach(r => {
                            const tgt_id = escapeAttr(r.target_id);
                            const tgt_title = escapeHtml(r.target_title);
                            relHtml += `<div style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span>this &#x2192;<strong style="color:#a855f7;"> ${escapeHtml(r.predicate)} </strong>&#x2192; <a href="#" onclick="closeModal(); setTimeout(()=>showEntityDetail('${tgt_id}','${tgt_title}'),50); return false;" style="color:#3b82f6; text-decoration:none;">${tgt_title}</a></span>
                            </div>`;
                        });
                        relHtml += '</div>';
                    }
                    if (data.relations.incoming.length > 0) {
                        relHtml += '<div><h4 style="color:#94a3b8; font-size:0.8rem; text-transform:uppercase; margin-bottom:0.5rem;">Incoming (This node is Object)</h4>';
                        data.relations.incoming.forEach(r => {
                            const src_id = escapeAttr(r.source_id);
                            const src_title = escapeHtml(r.source_title);
                            relHtml += `<div style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span><a href="#" onclick="closeModal(); setTimeout(()=>showEntityDetail('${src_id}','${src_title}'),50); return false;" style="color:#3b82f6; text-decoration:none;">${src_title}</a> &#x2192;<strong style="color:#a855f7;"> ${escapeHtml(r.predicate)} </strong>&#x2192; this</span>
                            </div>`;
                        });
                        relHtml += '</div>';
                    }
                }

                // Metadata
                let metadataHtml = '';
                if (data.metadata) {
                    metadataHtml = '<hr class="section-divider"><div class="modal-section-title">Metadata</div><pre style="background:rgba(0,0,0,0.3); border:1px solid var(--border-color); padding:1rem; border-radius:8px; overflow-x:auto; font-size:0.8rem;">' + escapeHtml(JSON.stringify(data.metadata, null, 2)) + '</pre>';
                }

                document.getElementById('modal-body').innerHTML = `
                    <div class="modal-meta-grid">${metaHtml}</div>
                    ${tagsHtml}
                    <hr class="section-divider">
                    <div class="modal-section-title">Content</div>
                    <div class="markdown-render">${parseMarkdown(data.full_content)}</div>
                    ${relHtml}
                    ${metadataHtml}
                `;

            } catch (err) {
                document.getElementById('modal-body').innerHTML = '<p style="color:var(--accent-error);">Failed to load: ' + escapeHtml(err.message) + '</p>';
            }
        }

        function copyEntityId() {
            if (activeEntityId) {
                navigator.clipboard.writeText(activeEntityId).then(() => {
                    document.getElementById('modal-copy-btn').textContent = 'Copied!';
                    setTimeout(() => document.getElementById('modal-copy-btn').textContent = 'Copy ID', 1500);
                });
            }
        }

        function closeModal(event) {
            if (!event || event.target === document.getElementById('detail-modal')) {
                document.getElementById('detail-modal').style.display = 'none';
            }
        }

        // ============================================================
        // Relations Graph (with zoom/pan, predicate color legend)
        // ============================================================
        const PREDICATE_COLORS = {
            'depends_on': '#f87171',
            'part_of': '#60a5fa',
            'resolved_by': '#34d399',
            'links_to': '#94a3b8',
            'duplicate_of': '#fbbf24',
            'consolidated_from': '#c084fc',
            'consolidated_into': '#a78bfa',
        };

        function getPredicateColor(predicate) {
            return PREDICATE_COLORS[predicate] || '#64748b';
        }

        async function loadRelationsTab() {
            try {
                const predicateFilter = document.getElementById('predicate-filter').value;
                const params = new URLSearchParams({ page: currentRelationsPage });
                if (predicateFilter) params.set('predicate', predicateFilter);

                const res = await fetch('/api/relations?' + params);
                const data = await res.json();

                const sidebar = document.getElementById('relations-sidebar-list');
                sidebar.innerHTML = '';

                if (data.error) {
                    sidebar.innerHTML = '<p style="color:var(--accent-error);">' + escapeHtml(data.error) + '</p>';
                    return;
                }

                const relations = data.relations || [];

                // Collect predicates for filter dropdown
                relations.forEach(r => knownPredicates.add(r.predicate));
                populatePredicateDropdown();

                // Build legend
                buildGraphLegend();

                if (relations.length === 0) {
                    sidebar.innerHTML = '<p style="color:var(--text-muted); text-align:center; padding:2rem;">No relationships found.</p>';
                    document.getElementById('relations-svg').innerHTML = '';
                    return;
                }

                // Sidebar list
                relations.forEach(r => {
                    const item = document.createElement('div');
                    item.style.cssText = 'background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.65rem; font-size:0.85rem;';
                    const srcSafe = escapeAttr(r.source_id);
                    const tgtSafe = escapeAttr(r.target_id);
                    item.innerHTML = `
                        <div style="display:flex; justify-content:space-between; margin-bottom:0.25rem; align-items:center;">
                            <a href="#" onclick="showEntityDetail('${srcSafe}','${escapeHtml(r.source_title)}');return false;" style="color:#3b82f6; text-decoration:none; font-weight:500; font-size:0.82rem;">${escapeHtml(r.source_title)}</a>
                            <span style="color:${getPredicateColor(r.predicate)}; font-size:0.75rem; font-weight:600;">${escapeHtml(r.predicate)}</span>
                        </div>
                        <div style="text-align:right;">
                            <a href="#" onclick="showEntityDetail('${tgtSafe}','${escapeHtml(r.target_title)}');return false;" style="color:#3b82f6; text-decoration:none; font-weight:500; font-size:0.82rem;">${escapeHtml(r.target_title)}</a>
                        </div>
                    `;
                    sidebar.appendChild(item);
                });

                renderPagination('relations', data.pagination);

                // Build graph
                graphNodeMap = {};
                graphNodes = [];
                graphLinks = [];

                relations.forEach(r => {
                    if (!graphNodeMap[r.source_id]) {
                        graphNodeMap[r.source_id] = { id: r.source_id, title: r.source_title };
                        graphNodes.push(graphNodeMap[r.source_id]);
                    }
                    if (!graphNodeMap[r.target_id]) {
                        graphNodeMap[r.target_id] = { id: r.target_id, title: r.target_title };
                        graphNodes.push(graphNodeMap[r.target_id]);
                    }
                    graphLinks.push({ source_id: r.source_id, target_id: r.target_id, predicate: r.predicate });
                });

                const svg = document.getElementById('relations-svg');
                const W = svg.clientWidth || 800;
                const H = svg.clientHeight || 600;

                graphNodes.forEach((n, idx) => {
                    const angle = (idx / graphNodes.length) * 2 * Math.PI;
                    n.x = W / 2 + Math.min(W, H) * 0.35 * Math.cos(angle);
                    n.y = H / 2 + Math.min(W, H) * 0.35 * Math.sin(angle);
                    n.vx = 0; n.vy = 0;
                });

                runGraphSimulation(W, H);
                drawRelationsGraph(svg);
                setupGraphInteraction(svg);

            } catch (err) {
                renderError('relations', err.message || String(err));
            }
        }

        function runGraphSimulation(W, H) {
            for (let step = 0; step < 120; step++) {
                // Repulsion
                for (let i = 0; i < graphNodes.length; i++) {
                    for (let j = i + 1; j < graphNodes.length; j++) {
                        const a = graphNodes[i], b = graphNodes[j];
                        const dx = b.x - a.x, dy = b.y - a.y;
                        const dist = Math.hypot(dx, dy) || 1;
                        if (dist < 140) {
                            const f = (140 - dist) / dist * 0.18;
                            a.x -= dx * f; a.y -= dy * f;
                            b.x += dx * f; b.y += dy * f;
                        }
                    }
                }
                // Attraction
                graphLinks.forEach(l => {
                    const s = graphNodeMap[l.source_id], t = graphNodeMap[l.target_id];
                    if (!s || !t) return;
                    const dx = t.x - s.x, dy = t.y - s.y;
                    const dist = Math.hypot(dx, dy) || 1;
                    const f = (dist - 110) / dist * 0.07;
                    s.x += dx * f; s.y += dy * f;
                    t.x -= dx * f; t.y -= dy * f;
                });
                // Gravity
                graphNodes.forEach(n => {
                    n.x += (W/2 - n.x) * 0.015;
                    n.y += (H/2 - n.y) * 0.015;
                });
            }
        }

        function drawRelationsGraph(svg) {
            svg.innerHTML = '';
            const transform = graphViewTransform;

            const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
            // Arrow markers per predicate color
            const usedColors = [...new Set(graphLinks.map(l => getPredicateColor(l.predicate)))];
            usedColors.forEach(color => {
                const markerId = 'arrow-' + color.replace('#', '');
                defs.innerHTML += `<marker id="${markerId}" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                    <path d="M 0 2 L 10 5 L 0 8 z" fill="${color}" opacity="0.8"/>
                </marker>`;
            });
            svg.appendChild(defs);

            const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
            g.setAttribute('transform', `translate(${transform.x},${transform.y}) scale(${transform.scale})`);
            svg.appendChild(g);

            // Edges
            graphLinks.forEach(l => {
                const s = graphNodeMap[l.source_id], t = graphNodeMap[l.target_id];
                if (!s || !t) return;
                const color = getPredicateColor(l.predicate);
                const markerId = 'arrow-' + color.replace('#', '');

                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', s.x); line.setAttribute('y1', s.y);
                line.setAttribute('x2', t.x); line.setAttribute('y2', t.y);
                line.setAttribute('stroke', color); line.setAttribute('stroke-width', '1.5');
                line.setAttribute('stroke-opacity', '0.7');
                line.setAttribute('marker-end', 'url(#' + markerId + ')');
                g.appendChild(line);

                const mx = (s.x + t.x) / 2, my = (s.y + t.y) / 2;
                const edgeLbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                edgeLbl.setAttribute('x', mx); edgeLbl.setAttribute('y', my - 4);
                edgeLbl.setAttribute('fill', color); edgeLbl.setAttribute('font-size', '9px');
                edgeLbl.setAttribute('font-weight', '600'); edgeLbl.setAttribute('text-anchor', 'middle');
                edgeLbl.setAttribute('opacity', '0.85');
                edgeLbl.textContent = l.predicate;
                g.appendChild(edgeLbl);
            });

            // Nodes
            graphNodes.forEach(n => {
                const ng = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                ng.style.cursor = 'pointer';

                ng.onmousedown = (e) => {
                    e.stopPropagation();
                    graphDraggedNode = n;
                };
                ng.onclick = (e) => { e.stopPropagation(); showEntityDetail(n.id, n.title); };

                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', n.x); circle.setAttribute('cy', n.y);
                circle.setAttribute('r', '11');
                circle.setAttribute('fill', '#2563eb'); circle.setAttribute('stroke', '#0f172a'); circle.setAttribute('stroke-width', '2');
                ng.appendChild(circle);

                const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                label.setAttribute('x', n.x); label.setAttribute('y', n.y + 25);
                label.setAttribute('fill', '#f3f4f6'); label.setAttribute('font-size', '10px');
                label.setAttribute('font-family', "'Outfit', sans-serif");
                label.setAttribute('text-anchor', 'middle');
                let t = n.title; if (t.length > 20) t = t.substring(0, 18) + '…';
                label.textContent = t;
                ng.appendChild(label);
                g.appendChild(ng);
            });
        }

        function setupGraphInteraction(svg) {
            let panStart = null;
            let panOrigin = null;

            svg.onmousedown = (e) => {
                if (graphDraggedNode) return;
                panStart = { x: e.clientX, y: e.clientY };
                panOrigin = { x: graphViewTransform.x, y: graphViewTransform.y };
                svg.style.cursor = 'grabbing';
            };

            svg.onmousemove = (e) => {
                if (graphDraggedNode) {
                    const rect = svg.getBoundingClientRect();
                    graphDraggedNode.x = (e.clientX - rect.left - graphViewTransform.x) / graphViewTransform.scale;
                    graphDraggedNode.y = (e.clientY - rect.top - graphViewTransform.y) / graphViewTransform.scale;
                    drawRelationsGraph(svg);
                } else if (panStart) {
                    graphViewTransform.x = panOrigin.x + (e.clientX - panStart.x);
                    graphViewTransform.y = panOrigin.y + (e.clientY - panStart.y);
                    drawRelationsGraph(svg);
                }
            };

            svg.onmouseup = () => {
                graphDraggedNode = null;
                panStart = null;
                svg.style.cursor = 'grab';
            };
            svg.onmouseleave = () => {
                graphDraggedNode = null;
                panStart = null;
                svg.style.cursor = 'grab';
            };

            svg.onwheel = (e) => {
                e.preventDefault();
                const rect = svg.getBoundingClientRect();
                const mx = e.clientX - rect.left;
                const my = e.clientY - rect.top;
                const oldScale = graphViewTransform.scale;
                const newScale = Math.max(0.2, Math.min(4, oldScale * (e.deltaY < 0 ? 1.12 : 0.9)));
                graphViewTransform.x = mx - (mx - graphViewTransform.x) * (newScale / oldScale);
                graphViewTransform.y = my - (my - graphViewTransform.y) * (newScale / oldScale);
                graphViewTransform.scale = newScale;
                drawRelationsGraph(svg);
            };
        }

        function resetGraphView() {
            graphViewTransform = { x: 0, y: 0, scale: 1 };
            const svg = document.getElementById('relations-svg');
            if (svg) drawRelationsGraph(svg);
        }

        function buildGraphLegend() {
            const legend = document.getElementById('graph-legend');
            const entries = Object.entries(PREDICATE_COLORS);
            legend.innerHTML = entries.map(([p, c]) => `
                <div class="legend-item">
                    <span class="legend-line" style="background:${c};"></span>
                    <span>${p}</span>
                </div>
            `).join('');
        }

        function populatePredicateDropdown() {
            const sel = document.getElementById('predicate-filter');
            const current = sel.value;
            sel.innerHTML = '<option value="">All predicates</option>';
            [...knownPredicates].sort().forEach(p => {
                const opt = document.createElement('option');
                opt.value = p; opt.textContent = p;
                if (p === current) opt.selected = true;
                sel.appendChild(opt);
            });
        }

        // ============================================================
        // Lineage Tab
        // ============================================================
        async function loadLineage() {
            const q = document.getElementById('lineage-search').value.trim();
            if (!q) return;

            document.getElementById('lineage-empty').style.display = 'none';
            document.getElementById('lineage-layout').style.display = 'none';

            // Try as entity ID first, then search for it
            let entityId = q;
            if (!q.match(/^[0-9a-f-]{36}$/i)) {
                // Try FTS to find the entity
                try {
                    const res = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=1');
                    const data = await res.json();
                    if (data.results && data.results.length > 0) {
                        entityId = data.results[0].id;
                    }
                } catch (e) {}
            }

            try {
                const res = await fetch('/api/lineage/' + entityId);
                const data = await res.json();

                if (data.error) {
                    document.getElementById('lineage-empty').textContent = 'Error: ' + data.error;
                    document.getElementById('lineage-empty').style.display = 'block';
                    return;
                }

                if (data.nodes.length <= 1) {
                    document.getElementById('lineage-empty').textContent = 'No consolidation lineage found for this entity.';
                    document.getElementById('lineage-empty').style.display = 'block';
                    return;
                }

                document.getElementById('lineage-layout').style.display = 'grid';

                // Render node list
                const list = document.getElementById('lineage-nodes-list');
                list.innerHTML = '';
                data.nodes.forEach(n => {
                    const item = document.createElement('div');
                    item.className = 'lineage-tree-item';
                    item.onclick = () => showEntityDetail(n.id, n.title);
                    const indent = '&nbsp;'.repeat(n.depth * 4);
                    item.innerHTML = `
                        ${indent}<div>
                            <div style="font-weight:500; font-size:0.9rem; color:#fff;">${escapeHtml(n.title)}</div>
                            <div style="font-size:0.75rem; color:var(--text-muted);">depth: ${n.depth} · <span class="status-badge ${n.status}">${n.status}</span></div>
                        </div>
                    `;
                    list.appendChild(item);
                });

                // Draw lineage SVG
                renderLineageSvg(data);

            } catch (err) {
                document.getElementById('lineage-empty').textContent = 'Failed: ' + err.message;
                document.getElementById('lineage-empty').style.display = 'block';
            }
        }

        function renderLineageSvg(data) {
            const svg = document.getElementById('lineage-svg');
            svg.innerHTML = '';

            const nodeMap = {};
            data.nodes.forEach(n => { nodeMap[n.id] = n; });

            const maxDepth = Math.max(...data.nodes.map(n => n.depth));
            const W = svg.clientWidth || 700;
            const H = svg.clientHeight || 460;
            const levelGroups = {};
            data.nodes.forEach(n => {
                if (!levelGroups[n.depth]) levelGroups[n.depth] = [];
                levelGroups[n.depth].push(n);
            });

            Object.entries(levelGroups).forEach(([depth, nodes]) => {
                const y = 60 + (parseInt(depth) / (maxDepth || 1)) * (H - 120);
                nodes.forEach((n, idx) => {
                    n.svgX = W * (idx + 1) / (nodes.length + 1);
                    n.svgY = y;
                });
            });

            // Edges
            data.edges.forEach(e => {
                const s = nodeMap[e.source], t = nodeMap[e.target];
                if (!s || !t || !s.svgX || !t.svgX) return;
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                line.setAttribute('x1', s.svgX); line.setAttribute('y1', s.svgY);
                line.setAttribute('x2', t.svgX); line.setAttribute('y2', t.svgY);
                line.setAttribute('stroke', '#c084fc'); line.setAttribute('stroke-width', '2');
                line.setAttribute('stroke-dasharray', '6,3');
                svg.appendChild(line);
            });

            // Nodes
            data.nodes.forEach(n => {
                if (!n.svgX) return;
                const color = n.id === data.root_id ? '#3b82f6' : (n.status === 'archived' ? '#475569' : '#22d3ee');
                const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                g.style.cursor = 'pointer';
                g.onclick = () => showEntityDetail(n.id, n.title);

                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', n.svgX); circle.setAttribute('cy', n.svgY);
                circle.setAttribute('r', '14'); circle.setAttribute('fill', color);
                circle.setAttribute('stroke', '#0f172a'); circle.setAttribute('stroke-width', '2');
                g.appendChild(circle);

                const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                lbl.setAttribute('x', n.svgX); lbl.setAttribute('y', n.svgY + 28);
                lbl.setAttribute('fill', '#f3f4f6'); lbl.setAttribute('font-size', '10px');
                lbl.setAttribute('text-anchor', 'middle'); lbl.setAttribute('font-family', "'Outfit',sans-serif");
                let t = n.title; if (t.length > 18) t = t.substring(0, 16) + '…';
                lbl.textContent = t;
                g.appendChild(lbl);
                svg.appendChild(g);
            });
        }

        // ============================================================
        // Pagination
        // ============================================================
        function renderPagination(type, pagination) {
            const container = document.getElementById(type + '-pagination');
            if (!container) return;
            const { page, pages, total } = pagination;
            if (pages <= 1) { container.innerHTML = ''; return; }
            container.innerHTML = `
                <button class="pagination-btn" ${page <= 1 ? 'disabled' : ''} onclick="changePage('${type}',${page - 1})">Prev</button>
                <span class="pagination-info">Page ${page} of ${pages} (${total} total)</span>
                <button class="pagination-btn" ${page >= pages ? 'disabled' : ''} onclick="changePage('${type}',${page + 1})">Next</button>
            `;
        }

        function changePage(type, targetPage) {
            if (type === 'entities') {
                currentEntitiesPage = targetPage;
                fetchEntities();
            } else if (type === 'events') {
                currentEventsPage = targetPage;
                fetchEvents();
            } else if (type === 'relations') {
                currentRelationsPage = targetPage;
                loadRelationsTab();
            }
        }

        // ============================================================
        // Error rendering (all tabs)
        // ============================================================
        function renderError(tabId, errorMsg) {
            const listMap = {
                entities: 'entities-list',
                events: 'events-list',
                tags: 'tags-list',
                locks: 'locks-list',
                relations: 'relations-sidebar-list'
            };
            const listId = listMap[tabId] || (tabId + '-list');
            const element = document.getElementById(listId);
            if (!element) return;

            const errorHtml = `
                <div style="grid-column:1/-1; text-align:center; padding:3rem; background:rgba(239,68,68,0.05); border:1px dashed var(--accent-error); border-radius:8px; margin:1rem 0; width:100%;">
                    <p style="color:var(--accent-error); font-weight:600; margin-bottom:0.5rem; font-size:1.1rem;">Operational Error</p>
                    <p style="color:var(--text-secondary);">${escapeHtml(errorMsg)}</p>
                </div>
            `;
            if (tabId === 'events') {
                element.innerHTML = '<tr><td colspan="6">' + errorHtml + '</td></tr>';
            } else {
                element.innerHTML = errorHtml;
            }
        }

        // ============================================================
        // Markdown Parser (fixed regex escapes)
        // ============================================================
        function parseMarkdown(md) {
            if (!md) return '';
            let html = escapeHtml(md);

            // Code blocks
            html = html.replace(/```((?:[^`]|`(?!``))*?)```/g, '<pre><code>$1</code></pre>');


            // Inline code
            html = html.replace(/`([^`\n]+)`/g, '<code class="code-snippet">$1</code>');

            // Headers (H3 first to avoid greedy match)
            html = html.replace(/^#{3} (.+)$/gm, '<h3>$1</h3>');
            html = html.replace(/^#{2} (.+)$/gm, '<h2>$1</h2>');
            html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

            // Blockquotes (&gt; is HTML-escaped >)
            html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

            // Bold then italic
            html = html.replace(/[*][*](.+?)[*][*]/g, '<strong>$1</strong>');
            html = html.replace(/[*](.+?)[*]/g, '<em>$1</em>');

            // Unordered list items
            html = html.replace(/^[*-] (.+)$/gm, '<li>$1</li>');

            // Paragraph breaks
            html = html.replace(/\n\n+/g, '<br><br>');
            html = html.replace(/\n/g, '<br>');

            return html;
        }

        // ============================================================
        // Utilities
        // ============================================================
        function escapeHtml(text) {
            if (text === null || text === undefined) return '';
            return String(text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function escapeAttr(text) {
            if (!text) return '';
            return String(text).replace(/'/g, "\\'").replace(/"/g, '\\"');
        }

        function formatDate(dateStr) {
            if (!dateStr) return 'N/A';
            try { return new Date(dateStr).toLocaleString(); } catch (e) { return dateStr; }
        }

        // ============================================================
        // Init
        // ============================================================
        loadStats();
        loadTabData('entities');
    </script>
</body>
</html>
"""
        self.send_html(html)

if __name__ == "__main__":
    # Ensure database is initialized before starting viewer
    if not os.path.exists(DB_PATH):
        # We need to initialize db, but viewer is read-only helper.
        # Create folder structure if missing
        db_dir = os.path.dirname(DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

    print(f"Starting SALTMDB Viewer on http://localhost:{PORT}")
    print(f"Reading database: {DB_PATH}")

    # Simple standard library socketserver
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), SALTMDBHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping SALTMDB Viewer.")
