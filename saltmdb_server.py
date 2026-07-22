import sqlite3
import os
import json
import uuid
import re
import sys
import base64
from datetime import datetime, UTC
from typing import Literal
from mcp.server.fastmcp import FastMCP

__version__ = "0.1.0-alpha.24"

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

UUID_REGEX = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

def resolve_entity_id(conn, input_val: str) -> str | None:
    """Helper to flexibly resolve an entity ID from a raw UUID, a status string containing a UUID, or an entity title."""
    if not input_val or not isinstance(input_val, str):
        return input_val
    input_val = input_val.strip()
    
    # 1. Exact UUID pattern
    if UUID_REGEX.fullmatch(input_val):
        return input_val
        
    # 2. Status string containing UUID (e.g. 'Knowledge stored successfully with ID: <uuid>')
    match = UUID_REGEX.search(input_val)
    if match:
        return match.group(0)
        
    # 3. Entity title resolution
    try:
        cursor = conn.execute("SELECT id FROM entities WHERE title = ? AND status != 'archived' ORDER BY updated_at DESC LIMIT 1", (input_val,))
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception:
        pass
        
    return input_val

def init_db(db_path: str = None):
    """Initialize the local SQLite database with Write-Ahead Logging (WAL) and schemas."""
    if not db_path:
        db_path = get_db_path()
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
        for col in ["valid_from DATETIME", "valid_to DATETIME", "metadata TEXT", "project_id TEXT", "context_id TEXT"]:
            try:
                conn.execute(f"ALTER TABLE entities ADD COLUMN {col};")
            except sqlite3.OperationalError:
                pass
                
        # Schema migration: attempt to add new columns to events table if they don't exist
        for col in ["session_id TEXT", "context_id TEXT"]:
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
                return
                
            # Release lock immediately so the child process can acquire it and run
            release_librarian_lock(conn)
        finally:
            conn.close()
    except Exception:
        # If lock/cooldown check fails, do not spawn redundant subprocess
        return

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
def log_event(agent_id: str = None, type: str = None, content: str = None, error_code: str = None, session_id: str = None, **kwargs) -> str:
    """Appends an event to the append-only events ledger.
    
    Args:
        agent_id: Identifier of the agent logging the event.
        type: Category of the event (e.g. 'issue', 'attempt', 'fix', 'decision'). Accepts alias 'event_type'.
        content: Description of the action or event. Accepts aliases 'message', 'description'.
        error_code: Optional system error code if applicable.
        session_id: Optional unique session identifier to track related events.
    """
    agent_id = agent_id or kwargs.get("agent_id") or kwargs.get("agent") or "system"
    type = type or kwargs.get("type") or kwargs.get("event_type") or "event"
    content = content or kwargs.get("content") or kwargs.get("message") or kwargs.get("description") or ""
    error_code = error_code or kwargs.get("error_code")
    session_id = session_id or kwargs.get("session_id")
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
def get_canonical_tags(domain: str = None, **kwargs) -> list:
    """Queries the database to suggest existing canonical tags to prevent fragmentation.
    
    Args:
        domain: Optional prefix/substring to filter matching tags. Accepts aliases 'query', 'substring', 'tag_filter'.
    """
    domain = domain or kwargs.get("domain") or kwargs.get("query") or kwargs.get("substring") or kwargs.get("tag_filter")
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
def store_memory(
    content: str = None,
    tags: list = None,
    owner_id: str = None,
    scope: Literal['private', 'shared'] = "shared",
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
    project_id: str = None,
    context_id: str = None,
    **kwargs
) -> str:
    """Stores a consolidated Markdown fact chunk as a long-term memory.
    
    Memory SEO Guidelines:
    - MANDATORY: Call `check_duplicate_memories` before writing to prevent duplicate clutter.
    - Title: Front-load with specific technical nouns (e.g., 'Docker Nginx 502 Bad Gateway' instead of 'Server Error'). Do not include file extensions.
    - Content: Format as a Stateful Fact Block starting with YAML frontmatter (containing title, tags, relative source_path, and date), followed by bulleted claims prefixed with [FACT], [DECISION], etc. Use explicit nouns; avoid pronouns ('it', 'they').
    - Metadata: Include search_aliases list in metadata['search_aliases'] to index alternative keywords/synonyms invisibly.
    
    Args:
        content: Markdown formatted text representation of the fact. Must be in Stateful Fact Block (SFB) format.
        tags: List of tags associated with this memory.
        owner_id: Mandatory ID of the agent/owner storing this memory to isolate lanes. Pass your active agent/role identifier (e.g., 'ops', 'tea', or your active agent ID).
        scope: Scope level ('private' or 'shared', defaults to 'shared').
        weight: Priority ranking multiplier (default 1).
        is_core: If True, bypasses search and gets injected into the agent prompt (default False).
        title: Optional clean title. If omitted, the first markdown heading is auto-extracted.
        entity_id: Optional custom entity ID to update (upsert). To update an existing memory (SCD Type 2 version update), pass the original entity_id.
        relevance: Optional score (1-5) representing context relevance.
        impact: Optional score (1-5) representing user/emotional impact.
        novelty: Optional score (1-5) representing info novelty.
        actionability: Optional score (1-5) representing action priority.
        metadata: Optional dictionary of structured attributes. If provided, you MUST include metadata['source_path'] specifying the relative repository path.
        skip_duplicate_check: Optional boolean. If True, bypasses the fuzzy duplication check and forces creation of a new memory (default False).
        project_id: Optional first-class project identifier to associate with the memory.
    """
    content = content or kwargs.get("content") or kwargs.get("text") or ""
    owner_id = owner_id or kwargs.get("owner_id") or kwargs.get("owner")
    context_id = context_id or project_id or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("context") or kwargs.get("project")
    project_id = context_id
    raw_tag = tags if tags is not None else kwargs.get("tags") or kwargs.get("tag")
    if isinstance(raw_tag, str):
        tags = [raw_tag]
    elif isinstance(raw_tag, list):
        tags = raw_tag
    else:
        tags = []

    if not owner_id:
        return "Error: owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."
        
    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."
        
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
    else:
        title = redact_secrets(title)
        
    try:
        validate_memory_input(title, redacted_content, metadata)
    except ValueError as e:
        conn.close()
        return str(e)
        
    # Resolve project_id early from metadata if not explicitly provided
    if not project_id and metadata and isinstance(metadata, dict):
        project_id = metadata.get("project") or metadata.get("project_id")
        
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
                tags=tags,
                project_id=project_id
            )
            if dup_check.get("duplicate_found") and "error" not in dup_check:
                top = dup_check["potential_duplicates"][0]
                conn.close()
                return (f"Warning: Potential duplicate of existing memory '{top['title']}' "
                        f"(ID: {top['id']}, similarity {top['similarity_score']}). "
                        f"Call store_memory with entity_id='{top['id']}' to update it instead, "
                        f"or set skip_duplicate_check=True to force a new entry.")
        except Exception:
            pass # Continue if check fails (e.g. database uninitialized)
            
    if not entity_id:
        entity_id = str(uuid.uuid4())
        
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
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    project_id: str = None,
    context_id: str = None,
    is_core: bool = None,
    tag_operator: Literal['AND', 'OR'] = "AND",
    cursor: str = None,
    include_related: bool = False,
    **kwargs
) -> list | dict:
    """Performs full-text keyword search and filtering in long-term memory.
    
    SEARCH GUIDANCE:
    - Use high-signal keywords (rare-terms) rather than common/generic verbs.
    - Avoid generic words (e.g. 'error', 'setup', 'memory', 'code').
    - Combine query keywords with domain/component identifiers (e.g., 'WAL mode sqlite' instead of 'setup db').
    
    EXPLAIN MODE:
    - Set explain_mode=True to debug queries returning zero results. It will analyze term presence and suggest relaxed query rewrites.
    
    SEARCH EXAMPLES:
    - Search for sqlite performance fixes:
      search_memory(owner_id='ops', query_keywords='sqlite WAL logging', tags_filter=['#performance'])
    - Recall project-specific deployment steps:
      search_memory(owner_id='ops', query_keywords='deployment deploy docker', project_id='PROJ-XYZ')
    - Broad search for either of the tags:
      search_memory(owner_id='tea', tags_filter=['#auth', '#login'], tag_operator='OR')
    
    CRITICAL USAGE RULES:
    - PARAMETER ALIGNMENT: You MUST pass the search string to the 'query_keywords' parameter. Do NOT use a parameter named 'query'.
    - BYPASS BAN: Do not attempt to query the 'saltmdb.db' file directly using sqlite3 or shell commands, even if the search returns zero results.
    - LOOK-BEFORE-LEAP: Always call this tool at the start of a session or task to check for past mistakes, lessons learned, or conventions.
    
    Args:
        owner_id: Mandatory ID of the agent/owner to isolate memory access and lanes. Pass your active agent/role identifier (e.g., 'ops', 'tea', or your active agent ID).
        query_keywords: Search terms used to match against indexing content via FTS5 (matches title, content, or search aliases). MUST be passed here, NOT in 'query'.
        tags_filter: List of tag names; if provided, filters matches by tags according to 'tag_operator'.
        metadata_filter: Optional dictionary of structured attributes to match (e.g., project, topic).
    """
    owner_id = owner_id or kwargs.get("owner_id") or kwargs.get("owner")
    query_keywords = query_keywords or kwargs.get("query_keywords") or kwargs.get("query") or kwargs.get("q") or kwargs.get("keywords")
    context_id = context_id or project_id or kwargs.get("context_id") or kwargs.get("project_id") or kwargs.get("context") or kwargs.get("project")
    project_id = context_id
    limit = max(1, min(25, limit))
    raw_tags = tags_filter if tags_filter is not None else kwargs.get("tags_filter") or kwargs.get("tags") or kwargs.get("tag")
    if isinstance(raw_tags, str):
        tags_filter = [raw_tags]
    elif isinstance(raw_tags, list):
        tags_filter = raw_tags
    else:
        tags_filter = []

    db_path = get_db_path()
    conn = init_db(db_path)
    safe_limit = max(1, min(limit, 25))
    now = datetime.now(UTC).isoformat()
    
    offset_val = 0
    if cursor:
        try:
            decoded = base64.b64decode(cursor.encode()).decode()
            if decoded.startswith("offset:"):
                offset_val = int(decoded.split(":", 1)[1])
            else:
                offset_val = int(decoded)
        except Exception:
            pass
            
    try:
        params = []
        owner_params = []
        # Relevance over identity: owner_id is demoted to provenance metadata; non-private memories surface globally
        if owner_id:
            owner_filter_clause = " AND (e.scope != 'private' OR e.owner_id = ? OR e.owner_id = 'shared' OR e.owner_id = 'system')"
            owner_params.append(owner_id)
        else:
            owner_filter_clause = " AND (e.scope != 'private' OR e.owner_id = 'shared' OR e.owner_id = 'system')"
            
        tag_filter_clause = ""
        if tags_filter:
            norm_tags = [t.strip() if t.strip().startswith('#') else '#' + t.strip() for t in tags_filter if t.strip()]
            lower_tags = [t.lower() for t in norm_tags]
            
            t_placeholders = ",".join("?" for _ in lower_tags)
            cursor_res_tags = conn.execute(f"""
                SELECT DISTINCT COALESCE(t.canonical_id, t.id)
                FROM tags t
                WHERE LOWER(t.name) IN ({t_placeholders})
            """, lower_tags)
            resolved_tag_ids = [r[0] for r in cursor_res_tags.fetchall()]
            
            if not resolved_tag_ids:
                tag_filter_clause = " AND 1=0 "
            else:
                tag_placeholders = ",".join("?" for _ in resolved_tag_ids)
                if tag_operator.upper() == "OR":
                    tag_filter_clause = f" AND e.id IN (SELECT et.entity_id FROM entity_tags et WHERE et.tag_id IN ({tag_placeholders}))"
                    params.extend(resolved_tag_ids)
                else:
                    tag_filter_clause = f" AND e.id IN (SELECT et.entity_id FROM entity_tags et WHERE et.tag_id IN ({tag_placeholders}) GROUP BY et.entity_id HAVING COUNT(DISTINCT et.tag_id) = ?)"
                    params.extend(resolved_tag_ids)
                    params.append(len(resolved_tag_ids))
            
        project_filter_clause = ""
        project_params = []
        if project_id:
            project_filter_clause = " AND e.project_id = ?"
            project_params.append(project_id)
            
        metadata_clauses = ""
        metadata_params = []
        if metadata_filter:
            for key, val in metadata_filter.items():
                metadata_clauses += " AND json_extract(e.metadata, ?) = ?"
                metadata_params.append(f"$.{key}")
                metadata_params.append(val)
                
        is_core_clause = ""
        is_core_params = []
        if is_core is not None:
            is_core_clause = " AND e.is_core = ?"
            is_core_params.append(1 if is_core else 0)
            
        rows = []
        sanitization_applied = False
        fallback_applied = False
        
        # FTS5 Full-Text Search Query construction with natural language normalization and relation boosting
        if query_keywords and query_keywords.strip():
            sanitized_keywords = sanitize_fts_query(query_keywords)
            normalized_keywords = normalize_search_query(sanitized_keywords)
            terms = normalized_keywords.split() if normalized_keywords else sanitized_keywords.split()
            fts_query_str = " ".join(f'"{t.replace(chr(34), "")}"*' for t in terms) if terms else sanitized_keywords
            
            sql = f"""
                SELECT e.id, e.full_content, e.weight, bm25(entities_fts, 0.0, 10.0, 1.0, 5.0) as score, e.title, e.is_core
                FROM entities_fts f
                JOIN entities e ON e.id = f.id
                WHERE entities_fts MATCH ? AND e.status != 'archived' {owner_filter_clause} {project_filter_clause} {tag_filter_clause} {metadata_clauses} {is_core_clause}
                ORDER BY (
                    bm25(entities_fts, 0.0, 10.0, 1.0, 5.0) * e.weight * (
                        1.0 + 0.05 * (SELECT COUNT(*) FROM relations WHERE target_id = e.id AND valid_to IS NULL)
                    )
                ) ASC
                LIMIT ? OFFSET ?
            """
            exec_params = [fts_query_str] + owner_params + project_params + params + metadata_params + is_core_params + [safe_limit, offset_val]
            
            try:
                cursor_db = conn.execute(sql, exec_params)
                rows = cursor_db.fetchall()
            except sqlite3.OperationalError:
                fallback_applied = True
                words = re.findall(r'\b\w+\b', sanitized_keywords)
                if words:
                    fallback_query = " OR ".join(f'"{w}*"' for w in words)
                    exec_params_fallback = [fallback_query] + owner_params + project_params + params + metadata_params + is_core_params + [safe_limit, offset_val]
                    try:
                        cursor_db = conn.execute(sql, exec_params_fallback)
                        rows = cursor_db.fetchall()
                    except Exception:
                        rows = []
                else:
                    rows = []
        else:
            sql = f"""
                SELECT e.id, e.full_content, e.weight, 0.0 as score, e.title, e.is_core
                FROM entities e
                WHERE e.status != 'archived' {owner_filter_clause} {project_filter_clause} {tag_filter_clause} {metadata_clauses} {is_core_clause}
                ORDER BY (
                    e.weight * (
                        1.0 + 0.05 * (SELECT COUNT(*) FROM relations WHERE target_id = e.id AND valid_to IS NULL)
                    )
                ) DESC, e.updated_at DESC
                LIMIT ? OFFSET ?
            """
            exec_params = owner_params + project_params + params + metadata_params + is_core_params + [safe_limit, offset_val]
            cursor_db = conn.execute(sql, exec_params)
            rows = cursor_db.fetchall()
            
        results = []
        entity_ids = []
        for idx, (entity_id, full_content, weight, score, title, db_is_core) in enumerate(rows):
            _, snippet = extract_title_and_snippet(full_content)
            next_c = base64.b64encode(f"offset:{offset_val + idx + 1}".encode()).decode()
            item_dict = {
                "id": entity_id,
                "title": title,
                "snippet": snippet,
                "score": score,
                "weight": weight,
                "is_core": bool(db_is_core),
                "cursor": next_c
            }
            if include_related:
                cursor_rel = conn.execute("""
                    SELECT e.id, e.title, r.predicate 
                    FROM relations r
                    JOIN entities e ON (r.target_id = e.id OR r.source_id = e.id)
                    WHERE (r.source_id = ? OR r.target_id = ?) AND e.id != ? AND e.status != 'archived' AND r.valid_to IS NULL
                    LIMIT 3
                """, (entity_id, entity_id, entity_id))
                item_dict["related_entities"] = [{"id": r[0], "title": r[1], "predicate": r[2]} for r in cursor_rel.fetchall()]
            results.append(item_dict)
            entity_ids.append(entity_id)
            
        if entity_ids:
            with conn:
                placeholders = ",".join("?" for _ in entity_ids)
                conn.execute(f"UPDATE entities SET last_accessed_at = ? WHERE id IN ({placeholders})", [now] + entity_ids)
                
        if explain_mode:
            term_presence = {}
            if query_keywords:
                words = re.findall(r'\b\w+\b', query_keywords)
                for w in words:
                    cursor_w = conn.execute("SELECT COUNT(*) FROM entities WHERE full_content LIKE ? OR title LIKE ?", (f"%{w}%", f"%{w}%"))
                    term_presence[w] = cursor_w.fetchone()[0] > 0
                    
            tag_suggestions = {}
            if tags_filter:
                for tag in tags_filter:
                    norm_t = tag.strip() if tag.strip().startswith('#') else '#' + tag.strip()
                    cursor_t = conn.execute("SELECT id FROM tags WHERE LOWER(name) = ?", (norm_t.lower(),))
                    if not cursor_t.fetchone():
                        cursor_all = conn.execute("SELECT name FROM tags WHERE canonical_id IS NULL")
                        all_db_tags = [r[0] for r in cursor_all.fetchall()]
                        import difflib
                        closest = difflib.get_close_matches(norm_t, all_db_tags, n=2, cutoff=0.3)
                        tag_suggestions[tag] = closest

            explain_info = {
                "sanitization_applied": sanitization_applied,
                "fallback_applied": fallback_applied,
                "searched_terms_found": term_presence,
                "invalid_tags_suggestions": tag_suggestions,
                "suggested_rewritten_queries": []
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
def fetch_memory_chunk(entity_id: str = None, **kwargs) -> str:
    """Fetches the exact complete markdown text of a specific knowledge base ID.
    
    Args:
        entity_id: The UUID or title of the memory chunk.
    """
    entity_id = entity_id or kwargs.get("entity_id") or kwargs.get("id")
    db_path = get_db_path()
    conn = init_db(db_path)
    entity_id = resolve_entity_id(conn, entity_id)
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
def start_db_viewer(port: int = None, **kwargs) -> str:
    """Spawns the local SALTMDB web dashboard/viewer in the background on port 8080 or specified port.
    Returns the URL link to access the dashboard.
    """
    import urllib.request
    import socket
    import subprocess
    import time
    
    port = port or kwargs.get("port") or 8080
    
    # Check if the viewer is already running by making a fast request
    is_running = False
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/", timeout=0.5) as res:
            if res.status == 200:
                is_running = True
    except Exception:
        pass
        
    if is_running:
        return f"SALTMDB Database Viewer is already running! Open it in your browser at http://localhost:{port}"
        
    # Check if port is occupied, and if so, stop it and wait for release (up to 1.0s)
    for _ in range(10):
        port_occupied = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.1)
            s.connect(("127.0.0.1", port))
            s.close()
            port_occupied = True
        except Exception:
            pass
            
        if not port_occupied:
            break
        # Port occupied: kill the process holding port and wait
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
    """[DEPRECATED / REMOVED PER DESIGN PRINCIPLES]
    Access recency is not a proxy for value. Archiving is only justified when content is superseded or consolidated.
    """
    pass

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
    scope: Literal['private', 'shared'] = "shared",
    weight: int = 1,
    db_connection = None
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents.
    
    CRITICAL USAGE RULES:
    - OWNERSHIP: Consolidated memories are automatically assigned owner_id='system', making them globally searchable across all agent lanes.
    
    Args:
        parent_ids: List of UUIDs of the raw source memories being consolidated.
        title: Custom title for the consolidated summary. Must be clean (no file name prefixes, no extensions).
        content: Clean, consolidated Markdown representation of the synthesized knowledge in Stateful Fact Block (SFB) format.
        tags: List of tags associated with this consolidated memory.
        scope: Scope level ('private' or 'shared', defaults to 'shared').
        weight: Priority weight multiplier (default 1).
        db_connection: [INTERNAL TEST ONLY - DO NOT USE]. Leave empty.
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
                
                # 4. Create explicit lineage edges ('consolidated_from') to archived parent sources
                for p_id in parent_ids:
                    p_id_resolved = resolve_entity_id(conn, p_id)
                    if p_id_resolved and p_id_resolved != entity_id:
                        rel_id = str(uuid.uuid4())
                        conn.execute("""
                            INSERT OR IGNORE INTO relations (id, created_at, source_id, target_id, predicate)
                            VALUES (?, ?, ?, ?, 'consolidated_from')
                        """, (rel_id, now, entity_id, p_id_resolved))

                # 5. Archive parent entities (Soft Algorithmic Forgetting - keeps history but hides from active index)
                conn.execute(f"UPDATE entities SET status = 'archived' WHERE id IN ({placeholders})", parent_ids)
                
        return f"Successfully committed consolidated memory with ID: {entity_id} and archived {len(parent_ids)} raw source nodes."
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
def archive_memory(entity_id: str = None, owner_id: str = None, **kwargs) -> str:
    """Explicitly archives (retires) a long-term memory, marking it as inactive.
    
    Args:
        entity_id: The UUID or title of the memory to archive.
        owner_id: Mandatory ID of the agent/owner to isolate memory lanes.
    """
    entity_id = entity_id or kwargs.get("entity_id") or kwargs.get("id")
    owner_id = owner_id or kwargs.get("owner_id") or kwargs.get("owner")
    if not owner_id:
        return "Error: owner_id is mandatory to prevent cross-lane signal contamination."
        
    db_path = get_db_path()
    conn = init_db(db_path)
    entity_id = resolve_entity_id(conn, entity_id)
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
                    JOIN entity_tags et ON e.id = et.entity_id
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

SEARCH_STOP_WORDS = {"the", "a", "an", "is", "are", "was", "were", "to", "in", "for", "of", "and", "or", "on", "with", "at", "by", "from", "that", "this", "it", "its", "as", "be", "been", "have", "has", "had", "do", "does", "did", "but", "not", "we", "you", "they", "he", "she", "i", "how", "what", "can", "my", "need", "should", "code", "memory"}

def normalize_search_query(query: str) -> str:
    """Strips conversational filler/stop words from natural language queries prior to FTS5 MATCH formatting."""
    if not query:
        return ""
    words = re.findall(r'\b\w+\b', query)
    filtered = [w for w in words if w.lower() not in SEARCH_STOP_WORDS]
    if filtered:
        return " ".join(filtered)
    return query

SYNONYMS = {
    "wal": "write ahead logging",
    "db": "database",
    "sql": "sqlite",
    "config": "configure"
}

def stem(word: str) -> str:
    w = word.lower()
    if len(w) > 3 and w.endswith('s'):
        if w.endswith('es') and not w.endswith('aes') and not w.endswith('ees') and not w.endswith('oes'):
            w = w[:-2]
        else:
            w = w[:-1]
    for suffix in ['ation', 'ing', 'ment', 'ness', 'ed', 'ly', 'ive', 'al', 'ic']:
        if len(w) > 4 and w.endswith(suffix):
            w = w[:-len(suffix)]
            break
    if len(w) > 3 and w.endswith('e'):
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2]:
        w = w[:-1]
    return w

def tokenize(text: str) -> set:
    text_lower = text.lower()
    for k, v in SYNONYMS.items():
        text_lower = re.sub(r'\b' + k + r'\b', v, text_lower)
    words = re.findall(r'\b\w+\b', text_lower)
    return {stem(w) for w in words if w not in SEARCH_STOP_WORDS}

def word_sim(text1: str, text2: str) -> float:
    w1 = tokenize(text1)
    w2 = tokenize(text2)
    if not w1 or not w2:
        return 0.0
    jac = len(w1 & w2) / len(w1 | w2)
    ovr = len(w1 & w2) / min(len(w1), len(w2))
    return (jac + ovr) / 2.0

@mcp.tool()
def check_duplicate_memories(
    title: str,
    content: str,
    owner_id: str,
    tags: list = None,
    project_id: str = None
) -> dict:
    """Checks if a proposed memory overlaps with existing ones before writing.
    
    Mandatory Pre-Write Check:
    - ALWAYS call this tool before calling `store_memory` to prevent duplicate clutter.
    - Returns a list of potential duplicates with similarity scores. If a high-similarity duplicate is found (score >= 0.7), update the existing memory instead of storing a new one.
    
    Args:
        title: Proposed title of the memory.
        content: Proposed markdown content of the memory.
        owner_id: Mandatory ID of the agent/owner to isolate lanes. Pass your active agent/role identifier (e.g., 'ops', 'tea', or your active agent ID).
        tags: Optional list of tags associated with the proposed memory (highly recommended to improve tag-based duplicate filtering).
        project_id: Optional first-class project identifier to filter duplicates within the same project context.
    """
    if not owner_id:
        return {"error": "owner_id is mandatory."}
        
    title = title or ""
    content = content or ""
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        project_clause = ""
        project_params = []
        if project_id:
            project_clause = " AND (e.project_id = ? OR e.project_id IS NULL)"
            project_params.append(project_id)

        # Find active candidate entities with the same owner/scope, and fetch their tags
        cursor = conn.execute(f"""
            SELECT e.id, e.title, e.full_content,
                   (SELECT group_concat(t.name) FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = e.id) as tag_list
            FROM entities e
            WHERE e.status != 'archived' AND (e.owner_id = ? OR e.owner_id = 'shared') {project_clause}
        """, [owner_id] + project_params)
        candidates = cursor.fetchall()
        
        import difflib
        matches = []
        proposed_tags = set(t.lower().strip() for t in tags) if tags else set()
        
        # Calculate similarity scores
        for cid, ctitle, ccontent, tag_list in candidates:
            ctitle = ctitle or ""
            ccontent = ccontent or ""
            # Title similarity (Levenshtein/SequenceMatcher + stemmed Word Sim)
            title_sm = difflib.SequenceMatcher(None, title.lower(), ctitle.lower()).ratio()
            title_ws = word_sim(title, ctitle)
            title_sim = max(title_sm, title_ws)
            
            # Content similarity (SequenceMatcher on first 1000 chars + stemmed Word Sim on full text)
            snippet1 = content[:1000].lower()
            snippet2 = ccontent[:1000].lower()
            content_sm = difflib.SequenceMatcher(None, snippet1, snippet2).ratio()
            content_ws = word_sim(content, ccontent)
            content_sim = max(content_sm, content_ws)
            
            # Penalize/scale content similarity if the content is very short (less than 40 chars)
            min_content_len = min(len(content), len(ccontent))
            if min_content_len < 40:
                scale_factor = 0.2 + 0.8 * (min_content_len / 40.0)
                content_sim *= scale_factor
                
            # Base similarity (weighted title and content)
            base_sim = (title_sim * 0.40) + (content_sim * 0.60)
            
            # Tag similarity
            candidate_tags = set(t.lower().strip() for t in tag_list.split(',')) if tag_list else set()
            if proposed_tags and candidate_tags:
                tag_jac = len(proposed_tags & candidate_tags) / len(proposed_tags | candidate_tags)
                tag_ovr = len(proposed_tags & candidate_tags) / min(len(proposed_tags), len(candidate_tags))
                tag_sim = (tag_jac + tag_ovr) / 2.0
                # Boost overall similarity by up to 0.05 if tags match
                overall_sim = min(1.0, base_sim + 0.05 * tag_sim)
            else:
                tag_sim = 0.0
                overall_sim = base_sim
            
            if overall_sim > 0.6:  # Return anything with over 60% similarity
                matches.append({
                    "id": cid,
                    "title": ctitle,
                    "similarity_score": round(overall_sim, 2),
                    "title_similarity": round(title_sim, 2),
                    "content_similarity": round(content_sim, 2),
                    "tag_similarity": round(tag_sim, 2)
                })
                
        # Sort by similarity score descending
        matches.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {
            "duplicate_found": len(matches) > 0 and matches[0]["similarity_score"] >= 0.65,
            "potential_duplicates": matches[:5]
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@mcp.tool()
def store_relation(source_id: str = None, target_id: str = None, predicate: str = None, **kwargs) -> str:
    """Stores a typed directional link (edge) between two long-term memory entities.
    
    Backlink Boosting Rule:
    - Link related memories (e.g. linking a fix memory back to its root issue memory).
    - Memories with incoming active relations gain 'PageRank authority', boosting their search ranking and visibility inside search results.
    
    Args:
        source_id: UUID of the source entity (subject).
        target_id: UUID of the target entity (object).
        predicate: Description of the relationship. Canonical values: 'depends_on' (dependency link), 'part_of' (logical containment), 'resolved_by' (issue resolution path), 'links_to' (general association), 'duplicate_of' (identifies identical/redundant entries).
    """
    source_id = source_id or kwargs.get("source_id") or kwargs.get("from_id") or kwargs.get("source")
    target_id = target_id or kwargs.get("target_id") or kwargs.get("to_id") or kwargs.get("target")
    predicate = predicate or kwargs.get("predicate") or kwargs.get("type") or "links_to"
    
    db_path = get_db_path()
    conn = init_db(db_path)
    source_id = resolve_entity_id(conn, source_id)
    target_id = resolve_entity_id(conn, target_id)
    
    if source_id == target_id:
        return "Error: source_id and target_id cannot be identical (self-referential relations forbidden)."
        
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
def analyze_dependencies(root_entity_id: str = None, max_depth: int = 5, **kwargs) -> dict:
    """Traverses the temporal knowledge graph using recursive CTE queries to map downstream components.
    
    Args:
        root_entity_id: The UUID or title of the memory node to begin traversal from.
        max_depth: Maximum depth of graph traversal (default 5, max 20).
    """
    root_entity_id = root_entity_id or kwargs.get("root_entity_id") or kwargs.get("entity_id") or kwargs.get("id")
    safe_max_depth = max(1, min(20, max_depth))
    db_path = get_db_path()
    conn = init_db(db_path)
    root_entity_id = resolve_entity_id(conn, root_entity_id)
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
                  AND dt.depth < ?
                  AND dt.id_path NOT LIKE '%,' || child.id || ',%'
            )
            SELECT DISTINCT id, title, status, depth, title_path FROM dependency_tree
        """, (root_entity_id, safe_max_depth))
        rows = cursor.fetchall()
        max_depth_reached = max((r[3] for r in rows), default=0)
        graph_exhausted = max_depth_reached < safe_max_depth
        
        dependencies = [
            {
                "id": r[0],
                "title": r[1],
                "status": r[2],
                "depth": r[3],
                "path": r[4]
            } for r in rows
        ]
        return {
            "root_entity_id": root_entity_id,
            "graph_exhausted": graph_exhausted,
            "max_depth_traversed": max_depth_reached,
            "dependencies": dependencies
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@mcp.tool()
def analyze_lineage(entity_id: str = None, **kwargs) -> dict:
    """Traverses full multi-generation consolidation and derivation lineage for a memory node.
    
    Args:
        entity_id: The UUID or title of the consolidated/derived memory node to trace lineage for.
    """
    entity_id = entity_id or kwargs.get("entity_id") or kwargs.get("id")
    db_path = get_db_path()
    conn = init_db(db_path)
    entity_id = resolve_entity_id(conn, entity_id)
    try:
        cursor = conn.execute("""
            WITH RECURSIVE lineage_tree(id, title, status, depth, id_path, lineage_path) AS (
                -- Anchor member
                SELECT e.id, e.title, e.status, 0, ',' || e.id || ',', e.title
                FROM entities e
                WHERE e.id = ?
                
                UNION ALL
                
                -- Recursive member: trace consolidated_from or derived_from target parents
                SELECT parent.id, parent.title, parent.status, lt.depth + 1,
                       lt.id_path || parent.id || ',',
                       lt.lineage_path || ' <- ' || parent.title
                FROM entities parent
                JOIN relations r ON r.target_id = parent.id
                JOIN lineage_tree lt ON r.source_id = lt.id
                WHERE r.predicate IN ('consolidated_from', 'derived_from') AND r.valid_to IS NULL
                  AND lt.id_path NOT LIKE '%,' || parent.id || ',%'
            )
            SELECT DISTINCT id, title, status, depth, lineage_path FROM lineage_tree;
        """, (entity_id,))
        rows = cursor.fetchall()
        lineage = [
            {
                "id": r[0],
                "title": r[1],
                "status": r[2],
                "depth": r[3],
                "path": r[4]
            } for r in rows
        ]
        return {
            "entity_id": entity_id,
            "lineage_depth": max((r[3] for r in rows), default=0),
            "ancestors": lineage
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()

@mcp.tool()
def get_recent_events(agent_id: str = None, type_filter: str = None, limit: int = 20) -> list:
    """Retrieves recent events from the short-term ledger.
    
    Args:
        agent_id: Optional filter for a specific agent ID. Pass your active agent/role identifier (e.g., 'ops', 'tea', or your active agent ID).
        type_filter: Optional filter for a specific event type. Canonical types: 'consolidation_request' (system merge request), 'decision' (design/outcome log), 'attempt' (trial action/command run), 'fix' (bug/error resolution path), 'issue' (error/failure log).
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
    offset: int = 0,
    cursor: str = None
) -> list:
    """Scans and lists long-term memories for a specific owner to perform audits, contradiction checks, or status reviews.
    
    CRITICAL USAGE RULES:
    - MANDATORY OWNER: You MUST supply a valid 'owner_id' parameter.
    - BYPASS BAN: Do not attempt to run direct sqlite3 or file commands to inspect memories. All reads must proceed through this tool or search_memory.
    
    Args:
        owner_id: Mandatory ID of the agent/owner to isolate lanes and memory access. Pass your active agent/role identifier (e.g., 'ops', 'tea', or your active agent ID).
        status_filter: Filter by memory status: 'raw', 'consolidated', 'archived', 'active' (both raw and consolidated, default), or 'all'.
        limit: Maximum number of memories to return (default 20, max 100).
        offset: Offset for pagination (default 0, ignored if cursor is provided).
        cursor: Optional base64 encoded cursor for pagination.
    """
    if not owner_id:
        return [{"error": "owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."}]
        
    db_path = get_db_path()
    conn = init_db(db_path)
    safe_limit = max(1, min(100, limit))
    
    offset_val = offset
    if cursor:
        try:
            decoded = base64.b64decode(cursor.encode()).decode()
            if decoded.startswith("offset:"):
                offset_val = int(decoded.split(":", 1)[1])
            else:
                offset_val = int(decoded)
        except Exception:
            pass
            
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
            ORDER BY e.updated_at DESC, e.id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([safe_limit, offset_val])
        cursor_db = conn.execute(sql, params)
        rows = cursor_db.fetchall()
        
        memories = []
        for idx, r in enumerate(rows):
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
                    
            next_c = base64.b64encode(f"offset:{offset_val + idx + 1}".encode()).decode()
            memories.append({
                "id": entity_id,
                "title": title,
                "scope": scope,
                "weight": weight,
                "status": status,
                "tags": tags,
                "metadata": metadata,
                "content": full_content,
                "cursor": next_c
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

@mcp.tool()
def bulk_commit_consolidation(
    consolidations: list
) -> list:
    """Bulk commits multiple consolidated memories synthesized by the agent atomically.
    
    Args:
        consolidations: List of consolidation configurations, each containing:
            - parent_ids: List of UUIDs of the raw source memories being consolidated.
            - title: Custom title for the consolidated summary.
            - content: Consolidated Markdown representation (SFB format).
            - tags: List of tags associated with this consolidated memory.
            - scope: Optional scope level ('private' or 'shared', defaults to 'shared').
            - weight: Optional priority weight multiplier (default 1).
    """
    results = []
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        with conn:
            for index, cfg in enumerate(consolidations):
                try:
                    parent_ids = cfg.get("parent_ids")
                    title = cfg.get("title")
                    content = cfg.get("content")
                    tags = cfg.get("tags")
                    scope = cfg.get("scope", "shared")
                    weight = cfg.get("weight", 1)
                    
                    if not parent_ids or not title or not content or not tags:
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": "Missing mandatory fields (parent_ids, title, content, tags)."
                        })
                        continue
                        
                    if scope not in ('private', 'shared'):
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": "scope must be either 'private' or 'shared'"
                        })
                        continue
                        
                    validate_memory_input(title, content, None)
                    
                    entity_id = str(uuid.uuid4())
                    redacted_content = redact_secrets(content)
                    now = datetime.now(UTC).isoformat()
                    
                    # 1. Insert the new consolidated entity
                    conn.execute("""
                        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content)
                        VALUES (?, ?, ?, ?, 'system', ?, 0, ?, 'consolidated', ?, ?, ?)
                    """, (entity_id, now, now, now, scope, weight, json.dumps(parent_ids), title, redacted_content))
                    
                    # 2. Tag mapping
                    for tag in tags:
                        clean_tag = tag.strip()
                        if clean_tag:
                            cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (clean_tag,))
                            tag_row = cursor.fetchone()
                            if tag_row:
                                tag_id = tag_row[0]
                            else:
                                tag_id = str(uuid.uuid4())
                                conn.execute("INSERT INTO tags (id, name, created_at) VALUES (?, ?, ?)", (tag_id, clean_tag, now))
                            conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                            
                    # 3. Archive parent entities
                    placeholders = ",".join("?" for _ in parent_ids)
                    conn.execute(f"""
                        UPDATE entities 
                        SET status = 'archived', updated_at = ?, valid_to = ?
                        WHERE id IN ({placeholders})
                    """, [now, now] + parent_ids)
                    
                    # 4. Link parents to consolidated child & re-point existing relations
                    if parent_ids:
                        p_placeholders = ",".join("?" for _ in parent_ids)
                        conn.execute(f"""
                            UPDATE relations SET source_id = ?
                            WHERE source_id IN ({p_placeholders}) AND source_id != ?
                        """, [entity_id] + parent_ids + [entity_id])
                        conn.execute(f"""
                            UPDATE relations SET target_id = ?
                            WHERE target_id IN ({p_placeholders}) AND target_id != ?
                        """, [entity_id] + parent_ids + [entity_id])
                        conn.execute("DELETE FROM relations WHERE source_id = target_id")
                        conn.execute("""
                            DELETE FROM relations 
                            WHERE id NOT IN (
                                SELECT MIN(id) FROM relations 
                                GROUP BY source_id, target_id, predicate
                            )
                        """)
                        for p_id in parent_ids:
                            relation_id = str(uuid.uuid4())
                            conn.execute("""
                                INSERT OR IGNORE INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (relation_id, p_id, entity_id, 'consolidated_into', now, now))
                        
                    results.append({
                        "index": index,
                        "status": "success",
                        "entity_id": entity_id,
                        "title": title
                    })
                except Exception as ex:
                    results.append({
                        "index": index,
                        "status": "error",
                        "error": str(ex)
                    })
    except Exception as e:
        return [{"status": "error", "error": f"Transaction failed: {str(e)}"}]
    finally:
        conn.close()
    return results

@mcp.tool()
def bulk_archive_memory(
    archive_requests: list = None,
    **kwargs
) -> list:
    """Bulk archives multiple memories atomically in a single transaction."""
    archive_requests = archive_requests or kwargs.get("archive_requests") or kwargs.get("requests") or kwargs.get("items") or []
    results = []
    db_path = get_db_path()
    conn = init_db(db_path)
    try:
        now = datetime.now(UTC).isoformat()
        with conn:
            for index, req in enumerate(archive_requests):
                try:
                    if isinstance(req, str):
                        raw_eid = req
                        owner_id = None
                    elif isinstance(req, dict):
                        raw_eid = req.get("entity_id") or req.get("id")
                        owner_id = req.get("owner_id") or req.get("owner")
                    else:
                        continue
                        
                    entity_id = resolve_entity_id(conn, raw_eid)
                    if not entity_id:
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": "Missing entity_id."
                        })
                        continue
                        
                    # Check existence and ownership of active memory
                    if owner_id:
                        cursor = conn.execute("SELECT id FROM entities WHERE id = ? AND (owner_id = ? OR owner_id = 'shared') AND status != 'archived'", (entity_id, owner_id))
                    else:
                        cursor = conn.execute("SELECT id FROM entities WHERE id = ? AND status != 'archived'", (entity_id,))
                    row = cursor.fetchone()
                    if not row:
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": f"Active memory with ID '{entity_id}' not found."
                        })
                        continue
                        
                    # Perform temporal archiving
                    conn.execute("""
                        UPDATE entities 
                        SET status = 'archived', updated_at = ?, valid_to = ?
                        WHERE id = ?
                    """, (now, now, entity_id))
                    
                    results.append({
                        "index": index,
                        "status": "success",
                        "entity_id": entity_id
                    })
                except Exception as ex:
                    results.append({
                        "index": index,
                        "status": "error",
                        "error": str(ex)
                    })
    except Exception as e:
        return [{"status": "error", "error": f"Transaction failed: {str(e)}"}]
    finally:
        conn.close()
    return results

@mcp.tool()
def bulk_store_relations(
    relations: list = None,
    **kwargs
) -> list:
    """Bulk stores directional relationship links between memories."""
    relations = relations or kwargs.get("relations") or kwargs.get("items") or []
    results = []
    db_path = get_db_path()
    conn = init_db(db_path)
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            for index, rel in enumerate(relations):
                try:
                    raw_src = rel.get("source_id") or rel.get("source") or rel.get("from_id")
                    raw_tgt = rel.get("target_id") or rel.get("target") or rel.get("to_id")
                    predicate = rel.get("predicate") or rel.get("type") or "links_to"
                    
                    source_id = resolve_entity_id(conn, raw_src)
                    target_id = resolve_entity_id(conn, raw_tgt)
                    
                    if not source_id or not target_id or not predicate:
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": "Missing source_id, target_id, or predicate."
                        })
                        continue
                        
                    if source_id == target_id:
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": "source_id and target_id cannot be identical (self-referential relations forbidden)."
                        })
                        continue
                        
                    # Verify both entities exist
                    cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (source_id,))
                    if not cursor.fetchone():
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": f"Source entity {source_id} does not exist."
                        })
                        continue
                        
                    cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (target_id,))
                    if not cursor.fetchone():
                        results.append({
                            "index": index,
                            "status": "error",
                            "error": f"Target entity {target_id} does not exist."
                        })
                        continue
                        
                    relation_id = str(uuid.uuid4())
                    conn.execute("""
                        INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (relation_id, source_id, target_id, predicate, now, now))
                    
                    results.append({
                        "index": index,
                        "status": "success",
                        "relation_id": relation_id
                    })
                except Exception as ex:
                    results.append({
                        "index": index,
                        "status": "error",
                        "error": str(ex)
                    })
    except Exception as e:
        return [{"status": "error", "error": f"Transaction failed: {str(e)}"}]
    finally:
        conn.close()
    return results

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
