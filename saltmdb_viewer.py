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

__version__ = "0.1.0-alpha.15"

PORT = 8080

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
            self.get_all_relations()
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
        try:
            page = 1
            if "page" in query:
                try:
                    page = int(query["page"][0])
                except ValueError:
                    pass
            limit = 100
            offset = (page - 1) * limit
            
            conn = self.get_db_connection()
            cursor = conn.execute("""
                SELECT id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title
                FROM entities
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            rows = cursor.fetchall()
            
            # Fetch total count for pagination
            count_cursor = conn.execute("SELECT COUNT(*) FROM entities")
            total_count = count_cursor.fetchone()[0]
            
            entities = []
            for r in rows:
                tag_cursor = conn.execute("""
                    SELECT t.name FROM tags t
                    JOIN entity_tags et ON t.id = et.tag_id
                    WHERE et.entity_id = ?
                """, (r[0],))
                tags = [tag_row[0] for tag_row in tag_cursor.fetchall()]
                
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
                    "tags": tags
                })
            conn.close()
            self.send_json({
                "entities": entities,
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

    def get_events(self, query):
        try:
            page = 1
            if "page" in query:
                try:
                    page = int(query["page"][0])
                except ValueError:
                    pass
            limit = 100
            offset = (page - 1) * limit
            
            conn = self.get_db_connection()
            cursor = conn.execute("""
                SELECT id, timestamp, agent_id, type, content, error_code
                FROM events
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            rows = cursor.fetchall()
            
            count_cursor = conn.execute("SELECT COUNT(*) FROM events")
            total_count = count_cursor.fetchone()[0]
            conn.close()
            
            events = [{
                "id": r[0],
                "timestamp": r[1],
                "agent_id": r[2],
                "type": r[3],
                "content": r[4],
                "error_code": r[5]
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

    def get_tags(self):
        try:
            conn = self.get_db_connection()
            cursor = conn.execute("""
                SELECT t.id, t.name, t.canonical_id, p.name as canonical_name
                FROM tags t
                LEFT JOIN tags p ON t.canonical_id = p.id
            """)
            rows = cursor.fetchall()
            
            tags = []
            for r in rows:
                count_cursor = conn.execute("SELECT COUNT(*) FROM entity_tags WHERE tag_id = ?", (r[0],))
                count = count_cursor.fetchone()[0]
                
                tags.append({
                    "id": r[0],
                    "name": r[1],
                    "canonical_id": r[2],
                    "canonical_name": r[3],
                    "count": count
                })
            conn.close()
            self.send_json(tags)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def get_locks(self):
        try:
            conn = self.get_db_connection()
            cursor = conn.execute("SELECT task_name, locked_at, locked_by_pid, last_run_at FROM _system_locks")
            rows = cursor.fetchall()
            conn.close()
            
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

    def get_entity_detail(self, entity_id):
        try:
            conn = self.get_db_connection()
            # 1. Fetch content
            cursor = conn.execute("SELECT full_content, title FROM entities WHERE id = ?", (entity_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                self.send_json({"error": "Entity not found"}, 404)
                return
                
            full_content, title = row
            
            # 2. Fetch outgoing relations (this entity is the source)
            cursor = conn.execute("""
                SELECT r.id, r.predicate, r.target_id, e.title, r.valid_from, r.valid_to
                FROM relations r
                JOIN entities e ON r.target_id = e.id
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
            
            # 3. Fetch incoming relations (this entity is the target)
            cursor = conn.execute("""
                SELECT r.id, r.predicate, r.source_id, e.title, r.valid_from, r.valid_to
                FROM relations r
                JOIN entities e ON r.source_id = e.id
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
            
            conn.close()
            self.send_json({
                "id": entity_id,
                "title": title,
                "full_content": full_content,
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

    def get_all_relations(self):
        try:
            conn = self.get_db_connection()
            cursor = conn.execute("""
                SELECT r.id, r.predicate, r.source_id, e1.title, r.target_id, e2.title, r.valid_from, r.valid_to
                FROM relations r
                JOIN entities e1 ON r.source_id = e1.id
                JOIN entities e2 ON r.target_id = e2.id
                ORDER BY r.created_at DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            
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
            self.send_json(relations)
        except sqlite3.OperationalError as e:
            msg = str(e)
            if "no such table" in msg:
                msg = "Database not initialized. Please run the MCP server first to create tables."
            self.send_json({"error": msg}, 500)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def serve_frontend(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SALTMDB Database Viewer</title>
    <!-- Premium Google Fonts -->
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
            --text-primary: #f3f4f6;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --border-color: rgba(255, 255, 255, 0.08);
            --transition-smooth: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: var(--bg-base);
            color: var(--text-primary);
            line-height: 1.5;
            padding: 2rem;
            min-height: 100vh;
        }

        h1, h2, h3, h4 {
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
        }

        .brand {
            display: flex;
            flex-direction: column;
        }

        .brand h1 {
            font-size: 2.2rem;
            letter-spacing: -0.5px;
            background: linear-gradient(135deg, #fff 0%, #94a3b8 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .brand span {
            font-size: 0.85rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 2px;
            margin-top: 0.2rem;
        }

        .db-path {
            font-size: 0.85rem;
            background-color: rgba(255, 255, 255, 0.04);
            border: 1px solid var(--border-color);
            padding: 0.5rem 1rem;
            border-radius: 99px;
            color: var(--text-secondary);
            font-family: monospace;
        }

        /* Tabs Navigation */
        .tabs {
            display: flex;
            gap: 0.5rem;
            background-color: rgba(255, 255, 255, 0.02);
            padding: 0.35rem;
            border-radius: 12px;
            border: 1px solid var(--border-color);
            margin-bottom: 2rem;
            width: fit-content;
        }

        .tab-btn {
            background: none;
            border: none;
            color: var(--text-secondary);
            padding: 0.75rem 1.5rem;
            font-size: 0.95rem;
            font-weight: 500;
            border-radius: 8px;
            cursor: pointer;
            transition: var(--transition-smooth);
        }

        .tab-btn:hover {
            color: var(--text-primary);
            background-color: rgba(255, 255, 255, 0.04);
        }

        .tab-btn.active {
            color: #fff;
            background-color: var(--accent-primary);
            box-shadow: 0 4px 12px var(--accent-primary-glow);
        }

        /* Main View Container */
        .view-content {
            display: none;
            animation: fadeIn 0.3s ease-in-out forwards;
        }

        .view-content.active {
            display: block;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Search Bar */
        .controls-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            gap: 1rem;
        }

        .search-input {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 0.8rem 1.2rem;
            color: var(--text-primary);
            font-size: 0.95rem;
            width: 100%;
            max-width: 400px;
            transition: var(--transition-smooth);
        }

        .search-input:focus {
            outline: none;
            border-color: var(--accent-primary);
            box-shadow: 0 0 0 3px var(--accent-primary-glow);
        }

        /* Entities View */
        .entities-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
            gap: 1.5rem;
        }

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

        .entity-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 3px;
            background: transparent;
            transition: var(--transition-smooth);
        }

        .entity-card:hover {
            transform: translateY(-4px);
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 12px 24px rgba(0, 0, 0, 0.4);
        }

        .entity-card.raw::before { background: var(--accent-primary); }
        .entity-card.consolidated::before { background: var(--accent-success); }
        .entity-card.archived::before { background: var(--text-muted); }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
        }

        .status-badge {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            padding: 0.25rem 0.6rem;
            border-radius: 6px;
            letter-spacing: 0.5px;
        }

        .status-badge.raw { background-color: rgba(37, 99, 235, 0.15); color: #3b82f6; }
        .status-badge.consolidated { background-color: rgba(16, 185, 129, 0.15); color: #10b981; }
        .status-badge.archived { background-color: rgba(148, 163, 184, 0.15); color: #94a3b8; }

        .card-title {
            font-size: 1.15rem;
            color: #fff;
            margin-bottom: 0.75rem;
            line-height: 1.4;
        }

        .card-meta {
            font-size: 0.8rem;
            color: var(--text-secondary);
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
            margin-bottom: 1rem;
        }

        .meta-item {
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }

        .meta-label {
            color: var(--text-muted);
            min-width: 90px;
        }

        .card-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 0.4rem;
            margin-top: auto;
        }

        .tag-pill {
            font-size: 0.75rem;
            background-color: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            padding: 0.2rem 0.5rem;
            border-radius: 6px;
            color: var(--text-secondary);
        }

        /* Events Table View */
        .table-container {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }

        th {
            background-color: rgba(255, 255, 255, 0.02);
            padding: 1rem 1.25rem;
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 1rem 1.25rem;
            font-size: 0.9rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            color: var(--text-primary);
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background-color: rgba(255, 255, 255, 0.01);
        }

        .event-type {
            font-weight: 600;
            font-size: 0.8rem;
            text-transform: uppercase;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            width: fit-content;
        }

        .event-type.issue { background-color: rgba(239, 68, 68, 0.1); color: var(--accent-error); }
        .event-type.attempt { background-color: rgba(245, 158, 11, 0.1); color: var(--accent-warning); }
        .event-type.fix { background-color: rgba(16, 185, 129, 0.1); color: var(--accent-success); }
        .event-type.decision { background-color: rgba(37, 99, 235, 0.1); color: var(--accent-primary); }

        .code-snippet {
            font-family: monospace;
            background-color: rgba(0,0,0,0.2);
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
            border: 1px solid var(--border-color);
        }

        /* Tags View */
        .tags-container {
            display: flex;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .tag-card {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            padding: 1rem 1.5rem;
            border-radius: 12px;
            display: flex;
            align-items: center;
            gap: 1rem;
            transition: var(--transition-smooth);
        }

        .tag-card:hover {
            border-color: rgba(255, 255, 255, 0.15);
            background-color: var(--bg-surface-elevated);
        }

        .tag-name {
            font-weight: 500;
            color: #fff;
        }

        .tag-badge-count {
            background-color: var(--accent-primary);
            color: #fff;
            font-size: 0.8rem;
            font-weight: 600;
            padding: 0.15rem 0.5rem;
            border-radius: 99px;
        }

        .tag-alias {
            font-size: 0.8rem;
            color: var(--text-muted);
        }

        /* Locks Status */
        .locks-row {
            display: flex;
            gap: 1.5rem;
            flex-wrap: wrap;
        }

        .lock-status-card {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1.5rem;
            width: 100%;
            max-width: 450px;
        }

        .lock-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.25rem;
        }

        .lock-indicator {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.9rem;
            font-weight: 600;
        }

        .indicator-dot {
            width: 10px;
            height: 10px;
            border-radius: 99px;
        }

        .indicator-dot.active { background-color: var(--accent-warning); box-shadow: 0 0 10px var(--accent-warning); }
        .indicator-dot.inactive { background-color: var(--accent-success); box-shadow: 0 0 10px var(--accent-success); }

        /* Detail Modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(11, 15, 25, 0.85);
            backdrop-filter: blur(8px);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            padding: 2rem;
        }

        .modal-card {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            width: 100%;
            max-width: 800px;
            max-height: 85vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 24px 48px rgba(0,0,0,0.5);
            animation: modalSlide 0.3s cubic-bezier(0.4, 0, 0.2, 1) forwards;
        }

        @keyframes modalSlide {
            from { transform: scale(0.95); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
        }

        .modal-header {
            padding: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .modal-body {
            padding: 2rem;
            overflow-y: auto;
            flex-grow: 1;
        }

        .modal-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.5rem;
            cursor: pointer;
            transition: var(--transition-smooth);
        }

        .modal-close:hover {
            color: #fff;
        }

        .markdown-render {
            font-family: inherit;
            color: var(--text-primary);
        }

        .markdown-render pre {
            background-color: rgba(0, 0, 0, 0.3);
            border: 1px solid var(--border-color);
            padding: 1rem;
            border-radius: 8px;
            overflow-x: auto;
            font-family: monospace;
            margin: 1rem 0;
        }

        .markdown-render h1, .markdown-render h2, .markdown-render h3 {
            margin-top: 1.5rem;
            margin-bottom: 0.75rem;
            color: #fff;
        }

        .markdown-render p {
            margin-bottom: 1rem;
            color: var(--text-secondary);
            font-size: 1rem;
            line-height: 1.6;
        }

        .markdown-render ul, .markdown-render ol {
            margin-left: 1.5rem;
            margin-bottom: 1rem;
            color: var(--text-secondary);
        }

        .markdown-render li {
            margin-bottom: 0.4rem;
        }

        .markdown-render blockquote {
            border-left: 4px solid var(--accent-primary);
            padding-left: 1rem;
            margin: 1rem 0;
            color: var(--text-muted);
            font-style: italic;
        }

        /* Pagination styles */
        .pagination-controls {
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 1.5rem;
            margin-top: 2rem;
            padding: 1rem 0;
        }
        .pagination-btn {
            background-color: var(--bg-surface);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 0.5rem 1.25rem;
            border-radius: 8px;
            cursor: pointer;
            font-family: inherit;
            font-size: 0.875rem;
            font-weight: 600;
            transition: var(--transition-smooth);
        }
        .pagination-btn:hover:not(:disabled) {
            background-color: var(--bg-surface-elevated);
            border-color: var(--accent-primary);
            box-shadow: 0 0 10px var(--accent-primary-glow);
        }
        .pagination-btn:disabled {
            opacity: 0.35;
            cursor: not-allowed;
        }
        .pagination-info {
            color: var(--text-secondary);
            font-size: 0.875rem;
            font-weight: 500;
        }
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

    <nav class="tabs">
        <button class="tab-btn active" onclick="switchTab('entities')">Entities (Long-Term)</button>
        <button class="tab-btn" onclick="switchTab('events')">Events (Short-Term)</button>
        <button class="tab-btn" onclick="switchTab('tags')">Tags folksonomy</button>
        <button class="tab-btn" onclick="switchTab('relations')">Relations Topology</button>
        <button class="tab-btn" onclick="switchTab('locks')">System Locks</button>
    </nav>

    <!-- Entities Tab -->
    <div id="tab-entities" class="view-content active">
        <div class="controls-row">
            <input type="text" class="search-input" id="entity-search" placeholder="Search memories by title or id..." oninput="filterEntities()">
        </div>
        <div class="entities-grid" id="entities-list">
            <!-- Loaded dynamically -->
        </div>
        <div class="pagination-controls" id="entities-pagination"></div>
    </div>

    <!-- Events Tab -->
    <div id="tab-events" class="view-content">
        <div class="controls-row">
            <input type="text" class="search-input" id="event-search" placeholder="Filter events by content or agent..." oninput="filterEvents()">
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th style="width: 180px;">Timestamp</th>
                        <th style="width: 120px;">Agent ID</th>
                        <th style="width: 100px;">Type</th>
                        <th>Content</th>
                        <th style="width: 100px;">Error Code</th>
                    </tr>
                </thead>
                <tbody id="events-list">
                    <!-- Loaded dynamically -->
                </tbody>
            </table>
        </div>
        <div class="pagination-controls" id="events-pagination"></div>
    </div>

    <!-- Tags Tab -->
    <div id="tab-tags" class="view-content">
        <div class="tags-container" id="tags-list">
            <!-- Loaded dynamically -->
        </div>
    </div>

    <!-- Locks Tab -->
    <div id="tab-locks" class="view-content">
        <div class="locks-row" id="locks-list">
            <!-- Loaded dynamically -->
        </div>
        <p style="margin-top: 2.5rem; color: var(--text-muted); font-size: 0.875rem; text-align: center; font-style: italic;">
            Note: Ephemeral memories are stored in RAM by the active MCP process and are not visible in this dashboard.
        </p>
    </div>

    <!-- Relations Tab -->
    <div id="tab-relations" class="view-content">
        <div class="relations-layout" style="display:grid; grid-template-columns: 1fr 320px; gap: 2rem; margin-top: 1rem;">
            <!-- Interactive Graph Visualization -->
            <div class="graph-container" style="background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; position: relative; overflow: hidden; height: 600px;">
                <svg id="relations-svg" width="100%" height="100%" style="cursor: grab;"></svg>
                <div style="position: absolute; bottom: 1rem; right: 1rem; background: rgba(15,23,42,0.8); border: 1px solid #1e293b; padding: 0.5rem 1rem; border-radius: 8px; font-size: 0.8rem; pointer-events: none; color: #94a3b8;">
                    Drag nodes to rearrange. Click to inspect memory.
                </div>
            </div>
            <!-- Sidebar Table / Detail List -->
            <div class="relations-sidebar" style="background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 1.5rem; overflow-y: auto; height: 600px; display: flex; flex-direction: column;">
                <h3 style="color:#f3f4f6; margin-top:0; margin-bottom:1rem; font-family:'Outfit',sans-serif;">All Relationships</h3>
                <div id="relations-sidebar-list" style="display:flex; flex-direction:column; gap:0.75rem; overflow-y:auto; flex: 1;">
                    <!-- Loaded dynamically -->
                </div>
            </div>
        </div>
    </div>

    <!-- Markdown Modal -->
    <div class="modal-overlay" id="detail-modal" onclick="closeModal(event)">
        <div class="modal-card" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h3 id="modal-title">Memory Detail</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body markdown-render" id="modal-content">
                <!-- Content rendering -->
            </div>
        </div>
    </div>

    <script>
        let allEntities = [];
        let allEvents = [];
        let currentEntitiesPage = 1;
        let currentEventsPage = 1;

        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.view-content').forEach(view => view.classList.remove('active'));
            
            // Highlight tab button programmatically
            const buttons = document.querySelectorAll('.tab-btn');
            let matchedBtn = null;
            if (tabId === 'entities') matchedBtn = buttons[0];
            else if (tabId === 'events') matchedBtn = buttons[1];
            else if (tabId === 'tags') matchedBtn = buttons[2];
            else if (tabId === 'relations') matchedBtn = buttons[3];
            else if (tabId === 'locks') matchedBtn = buttons[4];
            
            if (matchedBtn) matchedBtn.classList.add('active');
            document.getElementById(`tab-${tabId}`).classList.add('active');
            
            loadTabData(tabId);
        }

        async function loadTabData(tabId) {
            try {
                if (tabId === 'entities') {
                    const res = await fetch(`/api/entities?page=${currentEntitiesPage}`);
                    const data = await res.json();
                    if (data.error) {
                        renderError('entities', data.error);
                        return;
                    }
                    allEntities = data.entities;
                    renderEntities(allEntities);
                    renderPagination('entities', data.pagination);
                } else if (tabId === 'events') {
                    const res = await fetch(`/api/events?page=${currentEventsPage}`);
                    const data = await res.json();
                    if (data.error) {
                        renderError('events', data.error);
                        return;
                    }
                    allEvents = data.events;
                    renderEvents(allEvents);
                    renderPagination('events', data.pagination);
                } else if (tabId === 'tags') {
                    const res = await fetch('/api/tags');
                    const tags = await res.json();
                    if (tags.error) {
                        renderError('tags', tags.error);
                        return;
                    }
                    renderTags(tags);
                } else if (tabId === 'relations') {
                    loadRelationsTab();
                } else if (tabId === 'locks') {
                    const res = await fetch('/api/locks');
                    const locks = await res.json();
                    if (locks.error) {
                        renderError('locks', locks.error);
                        return;
                    }
                    renderLocks(locks);
                }
            } catch (err) {
                console.error(`Failed to load ${tabId} data:`, err);
                renderError(tabId, err.message || err);
            }
        }

        function renderEntities(entities) {
            const list = document.getElementById('entities-list');
            list.innerHTML = '';
            
            if (entities.length === 0) {
                list.innerHTML = '<p style="grid-column: 1/-1; color: var(--text-muted); text-align: center; padding: 2rem;">No long-term memories found.</p>';
                return;
            }

            entities.forEach(e => {
                const card = document.createElement('div');
                card.className = `entity-card ${e.status}`;
                card.onclick = () => showEntityDetail(e.id, e.title);
                
                const tagsHtml = e.tags.map(t => `<span class="tag-pill">${t}</span>`).join('');
                
                card.innerHTML = `
                    <div>
                        <div class="card-header">
                            <span class="status-badge ${e.status}">${e.status}</span>
                            ${e.is_core ? '<span class="status-badge" style="background-color:rgba(245,158,11,0.15); color:#f59e0b;">core</span>' : ''}
                        </div>
                        <h3 class="card-title">${escapeHtml(e.title)}</h3>
                        <div class="card-meta">
                            <div class="meta-item"><span class="meta-label">ID:</span><span>${e.id.substring(0, 8)}...</span></div>
                            <div class="meta-item"><span class="meta-label">Weight:</span><span>${e.weight}</span></div>
                            <div class="meta-item"><span class="meta-label">Updated:</span><span>${formatDate(e.updated_at)}</span></div>
                            <div class="meta-item"><span class="meta-label">Accessed:</span><span>${formatDate(e.last_accessed_at)}</span></div>
                        </div>
                    </div>
                    <div class="card-tags">${tagsHtml}</div>
                `;
                list.appendChild(card);
            });
        }

        function renderEvents(events) {
            const list = document.getElementById('events-list');
            list.innerHTML = '';

            if (events.length === 0) {
                list.innerHTML = '<tr><td colspan="5" style="color: var(--text-muted); text-align: center; padding: 2rem;">No short-term events logged.</td></tr>';
                return;
            }

            events.forEach(ev => {
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td style="color: var(--text-secondary);">${formatDate(ev.timestamp)}</td>
                    <td style="font-weight: 500;">${escapeHtml(ev.agent_id)}</td>
                    <td><span class="event-type ${ev.type.toLowerCase()}">${ev.type}</span></td>
                    <td style="word-break: break-word;">${escapeHtml(ev.content)}</td>
                    <td>${ev.error_code ? `<span class="code-snippet">${escapeHtml(ev.error_code)}</span>` : '<span style="color:var(--text-muted);">-</span>'}</td>
                `;
                list.appendChild(row);
            });
        }

        function renderTags(tags) {
            const list = document.getElementById('tags-list');
            list.innerHTML = '';

            if (tags.length === 0) {
                list.innerHTML = '<p style="color: var(--text-muted); padding: 2rem;">No folksonomy tags found.</p>';
                return;
            }

            tags.forEach(t => {
                const card = document.createElement('div');
                card.className = 'tag-card';
                card.innerHTML = `
                    <div>
                        <div class="tag-name">${escapeHtml(t.name)}</div>
                        ${t.canonical_id ? `<div class="tag-alias">alias of: <strong>${escapeHtml(t.canonical_name)}</strong></div>` : ''}
                    </div>
                    <span class="tag-badge-count">${t.count}</span>
                `;
                list.appendChild(card);
            });
        }

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
                        <div class="meta-item"><span class="meta-label">PID:</span><span>${l.locked_by_pid ? l.locked_by_pid : 'N/A'}</span></div>
                        <div class="meta-item"><span class="meta-label">Last Run:</span><span>${l.last_run_at ? formatDate(l.last_run_at) : 'Never'}</span></div>
                    </div>
                `;
                list.appendChild(card);
            });
        }

        function filterEntities() {
            const query = document.getElementById('entity-search').value.toLowerCase();
            const filtered = allEntities.filter(e => 
                e.title.toLowerCase().includes(query) || 
                e.id.toLowerCase().includes(query)
            );
            renderEntities(filtered);
        }

        function filterEvents() {
            const query = document.getElementById('event-search').value.toLowerCase();
            const filtered = allEvents.filter(ev => 
                ev.content.toLowerCase().includes(query) || 
                ev.agent_id.toLowerCase().includes(query) ||
                ev.type.toLowerCase().includes(query)
            );
            renderEvents(filtered);
        }

        async function showEntityDetail(id, title) {
            try {
                const res = await fetch(`/api/entity/${id}`);
                const data = await res.json();
                
                let relationsHtml = '';
                if (data.relations && (data.relations.outgoing.length > 0 || data.relations.incoming.length > 0)) {
                    relationsHtml += '<div class="relations-modal-section" style="margin-top:2rem; border-top:1px solid #334155; padding-top:1.5rem;">';
                    relationsHtml += '<h3 style="color:#f3f4f6; margin-bottom:1rem; font-family:Outfit,sans-serif;">Connected Relationships</h3>';
                    
                    if (data.relations.outgoing.length > 0) {
                        relationsHtml += '<div style="margin-bottom:1rem;">';
                        relationsHtml += '<h4 style="color:#94a3b8; font-size:0.875rem; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.5rem;">Outgoing Relations (This node is Subject)</h4>';
                        data.relations.outgoing.forEach(r => {
                            relationsHtml += `<div class="relation-item" style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span>this --(<strong style="color:#a855f7;">${escapeHtml(r.predicate)}</strong>)--> <a href="#" onclick="closeModal(); showEntityDetail('${r.target_id}', '${escapeHtml(r.target_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.target_title)}</a></span>
                            </div>`;
                        });
                        relationsHtml += '</div>';
                    }
                    
                    if (data.relations.incoming.length > 0) {
                        relationsHtml += '<div style="margin-bottom:1rem;">';
                        relationsHtml += '<h4 style="color:#94a3b8; font-size:0.875rem; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:0.5rem;">Incoming Relations (This node is Object)</h4>';
                        data.relations.incoming.forEach(r => {
                            relationsHtml += `<div class="relation-item" style="background:#1e293b; border:1px solid #334155; border-radius:8px; padding:0.5rem 1rem; margin-bottom:0.5rem;">
                                <span><a href="#" onclick="closeModal(); showEntityDetail('${r.source_id}', '${escapeHtml(r.source_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.source_title)}</a> --(<strong style="color:#a855f7;">${escapeHtml(r.predicate)}</strong>)--> this</span>
                            </div>`;
                        });
                        relationsHtml += '</div>';
                    }
                    relationsHtml += '</div>';
                }
                
                document.getElementById('modal-title').innerText = title;
                document.getElementById('modal-content').innerHTML = parseMarkdown(data.full_content) + relationsHtml;
                document.getElementById('detail-modal').style.display = 'flex';
            } catch (err) {
                console.error("Failed to fetch entity detail:", err);
            }
        }

        let isGraphDragging = false;
        let draggedNode = null;
        
        async function loadRelationsTab() {
            try {
                const res = await fetch('/api/relations');
                const relations = await res.json();
                
                const sidebar = document.getElementById('relations-sidebar-list');
                sidebar.innerHTML = '';
                
                if (relations.error) {
                    renderError('relations', relations.error);
                    return;
                }
                
                if (relations.length === 0) {
                    sidebar.innerHTML = '<p style="color: var(--text-muted); text-align: center; padding: 2rem;">No relationships found in database.</p>';
                    document.getElementById('relations-svg').innerHTML = '';
                    return;
                }
                
                relations.forEach(r => {
                    const item = document.createElement('div');
                    item.style = 'background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 0.75rem; font-size: 0.9rem;';
                    item.innerHTML = `
                        <div style="display:flex; justify-content:space-between; margin-bottom:0.25rem;">
                            <a href="#" onclick="showEntityDetail('${r.source_id}', '${escapeHtml(r.source_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.source_title)}</a>
                            <span style="color:#a855f7; font-weight:600; font-size:0.8rem; text-transform:uppercase;">${escapeHtml(r.predicate)}</span>
                        </div>
                        <div style="display:flex; justify-content:flex-end;">
                            <a href="#" onclick="showEntityDetail('${r.target_id}', '${escapeHtml(r.target_title)}'); return false;" style="color:#3b82f6; text-decoration:none; font-weight:500;">${escapeHtml(r.target_title)}</a>
                        </div>
                    `;
                    sidebar.appendChild(item);
                });
                
                // Build Graph Nodes & Edges
                const nodeMap = {};
                const nodes = [];
                const links = [];
                
                relations.forEach(r => {
                    if (!nodeMap[r.source_id]) {
                        nodeMap[r.source_id] = { id: r.source_id, title: r.source_title };
                        nodes.push(nodeMap[r.source_id]);
                    }
                    if (!nodeMap[r.target_id]) {
                        nodeMap[r.target_id] = { id: r.target_id, title: r.target_title };
                        nodes.push(nodeMap[r.target_id]);
                    }
                    links.push({
                        id: r.id,
                        source_id: r.source_id,
                        target_id: r.target_id,
                        predicate: r.predicate
                    });
                });
                
                const svg = document.getElementById('relations-svg');
                const svgWidth = svg.clientWidth || 800;
                const svgHeight = svg.clientHeight || 600;
                
                // Position nodes circular as a starting point
                nodes.forEach((n, idx) => {
                    const angle = (idx / nodes.length) * 2 * Math.PI;
                    n.x = svgWidth / 2 + 180 * Math.cos(angle);
                    n.y = svgHeight / 2 + 180 * Math.sin(angle);
                });
                
                const centerX = svgWidth / 2;
                const centerY = svgHeight / 2;
                
                function runSimulation() {
                    for (let step = 0; step < 100; step++) {
                        // Repulsion
                        for (let i = 0; i < nodes.length; i++) {
                            for (let j = i + 1; j < nodes.length; j++) {
                                const n1 = nodes[i];
                                const n2 = nodes[j];
                                const dx = n2.x - n1.x;
                                const dy = n2.y - n1.y;
                                const dist = Math.hypot(dx, dy) || 1;
                                if (dist < 160) {
                                    const force = (160 - dist) / dist * 0.2;
                                    n1.x -= dx * force;
                                    n1.y -= dy * force;
                                    n2.x += dx * force;
                                    n2.y += dy * force;
                                }
                            }
                        }
                        // Attraction
                        links.forEach(l => {
                            const s = nodeMap[l.source_id];
                            const t = nodeMap[l.target_id];
                            if (s && t) {
                                const dx = t.x - s.x;
                                const dy = t.y - s.y;
                                const dist = Math.hypot(dx, dy) || 1;
                                const force = (dist - 120) / dist * 0.08;
                                s.x += dx * force;
                                s.y += dy * force;
                                t.x -= dx * force;
                                t.y -= dy * force;
                            }
                        });
                        // Gravity to center
                        nodes.forEach(n => {
                            n.x += (centerX - n.x) * 0.02;
                            n.y += (centerY - n.y) * 0.02;
                        });
                    }
                }
                
                runSimulation();
                
                function drawGraph() {
                    svg.innerHTML = '';
                    
                    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
                    defs.innerHTML = `
                        <marker id="arrow" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                            <path d="M 0 2 L 10 5 L 0 8 z" fill="#475569" />
                        </marker>
                    `;
                    svg.appendChild(defs);
                    
                    // Links
                    links.forEach(l => {
                        const s = nodeMap[l.source_id];
                        const t = nodeMap[l.target_id];
                        if (!s || !t) return;
                        
                        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                        line.setAttribute('x1', s.x);
                        line.setAttribute('y1', s.y);
                        line.setAttribute('x2', t.x);
                        line.setAttribute('y2', t.y);
                        line.setAttribute('stroke', '#334155');
                        line.setAttribute('stroke-width', '2');
                        line.setAttribute('marker-end', 'url(#arrow)');
                        svg.appendChild(line);
                        
                        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                        text.setAttribute('x', (s.x + t.x) / 2);
                        text.setAttribute('y', (s.y + t.y) / 2 - 5);
                        text.setAttribute('fill', '#c084fc');
                        text.setAttribute('font-size', '10px');
                        text.setAttribute('font-weight', '600');
                        text.setAttribute('text-anchor', 'middle');
                        text.textContent = l.predicate;
                        svg.appendChild(text);
                    });
                    
                    // Nodes
                    nodes.forEach(n => {
                        const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
                        g.style.cursor = 'pointer';
                        
                        g.onmousedown = (e) => {
                            e.stopPropagation();
                            draggedNode = n;
                            isGraphDragging = true;
                        };
                        
                        g.onclick = (e) => {
                            e.stopPropagation();
                            showEntityDetail(n.id, n.title);
                        };
                        
                        let color = '#38bdf8'; // Default sky blue
                        if (n.title.startsWith('[ops]')) color = '#f87171'; // Red
                        else if (n.title.startsWith('[tea]')) color = '#34d399'; // Green
                        else if (n.title.toLowerCase().includes('universal')) color = '#fbbf24'; // Gold
                        
                        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                        circle.setAttribute('cx', n.x);
                        circle.setAttribute('cy', n.y);
                        circle.setAttribute('r', '10');
                        circle.setAttribute('fill', color);
                        circle.setAttribute('stroke', '#0f172a');
                        circle.setAttribute('stroke-width', '2');
                        g.appendChild(circle);
                        
                        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                        text.setAttribute('x', n.x);
                        text.setAttribute('y', n.y + 24);
                        text.setAttribute('fill', '#f3f4f6');
                        text.setAttribute('font-size', '11px');
                        text.setAttribute('font-family', "'Outfit', sans-serif");
                        text.setAttribute('text-anchor', 'middle');
                        
                        let trimmedTitle = n.title;
                        if (trimmedTitle.length > 22) {
                            trimmedTitle = trimmedTitle.substring(0, 20) + '...';
                        }
                        text.textContent = trimmedTitle;
                        
                        svg.appendChild(g);
                        
                        // Wait a tick to build rect behind labels
                        setTimeout(() => {
                            try {
                                const bbox = text.getBBox();
                                const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
                                rect.setAttribute('x', bbox.x - 4);
                                rect.setAttribute('y', bbox.y - 2);
                                rect.setAttribute('width', bbox.width + 8);
                                rect.setAttribute('height', bbox.height + 4);
                                rect.setAttribute('rx', 4);
                                rect.setAttribute('fill', 'rgba(15, 23, 42, 0.85)');
                                g.insertBefore(rect, text);
                            } catch (e) {}
                        }, 0);
                        g.appendChild(text);
                    });
                }
                
                drawGraph();
                
                svg.onmousemove = (e) => {
                    if (isGraphDragging && draggedNode) {
                        const rect = svg.getBoundingClientRect();
                        draggedNode.x = e.clientX - rect.left;
                        draggedNode.y = e.clientY - rect.top;
                        drawGraph();
                    }
                };
                
                svg.onmouseup = () => {
                    isGraphDragging = false;
                    draggedNode = null;
                };
                svg.onmouseleave = () => {
                    isGraphDragging = false;
                    draggedNode = null;
                };
            } catch (err) {
                console.error("Failed to render relations graph:", err);
            }
        }

        function closeModal(event) {
            document.getElementById('detail-modal').style.display = 'none';
        }

        // Markdown parsing helper
        function parseMarkdown(md) {
            if (!md) return '';
            let html = md;
            
            // Code Blocks
            html = html.replace(/```([\\s\\S]*?)```/g, '<pre><code>$1</code></pre>');
            
            // Inline Code
            html = html.replace(/`([^`]+)`/g, '<code class="code-snippet">$1</code>');
            
            // Headers
            html = html.replace(/^#\\s+(.+)$/gm, '<h1>$1</h1>');
            html = html.replace(/^##\\s+(.+)$/gm, '<h2>$1</h2>');
            html = html.replace(/^###\\s+(.+)$/gm, '<h3>$1</h3>');
            
            // Blockquotes
            html = html.replace(/^>\\s+(.+)$/gm, '<blockquote>$1</blockquote>');
            
            // Lists
            html = html.replace(/^\\*\\s+(.+)$/gm, '<li>$1</li>');
            html = html.replace(/^-\\s+(.+)$/gm, '<li>$1</li>');
            
            // Linebreaks/Paragraphs
            html = html.replace(/^\\s*$/gm, '<br>');
            
            return html;
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        function formatDate(dateStr) {
            if (!dateStr) return 'N/A';
            try {
                const d = new Date(dateStr);
                return d.toLocaleString();
            } catch (e) {
                return dateStr;
            }
        }

        function renderPagination(type, pagination) {
            const container = document.getElementById(`${type}-pagination`);
            if (!container) return;
            
            const { page, pages, total } = pagination;
            if (pages <= 1) {
                container.innerHTML = '';
                return;
            }
            
            container.innerHTML = `
                <button class="pagination-btn" id="${type}-prev" ${page <= 1 ? 'disabled' : ''} onclick="changePage('${type}', ${page - 1})">Prev</button>
                <span class="pagination-info">Page ${page} of ${pages} (${total} total)</span>
                <button class="pagination-btn" id="${type}-next" ${page >= pages ? 'disabled' : ''} onclick="changePage('${type}', ${page + 1})">Next</button>
            `;
        }

        function changePage(type, targetPage) {
            if (type === 'entities') {
                currentEntitiesPage = targetPage;
            } else {
                currentEventsPage = targetPage;
            }
            loadTabData(type);
        }

        function renderError(tabId, errorMsg) {
            const listId = tabId === 'entities' ? 'entities-list' : (tabId === 'events' ? 'events-list' : (tabId === 'tags' ? 'tags-list' : 'locks-list'));
            const element = document.getElementById(listId);
            if (!element) return;
            
            const errorHtml = `
                <div style="grid-column: 1/-1; text-align: center; padding: 3rem; background-color: rgba(239, 68, 68, 0.05); border: 1px dashed var(--accent-error); border-radius: 8px; margin: 1rem 0; width: 100%;">
                    <p style="color: var(--accent-error); font-weight: 600; margin-bottom: 0.5rem; font-size: 1.1rem;">Operational Error</p>
                    <p style="color: var(--text-secondary); font-size: 0.95rem;">${escapeHtml(errorMsg)}</p>
                </div>
            `;
            if (tabId === 'events') {
                element.innerHTML = `<tr><td colspan="5">${errorHtml}</td></tr>`;
            } else {
                element.innerHTML = errorHtml;
            }
        }

        // Init
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
