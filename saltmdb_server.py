import sqlite3
import os
import json
import uuid
import re
import sys
from datetime import datetime, UTC
from mcp.server.fastmcp import FastMCP

__version__ = "0.1.0-alpha.2"

# Define the FastMCP server
mcp = FastMCP("SALTMDB")

# Ephemeral in-memory database connection
EPHEMERAL_CONN = sqlite3.connect(":memory:", check_same_thread=False, timeout=10.0)

def init_ephemeral_db():
    with EPHEMERAL_CONN:
        EPHEMERAL_CONN.execute("""
        CREATE TABLE IF NOT EXISTS ephemeral_memories (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)

# Initialize the ephemeral database immediately
init_ephemeral_db()

def get_db_path() -> str:
    default_dir = os.path.expanduser("~/.saltmdb")
    os.makedirs(default_dir, exist_ok=True)
    return os.environ.get("SALTMDB_DB_PATH", os.path.join(default_dir, "saltmdb.db"))

# Core Regex patterns for credential redacting
SECRET_PATTERNS = [
    r"\bghp_[a-zA-Z0-9]{36,}\b",                # GitHub personal access token (classic)
    r"\bgithub_pat_[a-zA-Z0-9_]{82,}\b",         # GitHub fine-grained token
    r"\bsk-ant-sid01-[a-zA-Z0-9_-]{20,}\b",     # Anthropic session key
    r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b",            # Anthropic API key
    r"\bsk-[a-zA-Z0-9_-]{48,}\b",                # OpenAI API key
    r"\bsk-proj-[a-zA-Z0-9_-]{20,}\b",           # OpenAI project key
    r"\b[a-zA-Z0-9_]{20,}:[a-zA-Z0-9_]{40,}\b",  # Generic API secret pattern (ID:Secret)
    r"\bAKIA[A-Z0-9]{16}\b",                     # AWS access key ID
    r"\b[M-Q][a-zA-Z0-9_\-]{23}\.[a-zA-Z0-9_\-]{6}\.[a-zA-Z0-9_\-]{27}\b" # Discord token
]

# Custom redaction patterns loaded from .saltmdb_redact
CUSTOM_REDACT_PATTERNS = []

def load_custom_redact_patterns():
    global CUSTOM_REDACT_PATTERNS
    CUSTOM_REDACT_PATTERNS = []
    if os.path.exists(".saltmdb_redact"):
        try:
            with open(".saltmdb_redact", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        try:
                            # Test compile to make sure it's valid regex
                            re.compile(line)
                            CUSTOM_REDACT_PATTERNS.append(line)
                        except re.error as e:
                            print(f"Warning: Invalid regex pattern in .saltmdb_redact: '{line}' ({e})")
        except Exception as e:
            print(f"Warning: Failed to read .saltmdb_redact: {e}")

# Load custom patterns at startup
load_custom_redact_patterns()

def redact_secrets(text: str) -> str:
    """Scrub potential credentials and API keys from text."""
    if not isinstance(text, str):
        return text
    redacted = text
    # Core patterns
    for pattern in SECRET_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED_SECRET]", redacted, flags=re.IGNORECASE)
    # Custom patterns
    for pattern in CUSTOM_REDACT_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED_SECRET]", redacted, flags=re.IGNORECASE)
    return redacted

def init_db(db_path: str):
    """Initialize the local SQLite database with Write-Ahead Logging (WAL) and schemas."""
    # timeout=10.0 handles concurrent lock retrying transparently
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    
    with conn:
        # 1. Events Table (Short-Term append-only ledger)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            agent_id TEXT NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            error_code TEXT
        );
        """)
        
        # 2. Entities Table (Long-Term knowledge base)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            last_accessed_at DATETIME NOT NULL,
            owner_id TEXT,
            scope TEXT CHECK(scope IN ('private', 'shared')) DEFAULT 'shared',
            is_core BOOLEAN DEFAULT 0,
            weight INTEGER DEFAULT 1,
            status TEXT CHECK(status IN ('raw', 'consolidated', 'archived')) DEFAULT 'raw',
            parent_ids TEXT, -- JSON array of ancestor IDs
            title TEXT NOT NULL,
            full_content TEXT NOT NULL
        );
        """)
        
        # 3. Tags Table (Folksonomy with support for canonical aliases)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            canonical_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (canonical_id) REFERENCES tags(id) ON DELETE SET NULL
        );
        """)
        
        # 4. Entity Tags Join Table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_tags (
            entity_id TEXT,
            tag_id TEXT,
            PRIMARY KEY (entity_id, tag_id),
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
        """)
        
        # 5. Virtual FTS5 Table for weighted keyword search
        conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
            id UNINDEXED,
            title,
            full_content
        );
        """)
        
        # 6. Mutex Lock Table for Leader Election
        conn.execute("""
        CREATE TABLE IF NOT EXISTS _system_locks (
            task_name TEXT PRIMARY KEY,
            locked_at DATETIME,
            locked_by_pid INTEGER,
            last_run_at DATETIME
        );
        """)
        
        # Schema migration: attempt to add last_run_at column if updating an existing database
        try:
            conn.execute("ALTER TABLE _system_locks ADD COLUMN last_run_at DATETIME;")
        except sqlite3.OperationalError:
            pass # Column already exists
            
        conn.execute("""
        INSERT OR IGNORE INTO _system_locks (task_name, locked_at, locked_by_pid, last_run_at) 
        VALUES ('librarian_consolidation', NULL, NULL, NULL);
        """)
        
        # Triggers to keep FTS5 and Entities in sync
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS insert_entity_fts
        AFTER INSERT ON entities
        WHEN NEW.status != 'archived'
        BEGIN
            INSERT INTO entities_fts(id, title, full_content) VALUES (NEW.id, NEW.title, NEW.full_content);
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS update_entity_fts
        AFTER UPDATE ON entities
        WHEN NEW.status != 'archived' AND OLD.status != 'archived'
        BEGIN
            UPDATE entities_fts SET title = NEW.title, full_content = NEW.full_content WHERE id = OLD.id;
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS update_entity_fts_unarchived
        AFTER UPDATE ON entities
        WHEN NEW.status != 'archived' AND OLD.status = 'archived'
        BEGIN
            INSERT INTO entities_fts(id, title, full_content) VALUES (NEW.id, NEW.title, NEW.full_content);
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS archive_memory_fts
        AFTER UPDATE ON entities
        WHEN NEW.status = 'archived'
        BEGIN
            DELETE FROM entities_fts WHERE id = OLD.id;
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_entity_fts
        AFTER DELETE ON entities
        BEGIN
            DELETE FROM entities_fts WHERE id = OLD.id;
        END;
        """)
        
    return conn

def extract_title_and_snippet(markdown_text: str):
    """Heuristic helper to extract a clean title and snippet from markdown text."""
    if not markdown_text:
        return "Untitled", ""
    lines = [line.strip() for line in markdown_text.splitlines() if line.strip()]
    title = "Untitled"
    for line in lines:
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break
            
    if title == "Untitled" and lines:
        title = lines[0]
        if len(title) > 60:
            title = title[:57] + "..."
            
    text_lines = []
    for line in lines:
        if not line.startswith("#"):
            text_lines.append(line)
            if len(text_lines) >= 3:
                break
                
    snippet = " ".join(text_lines)
    if len(snippet) > 150:
        snippet = snippet[:147] + "..."
    return title, snippet

def trigger_librarian():
    """Asynchronously spawns the librarian consolidation process if threshold is met and cooldown has expired."""
    db_path = get_db_path()
    try:
        # Check raw count and cooldown before spawning process
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            # 1. Check raw count
            cursor = conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'raw'")
            raw_count = cursor.fetchone()[0]
            if raw_count < 2:
                conn.close()
                return
                
            # 2. Check cooldown (5 minutes / 300 seconds)
            cursor = conn.execute("SELECT last_run_at FROM _system_locks WHERE task_name = 'librarian_consolidation'")
            row = cursor.fetchone()
            if row and row[0]:
                last_run_str = row[0].replace("Z", "")
                if "+" in last_run_str:
                    last_run_str = last_run_str.split("+")[0]
                last_run = datetime.fromisoformat(last_run_str)
                elapsed = (datetime.now(UTC).replace(tzinfo=None) - last_run).total_seconds()
                if elapsed < 300:
                    conn.close()
                    return
        finally:
            conn.close()
    except Exception:
        # Fallback to spawn if check fails (e.g. database lock)
        pass

    try:
        import subprocess
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW prevents flashing terminal windows on Windows
            creationflags = 0x08000000
        subprocess.Popen(
            [sys.executable, __file__, "--librarian"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
    except Exception:
        pass

# =====================================================================
# MCP Server Tools
# =====================================================================

@mcp.tool()
def log_event(agent_id: str, type: str, content: str, error_code: str = None) -> str:
    """Appends an event to the append-only events ledger.
    
    Args:
        agent_id: Identifier of the agent logging the event.
        type: Category of the event (e.g. 'issue', 'attempt', 'fix', 'decision').
        content: Description of the action or event.
        error_code: Optional system error code if applicable.
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    event_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content, error_code)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event_id, now, agent_id, type, redacted_content, error_code))
        trigger_librarian()
        return f"Event logged successfully with ID: {event_id}"
    except Exception as e:
        return f"Error logging event: {e}"
    finally:
        conn.close()

@mcp.tool()
def get_canonical_tags(domain: str = None) -> list:
    """Queries the database to suggest existing canonical tags to prevent fragmentation.
    
    Args:
        domain: Optional prefix/substring to filter matching tags.
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        if domain:
            cursor = conn.execute("""
                SELECT id, name FROM tags 
                WHERE canonical_id IS NULL AND name LIKE ?
            """, (f"%{domain}%",))
        else:
            cursor = conn.execute("""
                SELECT id, name FROM tags 
                WHERE canonical_id IS NULL
            """)
        rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()

@mcp.tool()
def store_knowledge(content: str, tags: list, scope: str, weight: int = 1, is_core: bool = False, owner_id: str = None, title: str = None) -> str:
    """Stores a consolidated Markdown fact chunk in the long-term knowledge base.
    
    Args:
        content: Markdown formatted text representation of the fact.
        tags: List of tags associated with this knowledge.
        scope: Scope level ('private' or 'shared').
        weight: Priority ranking multiplier (default 1).
        is_core: If True, bypasses search and gets injected into the agent prompt (default False).
        owner_id: Optional ID of the agent/owner storing this knowledge.
        title: Optional custom title. If omitted, the first markdown heading is auto-extracted.
    """
    if scope not in ('private', 'shared'):
        return "Error: scope must be either 'private' or 'shared'"
        
    db_path = get_db_path()
    conn = init_db(db_path)
    entity_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    
    # Hybrid title extraction
    if not title:
        title, _ = extract_title_and_snippet(redacted_content)
        
    try:
        with conn:
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?)
            """, (entity_id, now, now, now, owner_id, scope, 1 if is_core else 0, weight, json.dumps([]), title, redacted_content))
            
            for tag_name in tags:
                tag_name = tag_name.strip()
                if not tag_name:
                    continue
                # Check if tag already exists or is an alias
                cursor = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (tag_name,))
                row = cursor.fetchone()
                if row:
                    tag_id = row[1] if row[1] else row[0]
                else:
                    tag_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)", (tag_id, tag_name))
                
                # Link tag to entity
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                
        trigger_librarian()
        return f"Knowledge stored successfully with ID: {entity_id}"
    except Exception as e:
        return f"Error storing knowledge: {e}"
    finally:
        conn.close()

@mcp.tool()
def search_memory(query_keywords: str = None, tags_filter: list = None) -> list:
    """Performs full-text keyword search and tag filtering in the long-term knowledge base.
    
    Args:
        query_keywords: Search terms used to match against indexing content via FTS5.
        tags_filter: List of tag names; if provided, matched items must have all specified tags.
    """
    if not query_keywords and not tags_filter:
        return []
        
    db_path = get_db_path()
    conn = init_db(db_path)
    now = datetime.now(UTC).isoformat()
    try:
        params = []
        tag_filter_clause = ""
        
        if tags_filter:
            placeholders = ",".join("?" for _ in tags_filter)
            tag_filter_clause = f"""
                AND e.id IN (
                    SELECT et.entity_id 
                    FROM entity_tags et 
                    JOIN tags t ON et.tag_id = t.id 
                    WHERE t.name IN ({placeholders})
                    GROUP BY et.entity_id
                    HAVING COUNT(DISTINCT t.name) = ?
                )
            """
            params.extend(tags_filter)
            params.append(len(tags_filter))
            
        if query_keywords:
            # 10:1 title-to-content search weighting using sqlite FTS5 bm25 column weights
            sql = f"""
                SELECT e.id, e.full_content, e.weight, bm25(entities_fts, 0.0, 10.0, 1.0) as score, e.title
                FROM entities_fts f
                JOIN entities e ON e.id = f.id
                WHERE entities_fts MATCH ? AND e.status != 'archived' {tag_filter_clause}
                ORDER BY (bm25(entities_fts, 0.0, 10.0, 1.0) * e.weight) ASC
                LIMIT 5
            """
            exec_params = [query_keywords] + params
        else:
            sql = f"""
                SELECT e.id, e.full_content, e.weight, 0.0 as score, e.title
                FROM entities e
                WHERE e.status != 'archived' {tag_filter_clause}
                ORDER BY e.weight DESC, e.updated_at DESC
                LIMIT 5
            """
            exec_params = params
            
        cursor = conn.execute(sql, exec_params)
        rows = cursor.fetchall()
        
        results = []
        entity_ids = []
        for entity_id, full_content, weight, score, title in rows:
            _, snippet = extract_title_and_snippet(full_content)
            results.append({
                "id": entity_id,
                "title": title,
                "snippet": snippet,
                "score": score,
                "weight": weight
            })
            entity_ids.append(entity_id)
            
        # Update last_accessed_at for matched entities (LRU access signal)
        if entity_ids:
            with conn:
                placeholders = ",".join("?" for _ in entity_ids)
                conn.execute(f"""
                    UPDATE entities 
                    SET last_accessed_at = ? 
                    WHERE id IN ({placeholders})
                """, [now] + entity_ids)
                
        return results
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()

@mcp.tool()
def fetch_memory_chunk(entity_id: str) -> str:
    """Fetches the exact complete markdown text of a specific knowledge base ID.
    
    Args:
        entity_id: The UUID of the memory chunk.
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    now = datetime.now(UTC).isoformat()
    try:
        cursor = conn.execute("SELECT full_content FROM entities WHERE id = ?", (entity_id,))
        row = cursor.fetchone()
        if row:
            # Update last_accessed_at (LRU access signal)
            with conn:
                conn.execute("UPDATE entities SET last_accessed_at = ? WHERE id = ?", (now, entity_id))
            return row[0]
        return f"Error: Memory ID '{entity_id}' not found"
    except Exception as e:
        return f"Error fetching memory: {e}"
    finally:
        conn.close()

@mcp.tool()
def store_ephemeral_memory(key: str, value: str) -> str:
    """Stores a temporary, volatile variable in the in-memory database.
    This database is cleared when the server stops.
    
    Args:
        key: Unique variable name/key.
        value: Temporary data string (e.g. OTP, short-lived session token).
    """
    redacted_value = redact_secrets(value)
    try:
        with EPHEMERAL_CONN:
            EPHEMERAL_CONN.execute("""
                INSERT OR REPLACE INTO ephemeral_memories (key, value)
                VALUES (?, ?)
            """, (key, redacted_value))
        return f"Ephemeral memory stored for key: '{key}'"
    except Exception as e:
        return f"Error storing ephemeral memory: {e}"

@mcp.tool()
def get_ephemeral_memory(key: str) -> str:
    """Retrieves a temporary, volatile variable from the in-memory database.
    
    Args:
        key: Unique variable name/key to look up.
    """
    try:
        cursor = EPHEMERAL_CONN.execute("SELECT value FROM ephemeral_memories WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return row[0]
        return f"Error: Key '{key}' not found in ephemeral memory"
    except Exception as e:
        return f"Error retrieving ephemeral memory: {e}"

@mcp.tool()
def start_db_viewer() -> str:
    """Spawns the local SALTMDB web dashboard/viewer in the background on port 8080.
    Returns the URL link to access the dashboard.
    """
    import urllib.request
    
    # Check if the viewer is already running by making a fast request
    is_running = False
    try:
        with urllib.request.urlopen("http://localhost:8080/", timeout=0.5) as res:
            if res.status == 200:
                is_running = True
    except Exception:
        pass
        
    if is_running:
        return "SALTMDB Database Viewer is already running! Open it in your browser at http://localhost:8080"
        
    # Start it in the background
    try:
        import subprocess
        viewer_script = os.path.join(os.path.dirname(__file__), "saltmdb_viewer.py")
        
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
            
        subprocess.Popen([sys.executable, viewer_script], **kwargs)
        return "SALTMDB Database Viewer started successfully! Open it in your browser at http://localhost:8080"
    except Exception as e:
        return f"Error starting database viewer: {e}"

# =====================================================================
# Librarian / Garbage Collection Process
# =====================================================================

def call_llm(system_prompt: str, user_prompt: str) -> str:
    """Utilizes direct HTTP requests to call available API keys in environment."""
    import urllib.request
    import urllib.error
    
    gemini_key = os.environ.get("GEMINI_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    
    if gemini_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [
                    {"text": f"System Instruction: {system_prompt}"},
                    {"text": user_prompt}
                ]
            }]
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                response_data = json.loads(res.read().decode("utf-8"))
                return response_data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Gemini API call failed: {e}")
            
    elif openai_key:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_key}"
        }
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                response_data = json.loads(res.read().decode("utf-8"))
                return response_data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"OpenAI API call failed: {e}")
            
    elif anthropic_key:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": anthropic_key,
            "anthropic-version": "2023-06-01"
        }
        payload = {
            "model": "claude-3-5-haiku-latest",
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 4096
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as res:
                response_data = json.loads(res.read().decode("utf-8"))
                return response_data["content"][0]["text"]
        except Exception as e:
            print(f"Anthropic API call failed: {e}")
            
    return None

def acquire_librarian_lock(conn) -> bool:
    """Attempts to acquire the atomic leader lock for the librarian process.
    Guarantees only one Librarian instance runs concurrently. Expire locks older than 10 mins.
    """
    pid = os.getpid()
    now = datetime.now(UTC).isoformat()
    with conn:
        cursor = conn.execute("""
            UPDATE _system_locks 
            SET locked_at = ?, locked_by_pid = ? 
            WHERE task_name = 'librarian_consolidation' 
              AND (locked_at IS NULL OR datetime(locked_at) < datetime('now', '-10 minutes'))
        """, (now, pid))
        return cursor.rowcount == 1

def release_librarian_lock(conn):
    """Releases the librarian leader lock and records the execution timestamp."""
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute("""
            UPDATE _system_locks 
            SET locked_at = NULL, locked_by_pid = NULL, last_run_at = ? 
            WHERE task_name = 'librarian_consolidation'
        """, (now,))

def merge_tags_heuristics(conn):
    """Scans tags to merge duplicate and near-identical names to prevent folksonomy fragmentation."""
    print("Running Tag Merging...")
    with conn:
        cursor = conn.execute("SELECT id, name, canonical_id FROM tags")
        tags = cursor.fetchall()
        
        # Group by normalized tag name (lowercase, stripped of symbols)
        grouped = {}
        for tag_id, name, canonical_id in tags:
            if canonical_id is not None:
                continue
            norm = name.lower().strip().replace("-", "").replace("_", "").replace("#", "")
            grouped.setdefault(norm, []).append((tag_id, name))
            
        for norm, tag_list in grouped.items():
            if len(tag_list) > 1:
                canonical_id, canonical_name = tag_list[0]
                print(f"Merging tags into canonical tag: '{canonical_name}' ({canonical_id})")
                for tag_id, name in tag_list[1:]:
                    print(f"  - Marking alias tag: '{name}' ({tag_id})")
                    conn.execute("UPDATE tags SET canonical_id = ? WHERE id = ?", (canonical_id, tag_id))
                    # Update all mapping references
                    conn.execute("UPDATE OR IGNORE entity_tags SET tag_id = ? WHERE tag_id = ?", (canonical_id, tag_id))
                    # Delete outdated mappings
                    conn.execute("DELETE FROM entity_tags WHERE tag_id = ? AND entity_id IN (SELECT entity_id FROM entity_tags WHERE tag_id = ?)", (tag_id, canonical_id))
                    conn.execute("UPDATE entity_tags SET tag_id = ? WHERE tag_id = ?", (canonical_id, tag_id))

def decay_lru_memories(conn):
    """Gradually decays weights of non-core memories not accessed in 90 days, archiving them at <= 0."""
    print("Running Access Decay (LRU) check...")
    now = datetime.now(UTC).isoformat()
    with conn:
        # Decrement weight for stale unaccessed items
        conn.execute("""
            UPDATE entities 
            SET weight = weight - 1, updated_at = ? 
            WHERE is_core = 0 
              AND status != 'archived'
              AND datetime(last_accessed_at) < datetime('now', '-90 days')
        """, (now,))
        
        # Archive any whose weight decays to 0 or below
        cursor = conn.execute("""
            SELECT id FROM entities 
            WHERE weight <= 0 AND status != 'archived'
        """)
        to_archive = [row[0] for row in cursor.fetchall()]
        if to_archive:
            placeholders = ",".join("?" for _ in to_archive)
            conn.execute(f"""
                UPDATE entities 
                SET status = 'archived', updated_at = ? 
                WHERE id IN ({placeholders})
            """, [now] + to_archive)
            print(f"Archived {len(to_archive)} stale memories due to access decay.")

def consolidate_cluttered_tags(conn):
    """Identifies tags with 5 or more raw entries and consolidates them."""
    print("Checking for high tag density clutter...")
    cursor = conn.execute("""
        SELECT et.tag_id, t.name, COUNT(*) 
        FROM entity_tags et
        JOIN entities e ON et.entity_id = e.id
        JOIN tags t ON et.tag_id = t.id
        WHERE e.status = 'raw'
        GROUP BY et.tag_id
        HAVING COUNT(*) >= 5
    """)
    cluttered = cursor.fetchall()
    if not cluttered:
        print("No cluttered tag clusters found.")
        return
        
    for tag_id, tag_name, count in cluttered:
        print(f"Tag '{tag_name}' has {count} raw memories. Initiating consolidation...")
        cursor = conn.execute("""
            SELECT e.id, e.full_content, e.owner_id, e.scope
            FROM entities e
            JOIN entity_tags et ON e.id = et.entity_id
            WHERE et.tag_id = ? AND e.status = 'raw'
        """, (tag_id,))
        raw_entities = cursor.fetchall()
        if len(raw_entities) < 2:
            continue
            
        parent_ids = [e[0] for e in raw_entities]
        contents = [e[1] for e in raw_entities]
        owner_id = raw_entities[0][2]
        scope = raw_entities[0][3]
        
        system_prompt = (
            "You are a memory consolidation assistant. Your task is to merge multiple short-term raw markdown facts "
            "into a single, cohesive, consolidated markdown document. Remove duplicates and resolve contradictions, "
            "preferring newer or more detailed information. Keep the structure clean and maintain all crucial facts."
        )
        user_prompt = f"Here are the raw memory chunks under tag {tag_name} to consolidate:\n\n"
        for idx, content in enumerate(contents, 1):
            user_prompt += f"--- Chunk {idx} ---\n{content}\n\n"
            
        consolidated_content = call_llm(system_prompt, user_prompt)
        if not consolidated_content:
            print("Using fallback concatenation for consolidation.")
            consolidated_content = f"# Consolidated Memory for {tag_name}\n\n" + "\n\n---\n\n".join(contents)
            
        new_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        title = f"Consolidated Memory for {tag_name}"
        
        with conn:
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'consolidated', ?, ?, ?)
            """, (new_id, now, now, now, owner_id, scope, json.dumps(parent_ids), title, consolidated_content))
            
            # Map tags from parents to the new entity
            placeholders = ",".join("?" for _ in parent_ids)
            tag_cursor = conn.execute(f"""
                SELECT DISTINCT tag_id FROM entity_tags WHERE entity_id IN ({placeholders})
            """, parent_ids)
            parent_tags = [row[0] for row in tag_cursor.fetchall()]
            for p_tag_id in parent_tags:
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (new_id, p_tag_id))
                
            # Archive original entities
            conn.execute(f"""
                UPDATE entities SET status = 'archived', updated_at = ? WHERE id IN ({placeholders})
            """, [now] + parent_ids)
            
        print(f"Successfully consolidated cluttered tag '{tag_name}' into new entity {new_id}")

def consolidate_memories(conn):
    """General consolidator that consolidates short-term 'raw' facts sharing matching properties."""
    print("Running General Memory Consolidation...")
    cursor = conn.execute("""
        SELECT e.id, e.full_content, e.owner_id, e.scope, e.title
        FROM entities e
        WHERE e.status = 'raw'
    """)
    raw_entities = cursor.fetchall()
    if not raw_entities:
        print("No raw memories to consolidate.")
        return
        
    print(f"Found {len(raw_entities)} raw memories for general consolidation.")
    
    # Group raw memories by scope and owner_id
    groups = {}
    for eid, content, owner_id, scope, title in raw_entities:
        key = (owner_id, scope)
        groups.setdefault(key, []).append((eid, content, title))
        
    for (owner_id, scope), entities in groups.items():
        if len(entities) < 2:
            continue
            
        parent_ids = [e[0] for e in entities]
        contents = [e[1] for e in entities]
        titles = [e[2] for e in entities]
        
        system_prompt = (
            "You are a memory consolidation assistant. Your task is to merge multiple short-term raw markdown facts "
            "into a single, cohesive, consolidated markdown document. Remove duplicates and resolve contradictions, "
            "preferring newer or more detailed information. Keep the structure clean and maintain all crucial facts."
        )
        user_prompt = "Here are the raw memory chunks to consolidate:\n\n"
        for idx, content in enumerate(contents, 1):
            user_prompt += f"--- Chunk {idx} ---\n{content}\n\n"
            
        consolidated_content = call_llm(system_prompt, user_prompt)
        
        if not consolidated_content:
            print("Using fallback concatenation for consolidation.")
            consolidated_content = "# Consolidated Memory\n\n" + "\n\n---\n\n".join(contents)
            
        new_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        title = f"Consolidated Memory ({', '.join(titles[:2])})"
        if len(titles) > 2:
            title += " and others"
            
        with conn:
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'consolidated', ?, ?, ?)
            """, (new_id, now, now, now, owner_id, scope, json.dumps(parent_ids), title, consolidated_content))
            
            # Map tags from parents to the new entity
            placeholders = ",".join("?" for _ in parent_ids)
            tag_cursor = conn.execute(f"""
                SELECT DISTINCT tag_id FROM entity_tags WHERE entity_id IN ({placeholders})
            """, parent_ids)
            tag_ids = [row[0] for row in tag_cursor.fetchall()]
            for tag_id in tag_ids:
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (new_id, tag_id))
                
            # Archive original entities
            archive_placeholders = ",".join("?" for _ in parent_ids)
            conn.execute(f"""
                UPDATE entities SET status = 'archived', updated_at = ? WHERE id IN ({archive_placeholders})
            """, [now] + parent_ids)
            
        print(f"Successfully consolidated {len(parent_ids)} memories into new memory {new_id}")

# =====================================================================
# Main Execution
# =====================================================================

if __name__ == "__main__":
    if "--librarian" in sys.argv:
        db_path = get_db_path()
        conn = init_db(db_path)
        if not acquire_librarian_lock(conn):
            print("Librarian is already running or locked. Exiting.")
            conn.close()
            sys.exit(0)
        try:
            print(f"Starting SALTMDB Librarian on {db_path}...")
            merge_tags_heuristics(conn)
            decay_lru_memories(conn)
            consolidate_cluttered_tags(conn)
            consolidate_memories(conn)
        finally:
            release_librarian_lock(conn)
            conn.close()
            print("Librarian consolidation complete.")
    else:
        mcp.run()
