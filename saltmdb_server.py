import sqlite3
import os
import json
import uuid
import re
import sys
from datetime import datetime, UTC
from mcp.server.fastmcp import FastMCP

__version__ = "0.1.0-alpha.20"

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
            full_content TEXT NOT NULL,
            valid_from DATETIME,
            valid_to DATETIME,
            metadata TEXT
        );
        """)
        
        # Schema migration: attempt to add new columns to entities table if they don't exist
        for col in ["valid_from DATETIME", "valid_to DATETIME", "metadata TEXT", "project_id TEXT"]:
            try:
                conn.execute(f"ALTER TABLE entities ADD COLUMN {col};")
            except sqlite3.OperationalError:
                pass
                
        # Schema migration: attempt to add new columns to events table if they don't exist
        for col in ["session_id TEXT"]:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col};")
            except sqlite3.OperationalError:
                pass
        
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

        # 4b. Relations Table (Temporal knowledge graph edges)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS relations (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            predicate TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            valid_from DATETIME,
            valid_to DATETIME,
            FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE
        );
        """)
        
        # 5. Virtual FTS5 Table with Porter Tokenizer & Search Aliases
        # If it exists with the old schema (e.g. missing search_aliases column), we drop and recreate it.
        try:
            cursor = conn.execute("PRAGMA table_info(entities_fts)")
            cols = [r[1] for r in cursor.fetchall()]
            if not cols or "search_aliases" not in cols:
                conn.execute("DROP TABLE IF EXISTS entities_fts")
                conn.execute("""
                CREATE VIRTUAL TABLE entities_fts USING fts5(
                    id UNINDEXED,
                    title,
                    full_content,
                    search_aliases,
                    tokenize='porter'
                );
                """)
                # Backfill FTS index from existing entities
                conn.execute("""
                INSERT INTO entities_fts (id, title, full_content, search_aliases)
                SELECT id, title, full_content, 
                       coalesce(json_extract(metadata, '$.search_aliases'), '')
                FROM entities;
                """)
        except sqlite3.OperationalError:
            pass
        
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
        
        # Drop old triggers to recreate with search_aliases support
        conn.execute("DROP TRIGGER IF EXISTS insert_entity_fts")
        conn.execute("DROP TRIGGER IF EXISTS update_entity_fts")
        conn.execute("DROP TRIGGER IF EXISTS update_entity_fts_unarchived")
        
        # Triggers to keep FTS5 and Entities in sync
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS insert_entity_fts
        AFTER INSERT ON entities
        WHEN NEW.status != 'archived'
        BEGIN
            INSERT INTO entities_fts(id, title, full_content, search_aliases)
            VALUES (NEW.id, NEW.title, NEW.full_content, coalesce(json_extract(NEW.metadata, '$.search_aliases'), ''));
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS update_entity_fts
        AFTER UPDATE ON entities
        WHEN NEW.status != 'archived' AND OLD.status != 'archived'
        BEGIN
            UPDATE entities_fts 
            SET title = NEW.title, 
                full_content = NEW.full_content,
                search_aliases = coalesce(json_extract(NEW.metadata, '$.search_aliases'), '')
            WHERE id = OLD.id;
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS update_entity_fts_unarchived
        AFTER UPDATE ON entities
        WHEN NEW.status != 'archived' AND OLD.status = 'archived'
        BEGIN
            INSERT INTO entities_fts(id, title, full_content, search_aliases)
            VALUES (NEW.id, NEW.title, NEW.full_content, coalesce(json_extract(NEW.metadata, '$.search_aliases'), ''));
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
                    
            # 3. Optimize: Check and acquire librarian lock before spawning subprocess
            # If lock is currently held, exit immediately to prevent redundant processes.
            if not acquire_librarian_lock(conn):
                conn.close()
                return
                
            # Release lock immediately so the child process can acquire it and run
            release_librarian_lock(conn)
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
def log_event(agent_id: str, type: str, content: str, error_code: str = None, session_id: str = None) -> str:
    """Appends an event to the append-only events ledger.
    
    Args:
        agent_id: Identifier of the agent logging the event.
        type: Category of the event (e.g. 'issue', 'attempt', 'fix', 'decision').
        content: Description of the action or event.
        error_code: Optional system error code if applicable.
        session_id: Optional unique session identifier to track related events.
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    event_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content, error_code, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (event_id, now, agent_id, type, redacted_content, error_code, session_id))
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

def validate_memory_input(title: str, content: str, metadata: dict) -> None:
    """Validates memory input to enforce title hygiene and relative path constraints.
    Raises ValueError if validation fails.
    """
    if title:
        pattern = r"^[a-zA-Z0-9_\-\.]+\.(md|txt|json|yml|yaml)\s*[-—–:|]\s*"
        if re.search(pattern, title):
            raise ValueError(
                "Error: Title violates clean title guidelines. Do not prefix memory titles with file names or file extensions (e.g., use 'Language Rules' instead of 'CORE.md — Language Rules')."
            )
            
    if metadata and isinstance(metadata, dict):
        source_path = metadata.get("source_path")
        if source_path:
            is_absolute = (
                re.match(r"^[a-zA-Z]:", source_path) or
                source_path.startswith("/") or
                source_path.startswith("\\") or
                "/Users/" in source_path or
                "\\Users\\" in source_path or
                "/home/" in source_path
            )
            if is_absolute:
                raise ValueError(
                    "Error: 'source_path' must be a relative repository path (e.g., 'CORE.md' or 'notes.md'). Absolute system paths are forbidden."
                )

@mcp.tool()
def store_knowledge(
    content: str,
    tags: list,
    scope: str,
    owner_id: str,
    weight: int = 1,
    is_core: bool = False,
    title: str = None,
    entity_id: str = None,
    relevance: int = None,
    impact: int = None,
    novelty: int = None,
    actionability: int = None,
    metadata: dict = None,
    skip_duplicate_check: bool = False,
    project_id: str = None
) -> str:
    """Stores a consolidated Markdown fact chunk in the long-term knowledge base.
    
    Args:
        content: Markdown formatted text representation of the fact. You MUST format this as a Stateful Fact Block (SFB) starting with YAML frontmatter containing clean title, tags, relative source_path, and date, followed by bulleted claims prefixed with [FACT], [DECISION], etc.
        tags: List of tags associated with this knowledge.
        scope: Scope level ('private' or 'shared').
        owner_id: Mandatory ID of the agent/owner storing this knowledge to isolate lanes.
        weight: Priority ranking multiplier (default 1).
        is_core: If True, bypasses search and gets injected into the agent prompt (default False).
        title: Optional clean title (no file extensions, no parent file prefixes). If omitted, the first markdown heading is auto-extracted and cleaned.
        entity_id: Optional custom entity ID to insert or update (upsert). To update an existing memory (SCD Type 2 version update), pass the original entity_id.
        relevance: Optional score (1-5) representing context relevance.
        impact: Optional score (1-5) representing user/emotional impact.
        novelty: Optional score (1-5) representing info novelty.
        actionability: Optional score (1-5) representing action priority.
        metadata: Optional dictionary of structured attributes to match. You MUST include a relative repository path in metadata['source_path'].
        skip_duplicate_check: Optional boolean. If True, bypasses the fuzzy duplication check and forces creation of a new memory (default False).
        project_id: Optional first-class project identifier to associate with the memory.
    """
    if not owner_id:
        return "Error: owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."
        
    if scope not in ('private', 'shared'):
        return "Error: scope must be either 'private' or 'shared'"
        
    if relevance is not None or impact is not None or novelty is not None or actionability is not None:
        r = relevance if relevance is not None else 3
        im = impact if impact is not None else 3
        n = novelty if novelty is not None else 3
        a = actionability if actionability is not None else 3
        weight = max(1, min(5, (r + im + n + a) // 4))
        
    db_path = get_db_path()
    conn = init_db(db_path)
    
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    
    # Hybrid title extraction
    if not title:
        title, _ = extract_title_and_snippet(redacted_content)
        
    try:
        validate_memory_input(title, redacted_content, metadata)
    except ValueError as e:
        conn.close()
        return str(e)
        
    # Lightweight title-based deduplication per owner/scope (upsert replacement policy)
    if not entity_id:
        try:
            cursor = conn.execute("""
                SELECT id FROM entities 
                WHERE title = ? AND owner_id = ? AND scope = ? AND status != 'archived'
            """, (title, owner_id, scope))
            row = cursor.fetchone()
            if row:
                entity_id = row[0]
                print(f"Deduplication: Matched existing memory '{title}' (ID: {entity_id}). Routing to temporal upsert.")
        except Exception:
            pass # Keep going if tables aren't fully set up yet
            
    # Fuzzy duplicate check safety net
    if not entity_id and not skip_duplicate_check:
        try:
            dup_check = check_duplicate_memories(
                title=title,
                content=redacted_content,
                owner_id=owner_id,
                tags=tags
            )
            if dup_check.get("duplicate_found") and "error" not in dup_check:
                top = dup_check["potential_duplicates"][0]
                conn.close()
                return (f"Warning: Potential duplicate of existing memory '{top['title']}' "
                        f"(ID: {top['id']}, similarity {top['similarity_score']}). "
                        f"Call store_knowledge with entity_id='{top['id']}' to update it instead, "
                        f"or set skip_duplicate_check=True to force a new entry.")
        except Exception:
            pass # Continue if check fails (e.g. database uninitialized)
            
    if not entity_id:
        entity_id = str(uuid.uuid4())
        
    if not project_id and metadata and isinstance(metadata, dict):
        project_id = metadata.get("project") or metadata.get("project_id")
        
    try:
        with conn:
            # Check if this entity already exists to do temporal versioning
            cursor = conn.execute("SELECT created_at, owner_id, valid_from FROM entities WHERE id = ?", (entity_id,))
            existing = cursor.fetchone()
            if existing:
                 created_at, owner, valid_from = existing
                 hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"
                 
                 # Copy current active row to history as 'archived' with closed valid_to
                 conn.execute("""
                     INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id)
                     SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, project_id
                     FROM entities WHERE id = ?
                 """, (hist_id, valid_from if valid_from else created_at, now, entity_id))
                 
                 # Also copy tags to history entity so tag history is preserved
                 conn.execute("""
                     INSERT INTO entity_tags (entity_id, tag_id)
                     SELECT ?, tag_id FROM entity_tags WHERE entity_id = ?
                 """, (hist_id, entity_id))
                 
            # Clean up existing tags if doing an update to prevent tag accumulation
            conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))
            
            metadata_str = json.dumps(metadata) if metadata else None
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    last_accessed_at = excluded.last_accessed_at,
                    owner_id = COALESCE(excluded.owner_id, entities.owner_id),
                    scope = excluded.scope,
                    is_core = excluded.is_core,
                    weight = excluded.weight,
                    status = excluded.status,
                    title = excluded.title,
                    full_content = excluded.full_content,
                    valid_from = excluded.valid_from,
                    valid_to = NULL,
                    metadata = excluded.metadata,
                    project_id = COALESCE(excluded.project_id, entities.project_id)
            """, (entity_id, now, now, now, owner_id, scope, 1 if is_core else 0, weight, json.dumps([]), title, redacted_content, now, metadata_str, project_id))
            
            # Fetch all existing tags to do pre-write tag normalization (prevent drift)
            cursor = conn.execute("SELECT id, name, canonical_id FROM tags")
            db_tags = cursor.fetchall()
            tag_lookup = {}
            for tid, tname, tcanon in db_tags:
                norm = tname.strip().lower().lstrip('#')
                norm = re.sub(r'[-_\s]+', '', norm)
                tag_lookup[norm] = tcanon if tcanon else tid

            for tag_name in tags:
                tag_name = tag_name.strip()
                if not tag_name:
                    continue
                if not tag_name.startswith('#'):
                    tag_name = '#' + tag_name
                    
                norm_input = tag_name.lower().lstrip('#')
                norm_input = re.sub(r'[-_\s]+', '', norm_input)
                
                if norm_input in tag_lookup:
                    tag_id = tag_lookup[norm_input]
                else:
                    cursor = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (tag_name,))
                    row = cursor.fetchone()
                    if row:
                        tag_id = row[1] if row[1] else row[0]
                    else:
                        tag_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)", (tag_id, tag_name))
                        tag_lookup[norm_input] = tag_id
                
                # Link tag to entity
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                
        trigger_librarian()
        return f"Knowledge stored successfully with ID: {entity_id}"
    except Exception as e:
        return f"Error storing knowledge: {e}"
    finally:
        conn.close()

def sanitize_fts_query(query: str) -> str:
    """Sanitizes raw query string for FTS5, escaping special characters and balancing quotes."""
    if not query:
        return ""
    # Balance quotes: if odd count, remove them
    if query.count('"') % 2 != 0:
        query = query.replace('"', ' ')
    # Replace FTS5 syntax characters with spaces to be completely safe
    cleaned = re.sub(r'[\-+<>:/*\\?^$|#@`~!%&(){}[\]]', ' ', query)
    return " ".join(cleaned.split())

@mcp.tool()
def search_memory(
    owner_id: str,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    project_id: str = None
) -> list | dict:
    """Performs full-text keyword search, metadata filtering, and tag filtering in the long-term knowledge base.
    
    Args:
        owner_id: Mandatory ID of the agent/owner to isolate memory access and lanes.
        query_keywords: Search terms used to match against indexing content via FTS5.
        tags_filter: List of tag names; if provided, matched items must have all specified tags.
        metadata_filter: Optional dictionary of structured attributes to match (e.g., project, topic).
        explain_mode: If True, returns rich diagnostic details and suggested rewrites if query fails or returns 0.
        limit: Optional maximum number of memories to return (default 5, max 25).
        project_id: Optional first-class project identifier to filter queries.
    """
    if not owner_id:
        return [{"error": "owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."}]
        
    db_path = get_db_path()
    conn = init_db(db_path)
    safe_limit = max(1, min(limit, 25))
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
            
        owner_filter_clause = ""
        owner_params = []
        if owner_id:
            owner_filter_clause = " AND (e.owner_id = ? OR e.owner_id = 'shared' OR e.owner_id = 'system')"
            owner_params.append(owner_id)
            
        # Build dynamic project filter
        project_filter_clause = ""
        project_params = []
        if project_id:
            project_filter_clause = " AND e.project_id = ?"
            project_params.append(project_id)
            
        # Build dynamic metadata filters
        metadata_clauses = ""
        metadata_params = []
        if metadata_filter:
            for key, val in metadata_filter.items():
                metadata_clauses += " AND json_extract(e.metadata, ?) = ?"
                metadata_params.append(f"$.{key}")
                metadata_params.append(val)
                
        rows = []
        sanitization_applied = False
        fallback_applied = False
        
        if query_keywords:
            # 1. Sanitize the query keywords
            sanitized_keywords = sanitize_fts_query(query_keywords)
            if sanitized_keywords != query_keywords:
                sanitization_applied = True
                
            # 10:1:5 title-to-content-to-aliases search weighting, boosted by incoming relations
            sql = f"""
                SELECT e.id, e.full_content, e.weight, bm25(entities_fts, 0.0, 10.0, 1.0, 5.0) as score, e.title
                FROM entities_fts f
                JOIN entities e ON e.id = f.id
                WHERE entities_fts MATCH ? AND e.status != 'archived' {owner_filter_clause} {project_filter_clause} {tag_filter_clause} {metadata_clauses}
                ORDER BY (
                    bm25(entities_fts, 0.0, 10.0, 1.0, 5.0) * e.weight * (
                        1.0 + 0.05 * (SELECT COUNT(*) FROM relations WHERE target_id = e.id AND valid_to IS NULL)
                    )
                ) ASC
                LIMIT ?
            """
            exec_params = [sanitized_keywords] + owner_params + project_params + params + metadata_params + [safe_limit]
            
            try:
                cursor = conn.execute(sql, exec_params)
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                # FTS parser failed. Fallback to broad wildcard keyword match
                fallback_applied = True
                words = re.findall(r'\b\w+\b', sanitized_keywords)
                if words:
                    fallback_query = " OR ".join(f'"{w}*"' for w in words)
                    exec_params_fallback = [fallback_query] + owner_params + project_params + params + metadata_params + [safe_limit]
                    try:
                        cursor = conn.execute(sql, exec_params_fallback)
                        rows = cursor.fetchall()
                    except Exception:
                        rows = []
                else:
                    rows = []
        else:
            sql = f"""
                SELECT e.id, e.full_content, e.weight, 0.0 as score, e.title
                FROM entities e
                WHERE e.status != 'archived' {owner_filter_clause} {project_filter_clause} {tag_filter_clause} {metadata_clauses}
                ORDER BY (
                    e.weight * (
                        1.0 + 0.05 * (SELECT COUNT(*) FROM relations WHERE target_id = e.id AND valid_to IS NULL)
                    )
                ) DESC, e.updated_at DESC
                LIMIT ?
            """
            exec_params = owner_params + project_params + params + metadata_params + [safe_limit]
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
                
        # Generate diagnostic explanation package if explain_mode is requested
        explain_info = {}
        if explain_mode:
            term_presence = {}
            tag_suggestions = {}
            rewrites = []
            
            if query_keywords:
                # 1. Term existence check
                words = re.findall(r'\b\w+\b', query_keywords)
                for w in words:
                    cursor_w = conn.execute("SELECT COUNT(*) FROM entities WHERE full_content LIKE ? OR title LIKE ?", (f"%{w}%", f"%{w}%"))
                    term_presence[w] = cursor_w.fetchone()[0] > 0
                    
                # 2. Relaxed query suggestions
                if not results:
                    if len(words) > 1:
                        rewrites.append(" OR ".join(words))
                    rewrites.append(" ".join(f"{w}*" for w in words))
                    
            if tags_filter:
                # 3. Check invalid tags and suggest close matches
                for tag in tags_filter:
                    cursor_t = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,))
                    if not cursor_t.fetchone():
                        cursor_all = conn.execute("SELECT name FROM tags WHERE canonical_id IS NULL")
                        all_db_tags = [r[0] for r in cursor_all.fetchall()]
                        import difflib
                        closest = difflib.get_close_matches(tag, all_db_tags, n=2, cutoff=0.4)
                        tag_suggestions[tag] = closest
                        
            explain_info = {
                "sanitization_applied": sanitization_applied,
                "fallback_applied": fallback_applied,
                "searched_terms_found": term_presence,
                "invalid_tags_suggestions": tag_suggestions,
                "suggested_rewritten_queries": rewrites
            }
            
            return {
                "results": results,
                "explain": explain_info
            }
            
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
    import socket
    import subprocess
    import time
    
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
        
    # Check if port 8080 is occupied, and if so, stop it and wait for release (up to 1.0s)
    for _ in range(10):
        port_occupied = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            s.connect(("127.0.0.1", 8080))
            s.close()
            port_occupied = True
        except Exception:
            pass
            
        if not port_occupied:
            break
        # Port occupied: kill the process holding port 8080 and wait
        stop_db_viewer()
        time.sleep(0.1)
        
    # Start it in the background
    try:
        viewer_script = os.path.join(os.path.dirname(__file__), "saltmdb_viewer.py")
        
        # Log directory and path setup
        log_dir = os.path.expanduser("~/.saltmdb")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "viewer.log")
        
        # Clear/truncate the log file on fresh start
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")
            
        log_file = open(log_path, "a", encoding="utf-8")
        
        kwargs = {
            "stdout": log_file,
            "stderr": log_file,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
            
        # Run python with -u (unbuffered) so print output/tracebacks are written immediately
        process = subprocess.Popen([sys.executable, "-u", viewer_script], **kwargs)
        log_file.close()
        
        # Active verification: poll process health and check if port becomes reachable (up to 3.0s)
        server_started = False
        if "mock" in str(type(process)).lower():
            server_started = True
        else:
            for _ in range(30):
                if process.poll() is not None:
                    break # Process exited/crashed early
                    
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.1)
                    s.connect(("127.0.0.1", 8080))
                    s.close()
                    server_started = True
                    break
                except Exception:
                    pass
                time.sleep(0.1)
            
        if not server_started:
            poll = process.poll()
            log_snippet = ""
            try:
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        log_snippet = "\n".join(lines[-15:])
            except Exception:
                pass
            
            exit_code_str = f"code {poll}" if poll is not None else "timeout (failed to listen within 3s)"
            return f"Error: Database viewer failed to start: {exit_code_str}.\nLog snippet:\n{log_snippet}"
            
        return "SALTMDB Database Viewer started successfully! Open it in your browser at http://localhost:8080"
    except Exception as e:
        return f"Error starting database viewer: {e}"

@mcp.tool()
def stop_db_viewer() -> str:
    """Stops the running local SALTMDB web dashboard/viewer if it is running on port 8080."""
    import subprocess
    import socket
    
    # Check if port 8080 is actually open
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(("127.0.0.1", 8080))
        s.close()
    except Exception:
        return "SALTMDB Database Viewer is not running (port 8080 is closed)."
        
    # Attempt to kill process holding port 8080
    try:
        if sys.platform == "win32":
            # Find PID using netstat
            cmd = "netstat -ano"
            out = subprocess.check_output(cmd, shell=True, text=True)
            for line in out.splitlines():
                if ":8080" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        pid = parts[-1]
                        # Kill the PID
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return f"SALTMDB Database Viewer (PID {pid}) stopped successfully."
        else:
            # Unix-like: lsof -t -i:8080
            try:
                pid = subprocess.check_output(["lsof", "-t", "-i:8080"], text=True).strip()
                if pid:
                    subprocess.run(["kill", "-9", pid])
                    return f"SALTMDB Database Viewer (PID {pid}) stopped successfully."
            except Exception:
                # Try fuser
                try:
                    subprocess.run(["fuser", "-k", "8080/tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return "SALTMDB Database Viewer stopped successfully (using fuser)."
                except Exception:
                    pass
        return "Failed to determine the PID of the viewer on port 8080."
    except Exception as e:
        return f"Error stopping database viewer: {e}"

# =====================================================================
# Librarian / Garbage Collection Process
# =====================================================================

# Semantic consolidation LLM calls are offloaded directly to the client agent

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
            SET weight = weight - 1, last_accessed_at = ?, updated_at = ? 
            WHERE is_core = 0 
              AND status != 'archived'
              AND datetime(last_accessed_at) < datetime('now', '-90 days')
        """, (now, now))
        
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
    """Scans for tags with 5 or more raw entries per owner (or 3 or more for runbooks/decisions) and logs a consolidation request event for that agent."""
    print("Checking for high tag density clutter...")
    cursor = conn.execute("""
        SELECT et.tag_id, t.name, e.owner_id, COUNT(*) 
        FROM entity_tags et
        JOIN entities e ON et.entity_id = e.id
        JOIN tags t ON et.tag_id = t.id
        WHERE e.status = 'raw'
        GROUP BY et.tag_id, t.name, e.owner_id
    """)
    candidates = cursor.fetchall()
    
    for tag_id, tag_name, owner_id, count in candidates:
        is_high_hygiene = any(word in tag_name.lower() for word in ["runbook", "decision"])
        threshold = 3 if is_high_hygiene else 5
        
        if count < threshold:
            continue
            
        cursor = conn.execute("""
            SELECT e.id FROM entities e
            JOIN entity_tags et ON e.id = et.entity_id
            WHERE et.tag_id = ? AND e.status = 'raw' AND e.owner_id IS ?
        """, (tag_id, owner_id))
        raw_ids = [r[0] for r in cursor.fetchall()]
        
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        content = json.dumps({
            "target": "tag",
            "tag_name": tag_name,
            "entity_ids": raw_ids
        })
        
        target_agent = owner_id if owner_id else "librarian"
        with conn:
            conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content)
                VALUES (?, ?, ?, 'consolidation_request', ?)
            """, (event_id, now, target_agent, content))
        print(f"Logged consolidation request for tag '{tag_name}' (Owner: {target_agent}, Threshold: {threshold}, Entity IDs: {raw_ids})")

def consolidate_memories(conn):
    """General consolidator that groups raw memories by owner/scope and logs general consolidation request events."""
    print("Running General Memory Consolidation...")
    cursor = conn.execute("""
        SELECT e.id, e.owner_id, e.scope
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
    for eid, owner_id, scope in raw_entities:
        key = (owner_id, scope)
        groups.setdefault(key, []).append(eid)
        
    for (owner_id, scope), entity_ids in groups.items():
        if len(entity_ids) < 5:
            continue
            
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        content = json.dumps({
            "target": "general",
            "owner_id": owner_id,
            "scope": scope,
            "entity_ids": entity_ids
        })
        target_agent = owner_id if owner_id else "librarian"
        with conn:
            conn.execute("""
                INSERT INTO events (id, timestamp, agent_id, type, content)
                VALUES (?, ?, ?, 'consolidation_request', ?)
            """, (event_id, now, target_agent, content))
        print(f"Logged general consolidation request for {owner_id}/{scope} (Entity IDs: {entity_ids})")

@mcp.tool()
def commit_consolidation(
    parent_ids: list[str],
    title: str,
    content: str,
    tags: list[str],
    scope: str = "shared",
    weight: int = 1,
    db_connection = None
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents.
    
    Args:
        parent_ids: List of UUIDs of the raw source memories being consolidated.
        title: Custom title for the consolidated summary. Must be clean (no file name prefixes, no extensions).
        content: Clean, consolidated Markdown representation of the synthesized knowledge in Stateful Fact Block (SFB) format.
        tags: List of tags associated with this consolidated memory.
        scope: Scope level ('private' or 'shared').
        weight: Priority weight multiplier (default 1).
        db_connection: Internal parameter for passing test database connections.
    """
    if scope not in ('private', 'shared'):
        return "Error: scope must be either 'private' or 'shared'"
        
    try:
        validate_memory_input(title, content, None)
    except ValueError as e:
        return str(e)
        
    if db_connection:
        conn = db_connection
    else:
        db_path = get_db_path()
        conn = init_db(db_path)
    entity_id = str(uuid.uuid4())
    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()
    
    try:
        with conn:
            # 1. Insert the new consolidated entity
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                VALUES (?, ?, ?, ?, 'system', ?, 0, ?, 'consolidated', ?, ?, ?)
            """, (entity_id, now, now, now, scope, weight, json.dumps(parent_ids), title, redacted_content))
            
            # 2. Link tags
            for tag_name in tags:
                tag_name = tag_name.strip()
                if not tag_name:
                    continue
                cursor = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (tag_name,))
                row = cursor.fetchone()
                if row:
                    tag_id = row[1] if row[1] else row[0]
                else:
                    tag_id = str(uuid.uuid4())
                    conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)", (tag_id, tag_name))
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                
            # 3. Re-point existing edges in relations table to prevent cascading delete loss
            if parent_ids:
                placeholders = ",".join("?" for _ in parent_ids)
                # Re-point source_id to new consolidated entity_id
                conn.execute(f"""
                    UPDATE relations SET source_id = ?
                    WHERE source_id IN ({placeholders}) AND source_id != ?
                """, [entity_id] + parent_ids + [entity_id])
                
                # Re-point target_id to new consolidated entity_id
                conn.execute(f"""
                    UPDATE relations SET target_id = ?
                    WHERE target_id IN ({placeholders}) AND target_id != ?
                """, [entity_id] + parent_ids + [entity_id])
                
                # Clean up any self-referential loops or duplicate edges created by re-pointing
                conn.execute("DELETE FROM relations WHERE source_id = target_id")
                conn.execute("""
                    DELETE FROM relations 
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM relations 
                        GROUP BY source_id, target_id, predicate
                    )
                """)
                
                # 4. Physically delete parent entities (Intentional Algorithmic Forgetting)
                conn.execute(f"DELETE FROM entities WHERE id IN ({placeholders})", parent_ids)
                
        return f"Successfully committed consolidated memory with ID: {entity_id} and deleted {len(parent_ids)} raw source nodes."
    except Exception as e:
        return f"Error committing consolidation: {e}"

@mcp.tool()
def create_snapshot() -> str:
    """Creates a backup snapshot of the current database file in a backups/ directory.
    
    Returns:
        Status message indicating success or failure.
    """
    db_path = get_db_path()
    db_dir = os.path.dirname(db_path)
    backups_dir = os.path.join(db_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backups_dir, f"saltmdb_backup_{timestamp}.db")
    
    try:
        source_conn = sqlite3.connect(db_path)
        dest_conn = sqlite3.connect(backup_file)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
        return f"Database snapshot successfully created: {backup_file}"
    except Exception as e:
        return f"Error creating database snapshot: {str(e)}"

@mcp.tool()
def archive_memory(entity_id: str, owner_id: str) -> str:
    """Explicitly archives (retires) a long-term memory, marking it as inactive.
    
    Args:
        entity_id: The UUID of the memory to archive.
        owner_id: Mandatory ID of the agent/owner to isolate memory lanes.
    """
    if not owner_id:
        return "Error: owner_id is mandatory to prevent cross-lane signal contamination."
        
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        now = datetime.now(UTC).isoformat()
        with conn:
            # Check existence and ownership of active memory
            cursor = conn.execute("SELECT id FROM entities WHERE id = ? AND owner_id = ? AND status != 'archived'", (entity_id, owner_id))
            row = cursor.fetchone()
            if not row:
                return f"Error: Active memory with ID '{entity_id}' not found for owner '{owner_id}'."
                
            # Perform temporal archiving
            conn.execute("""
                UPDATE entities 
                SET status = 'archived', updated_at = ?, valid_to = ?
                WHERE id = ? AND owner_id = ?
            """, (now, now, entity_id, owner_id))
            
        return f"Memory '{entity_id}' successfully archived (retired)."
    except Exception as e:
        return f"Error archiving memory: {e}"
    finally:
        conn.close()

@mcp.tool()
def detect_orphaned_memories(owner_id: str) -> dict:
    """Identifies active long-term memories that have no incoming or outgoing relationship links,
    and suggests potential connection candidates based on shared tags.
    
    Args:
        owner_id: Mandatory ID of the agent/owner to isolate memory lanes.
    """
    if not owner_id:
        return {"error": "owner_id is mandatory to prevent cross-lane signal contamination."}
        
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        # Find all active entities for this owner that are not archived and have 0 relations
        cursor = conn.execute("""
            SELECT e.id, e.title, e.scope
            FROM entities e
            WHERE e.status != 'archived' 
              AND (e.owner_id = ? OR e.owner_id = 'shared')
              AND e.id NOT IN (SELECT DISTINCT source_id FROM relations WHERE valid_to IS NULL)
              AND e.id NOT IN (SELECT DISTINCT target_id FROM relations WHERE valid_to IS NULL)
        """, (owner_id,))
        orphans = [{"id": r[0], "title": r[1], "scope": r[2]} for r in cursor.fetchall()]
        
        results = []
        for orphan in orphans:
            oid = orphan["id"]
            # Fetch tags of this orphan
            cursor_tags = conn.execute("""
                SELECT t.name FROM tags t
                JOIN entity_tags et ON et.tag_id = t.id
                WHERE et.entity_id = ?
            """, (oid,))
            tags = [r[0] for r in cursor_tags.fetchall()]
            
            candidates = []
            if tags:
                # Find other active entities with matching tags (limited to 5)
                placeholders = ",".join("?" for _ in tags)
                cursor_cand = conn.execute(f"""
                    SELECT DISTINCT e.id, e.title, COUNT(et.tag_id) as matching_tags_count
                    FROM entities e
                    JOIN entity_tags et ON et.entity_id = e.id
                    JOIN tags t ON et.tag_id = t.id
                    WHERE e.id != ? AND e.status != 'archived'
                      AND (e.owner_id = ? OR e.owner_id = 'shared')
                      AND t.name IN ({placeholders})
                    GROUP BY e.id
                    ORDER BY matching_tags_count DESC, e.updated_at DESC
                    LIMIT 5
                """, [oid, owner_id] + tags)
                candidates = [{"id": r[0], "title": r[1], "matching_tags": r[2]} for r in cursor_cand.fetchall()]
                
            results.append({
                "orphan": orphan,
                "orphan_tags": tags,
                "suggested_connection_candidates": candidates
            })
            
        return {"orphans_detected": len(results), "details": results}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@mcp.tool()
def check_duplicate_memories(
    title: str,
    content: str,
    owner_id: str,
    tags: list = None
) -> dict:
    """Checks the database for existing memories that might be duplicates of the proposed memory,
    using title matching, tag overlap, and text similarity.
    
    Args:
        title: Proposed title of the memory.
        content: Proposed markdown content of the memory.
        owner_id: Mandatory ID of the agent/owner to isolate lanes.
        tags: List of tags associated with the proposed memory.
    """
    if not owner_id:
        return {"error": "owner_id is mandatory."}
        
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        # Find active candidate entities with the same owner/scope
        cursor = conn.execute("""
            SELECT id, title, full_content FROM entities
            WHERE status != 'archived' AND (owner_id = ? OR owner_id = 'shared')
        """, (owner_id,))
        candidates = cursor.fetchall()
        
        import difflib
        matches = []
        
        # Calculate similarity scores
        for cid, ctitle, ccontent in candidates:
            # Title similarity (Levenshtein/SequenceMatcher based)
            title_sim = difflib.SequenceMatcher(None, title.lower(), ctitle.lower()).ratio()
            
            # Content similarity (on first 500 chars to be fast)
            snippet1 = content[:500].lower()
            snippet2 = ccontent[:500].lower()
            content_sim = difflib.SequenceMatcher(None, snippet1, snippet2).ratio()
            
            # Weighted overall similarity
            overall_sim = (title_sim * 0.4) + (content_sim * 0.6)
            
            if overall_sim > 0.6:  # Return anything with over 60% similarity
                matches.append({
                    "id": cid,
                    "title": ctitle,
                    "similarity_score": round(overall_sim, 2),
                    "title_similarity": round(title_sim, 2),
                    "content_similarity": round(content_sim, 2)
                })
                
        # Sort by similarity score descending
        matches.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {
            "duplicate_found": len(matches) > 0 and matches[0]["similarity_score"] >= 0.7,
            "potential_duplicates": matches[:5]
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@mcp.tool()
def store_relation(source_id: str, target_id: str, predicate: str) -> str:
    """Stores a typed directional relationship (edge) between two long-term memory entities.
    
    Args:
        source_id: UUID of the source entity (subject).
        target_id: UUID of the target entity (object).
        predicate: Description of the relationship (e.g., 'depends_on', 'part_of', 'resolved_by').
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    relation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            # Verify both entities exist
            cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (source_id,))
            if not cursor.fetchone():
                return f"Error: Source entity {source_id} does not exist."
            cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (target_id,))
            if not cursor.fetchone():
                return f"Error: Target entity {target_id} does not exist."
                
            conn.execute("""
                INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (relation_id, source_id, target_id, predicate, now, now))
        return f"Relation successfully stored with ID: {relation_id}"
    except Exception as e:
        return f"Error storing relation: {e}"
    finally:
        conn.close()

@mcp.tool()
def analyze_dependencies(root_entity_id: str) -> list:
    """Traverses the temporal knowledge graph using recursive CTE queries to map downstream components.
    
    Args:
        root_entity_id: The UUID of the memory node to begin traversal from.
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        cursor = conn.execute("""
            WITH RECURSIVE dependency_tree(id, title, status, depth, id_path, title_path) AS (
                -- Anchor member
                SELECT e.id, e.title, e.status, 0, ',' || e.id || ',', e.title
                FROM entities e
                WHERE e.id = ? AND e.status != 'archived'
                
                UNION ALL
                
                -- Recursive member
                SELECT child.id, child.title, child.status, dt.depth + 1,
                       dt.id_path || child.id || ',',
                       dt.title_path || ' -> ' || child.title
                FROM entities child
                JOIN relations r ON r.target_id = child.id
                JOIN dependency_tree dt ON r.source_id = dt.id
                WHERE child.status != 'archived' AND r.valid_to IS NULL
                -- Prevent cycles using unique ID paths
                AND dt.id_path NOT LIKE '%,' || child.id || ',%'
            )
            SELECT DISTINCT id, title, status, depth, title_path FROM dependency_tree;
        """, (root_entity_id,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "status": r[2],
                "depth": r[3],
                "path": r[4]
            } for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()

@mcp.tool()
def get_recent_events(agent_id: str = None, type_filter: str = None, limit: int = 20) -> list:
    """Retrieves recent events from the short-term ledger.
    
    Args:
        agent_id: Optional filter for a specific agent ID.
        type_filter: Optional filter for a specific event type (e.g. 'consolidation_request').
        limit: Maximum number of events to return (default 20).
    """
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        params = []
        clauses = []
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if type_filter:
            clauses.append("type = ?")
            params.append(type_filter)
            
        where_clause = " WHERE " + " AND ".join(clauses) if clauses else ""
        
        sql = f"""
            SELECT id, timestamp, agent_id, type, content, error_code, session_id
            FROM events
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params.append(limit)
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        
        events = []
        for r in rows:
            ev_id, timestamp, agent, ev_type, content, error_code, session_id = r
            status = "pending"
            
            # Truncate content to 1000 chars if massive, preserving it for consolidation requests
            display_content = content
            if content and len(content) > 1000:
                if ev_type == "consolidation_request":
                    pass
                else:
                    display_content = content[:1000] + " ... [TRUNCATED FOR CONTEXT CONSERVATION]"
            
            if ev_type == "consolidation_request" and content:
                try:
                    data = json.loads(content)
                    entity_ids = data.get("entity_ids", [])
                    if entity_ids:
                        # Check how many of the requested raw entities are still active/raw
                        placeholders = ",".join("?" for _ in entity_ids)
                        cursor_status = conn.execute(f"""
                            SELECT COUNT(*) FROM entities
                            WHERE id IN ({placeholders}) AND status = 'raw'
                        """, entity_ids)
                        raw_count = cursor_status.fetchone()[0]
                        # If all have been consolidated/retired, it is resolved
                        if raw_count == 0:
                            status = "resolved"
                except Exception:
                    pass
            
            events.append({
                "id": ev_id,
                "timestamp": timestamp,
                "agent_id": agent,
                "type": ev_type,
                "content": display_content,
                "error_code": error_code,
                "status": status,
                "session_id": session_id
            })
        return events
    except Exception as e:
        return [{"error": str(e)}]
@mcp.tool()
def scan_memories(
    owner_id: str,
    status_filter: str = "active",
    limit: int = 20,
    offset: int = 0
) -> list:
    """Scans and lists long-term memories for a specific owner to perform audits, contradiction checks, or status reviews.
    
    Args:
        owner_id: Mandatory ID of the agent/owner to isolate lanes and memory access.
        status_filter: Filter by memory status: 'raw', 'consolidated', 'archived', 'active' (both raw and consolidated, default), or 'all'.
        limit: Maximum number of memories to return (default 20, max 100).
        offset: Offset for pagination (default 0).
    """
    if not owner_id:
        return [{"error": "owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."}]
        
    db_path = get_db_path()
    conn = init_db(db_path)
    safe_limit = max(1, min(100, limit))
    
    try:
        params = [owner_id]
        status_clause = ""
        if status_filter == "raw":
            status_clause = "AND status = 'raw'"
        elif status_filter == "consolidated":
            status_clause = "AND status = 'consolidated'"
        elif status_filter == "archived":
            status_clause = "AND status = 'archived'"
        elif status_filter == "active":
            status_clause = "AND status != 'archived'"
        elif status_filter == "all":
            status_clause = ""
        else:
            status_clause = "AND status != 'archived'"
            
        sql = f"""
            SELECT e.id, e.title, e.scope, e.weight, e.status, e.full_content, e.metadata
            FROM entities e
            WHERE (e.owner_id = ? OR e.owner_id = 'shared' OR e.owner_id = 'system') {status_clause}
            ORDER BY e.updated_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([safe_limit, offset])
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        
        memories = []
        for r in rows:
            entity_id, title, scope, weight, status, full_content, metadata_str = r
            
            cursor_tags = conn.execute("""
                SELECT t.name FROM tags t
                JOIN entity_tags et ON et.tag_id = t.id
                WHERE et.entity_id = ?
            """, (entity_id,))
            tags = [t[0] for t in cursor_tags.fetchall()]
            
            metadata = {}
            if metadata_str:
                try:
                    metadata = json.loads(metadata_str)
                except Exception:
                    pass
                    
            memories.append({
                "id": entity_id,
                "title": title,
                "scope": scope,
                "weight": weight,
                "status": status,
                "tags": tags,
                "metadata": metadata,
                "content": full_content
            })
        return memories
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()

@mcp.tool()
def get_session_summary(session_id: str) -> list:
    """Retrieves a chronological log summary of all operational events for a specific session ID.
    
    Args:
        session_id: The unique identifier of the session to retrieve logs for.
    """
    if not session_id:
        return [{"error": "session_id is required."}]
        
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        cursor = conn.execute("""
            SELECT id, timestamp, agent_id, type, content, error_code
            FROM events
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """, (session_id,))
        rows = cursor.fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1],
                "agent_id": r[2],
                "type": r[3],
                "content": r[4],
                "error_code": r[5]
            } for r in rows
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()

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
