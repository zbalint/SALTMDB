import sqlite3
import os
import sys
import uuid
import re
import json
from datetime import datetime, UTC

# Import init_db from saltmdb_server
from saltmdb_server import init_db

default_dir = os.path.expanduser("~/.saltmdb")
DB_PATH = os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))

EXAMPLE_DIR = "example_data"

# Event filter blacklist
BLACKLIST = [
    "session date",
    "operational context",
    "next_session_index",
    "last_session_index",
    "last_committed_index",
    "no regressions",
    "file:",
    "date:",
    "persona:",
    "title:",
    "environment:",
    "summary:",
    "instructions:",
    "what we organized",
    "obstacles overcome",
    "agent regression tracking",
    "decisions & commitments",
    "operational constraints noted",
    "key insights",
    "future safeguards",
    "current status",
    "immediate next steps",
    "user context",
    "patch content",
    "core.md patch",
    "memory.md patch",
    "operations.md patch",
    "registry.md patch",
    "none"
]

def should_skip_event(content):
    content_lower = content.lower().strip()
    if not content_lower:
        return True
    for phrase in BLACKLIST:
        if phrase in content_lower:
            return True
    return False

def load_session_catalog_dates():
    dates = {}
    path = os.path.join(EXAMPLE_DIR, "SESSION_CATALOG.md")
    if not os.path.exists(path):
        return dates
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            match = re.match(r"^\s*\|\s*([a-zA-Z0-9_-]+\.md)\s*\|\s*(\d{4}-\d{2}-\d{2})", line)
            if match:
                filename = match.group(1).strip()
                date_str = match.group(2).strip()
                dates[filename] = date_str
    return dates

def insert_tag_and_link(conn, entity_id, tag_name):
    # Ensure tag exists
    cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
    row = cursor.fetchone()
    if row:
        tag_id = row[0]
    else:
        tag_id = str(uuid.uuid4())
        conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
    # Link tag
    conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))

def import_core_md(conn):
    print("Importing CORE.md...")
    path = os.path.join(EXAMPLE_DIR, "CORE.md")
    if not os.path.exists(path):
        print("CORE.md not found.")
        return
        
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
        
    now = datetime.now(UTC).isoformat()
    entity_id = str(uuid.uuid4())
    title = "Universal Identity & Behavioral Baseline"
    
    with conn:
        conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
            VALUES (?, ?, ?, ?, 'system', 'shared', 1, 5, 'consolidated', '[]', ?, ?)
        """, (entity_id, now, now, now, title, content))
        
    insert_tag_and_link(conn, entity_id, "#core")
    insert_tag_and_link(conn, entity_id, "#identity")

def import_claude_md(conn):
    print("Importing CLAUDE.md...")
    path = os.path.join(EXAMPLE_DIR, "CLAUDE.md")
    if not os.path.exists(path):
        print("CLAUDE.md not found.")
        return
        
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
        
    now = datetime.now(UTC).isoformat()
    entity_id = str(uuid.uuid4())
    title = "CLAUDE.md Bootstrap Entry Point"
    
    with conn:
        conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
            VALUES (?, ?, ?, ?, 'system', 'shared', 0, 1, 'consolidated', '[]', ?, ?)
        """, (entity_id, now, now, now, title, content))
        
    insert_tag_and_link(conn, entity_id, "#mcp-meta")
    insert_tag_and_link(conn, entity_id, "#bootstrap")

def import_memory_md(conn):
    print("Importing MEMORY.md...")
    path = os.path.join(EXAMPLE_DIR, "MEMORY.md")
    if not os.path.exists(path):
        print("MEMORY.md not found.")
        return
        
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Split content into sections
    sections = re.split(r"^##\s+", content, flags=re.MULTILINE)
    
    now = datetime.now(UTC).isoformat()
    
    for section in sections:
        section = section.strip()
        if not section:
            continue
            
        lines = section.splitlines()
        header = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        
        if header.startswith("1. Active Initiatives"):
            # Parse subsections under Active Initiatives
            subsections = re.split(r"^###\s+", body, flags=re.MULTILINE)
            for sub in subsections:
                sub = sub.strip()
                if not sub:
                    continue
                sub_lines = sub.splitlines()
                sub_title = sub_lines[0].strip()
                sub_body = "\n".join(sub_lines[1:]).strip()
                
                # Determine tag
                tag = "#general"
                if "[tea]" in sub_title:
                    tag = "#tea"
                elif "[ops]" in sub_title:
                    tag = "#ops"
                    
                entity_id = str(uuid.uuid4())
                with conn:
                    conn.execute("""
                        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                        VALUES (?, ?, ?, ?, 'system', 'shared', 0, 1, 'consolidated', '[]', ?, ?)
                    """, (entity_id, now, now, now, sub_title, f"# {sub_title}\n\n{sub_body}"))
                insert_tag_and_link(conn, entity_id, tag)
                
        elif header.startswith("2. Cross-Domain Decisions"):
            entity_id = str(uuid.uuid4())
            with conn:
                conn.execute("""
                    INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                    VALUES (?, ?, ?, ?, 'system', 'shared', 0, 2, 'consolidated', '[]', ?, ?)
                """, (entity_id, now, now, now, "Cross-Domain Decisions", f"## Cross-Domain Decisions\n\n{body}"))
            insert_tag_and_link(conn, entity_id, "#decisions")
            insert_tag_and_link(conn, entity_id, "#meta")
            
        elif header.startswith("3. Agent Regression Prevention Core"):
            # Parse subsections (Tea and Ops regression preventions)
            subsections = re.split(r"^###\s+", body, flags=re.MULTILINE)
            for sub in subsections:
                sub = sub.strip()
                if not sub:
                    continue
                sub_lines = sub.splitlines()
                sub_title = sub_lines[0].strip()
                sub_body = "\n".join(sub_lines[1:]).strip()
                
                tag = "#general-rules"
                if "[tea]" in sub_title:
                    tag = "#tea-rules"
                elif "[ops]" in sub_title:
                    tag = "#ops-rules"
                    
                entity_id = str(uuid.uuid4())
                with conn:
                    # Mark these safeguards as core rules (is_core=1) or high priority (weight=3)
                    conn.execute("""
                        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                        VALUES (?, ?, ?, ?, 'system', 'shared', 1, 3, 'consolidated', '[]', ?, ?)
                    """, (entity_id, now, now, now, sub_title, f"# {sub_title}\n\n{sub_body}"))
                insert_tag_and_link(conn, entity_id, tag)
                insert_tag_and_link(conn, entity_id, "#rules")

def parse_and_import_sessions(conn):
    print("Importing sessions from tea and ops...")
    catalog_dates = load_session_catalog_dates()
    
    session_dirs = [
        ("Tea", os.path.join(EXAMPLE_DIR, "sessions", "tea")),
        ("Tea", os.path.join(EXAMPLE_DIR, "sessions", "tea", "archive")),
        ("Ops", os.path.join(EXAMPLE_DIR, "sessions", "ops", "archive"))
    ]
    
    event_count = 0
    
    for agent_id, directory in session_dirs:
        if not os.path.exists(directory):
            continue
            
        for filename in os.listdir(directory):
            if not filename.endswith(".md"):
                continue
                
            path = os.path.join(directory, filename)
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
            # Try to resolve date from Catalog, or fallback to file parsing
            date_str = catalog_dates.get(filename)
            if not date_str:
                for line in lines[:10]:
                    if line.startswith("Date:"):
                        date_str = line.split(":", 1)[1].strip()
                        break
            
            # Format timestamp
            if date_str:
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    timestamp = dt.replace(tzinfo=UTC).isoformat()
                except Exception:
                    timestamp = datetime.now(UTC).isoformat()
            else:
                timestamp = datetime.now(UTC).isoformat()
                
            # Parse events
            with conn:
                for line in lines:
                    line = line.strip()
                    match = re.match(r"^(?:[-*]\s+)?\[(DECISION|FACT|ISSUE|RESOLUTION|STATUS|OPEN|IDEA)\]\s+(.+)$", line, re.IGNORECASE)
                    if match:
                        etype_raw = match.group(1).upper()
                        content = match.group(2).strip()
                        
                        # Apply blacklist filter to skip metadata
                        if should_skip_event(content):
                            continue
                            
                        # Map type
                        if etype_raw == "DECISION":
                            etype = "decision"
                        elif etype_raw == "ISSUE":
                            etype = "issue"
                        elif etype_raw == "RESOLUTION":
                            etype = "fix"
                        else:
                            etype = "attempt"
                            
                        event_id = str(uuid.uuid4())
                        conn.execute("""
                            INSERT INTO events (id, timestamp, agent_id, type, content, error_code)
                            VALUES (?, ?, ?, ?, ?, NULL)
                        """, (event_id, timestamp, agent_id, etype, content))
                        event_count += 1
                        
    print(f"Successfully imported {event_count} events from session files!")

def main():
    print(f"Connecting to database: {DB_PATH}")
    
    # Check if database has data and --force flag is missing
    if os.path.exists(DB_PATH) and "--force" not in sys.argv:
        conn = sqlite3.connect(DB_PATH)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM entities")
            entity_count = cursor.fetchone()[0]
            cursor = conn.execute("SELECT COUNT(*) FROM events")
            event_count = cursor.fetchone()[0]
            if entity_count > 0 or event_count > 0:
                print("Warning: Database already contains data. Running this script will wipe all data!")
                print("Run with '--force' to proceed (e.g. python scratch/import_example_data.py --force).")
                conn.close()
                sys.exit(1)
        except sqlite3.OperationalError:
            # Table might not exist yet, safe to proceed
            pass
        finally:
            conn.close()

    conn = init_db(DB_PATH)
    
    # Clean previous data to allow fresh import
    with conn:
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM tags")
        conn.execute("DELETE FROM entity_tags")
        conn.execute("DELETE FROM entities_fts")
        conn.execute("DELETE FROM events")
        
    import_core_md(conn)
    import_claude_md(conn)
    import_memory_md(conn)
    parse_and_import_sessions(conn)
    
    conn.close()
    print("Example data import complete!")

if __name__ == "__main__":
    main()
