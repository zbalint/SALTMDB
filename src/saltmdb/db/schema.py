import sqlite3
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection

def init_db(db_path: str = None) -> sqlite3.Connection:
    """Initialize the local SQLite database with Write-Ahead Logging (WAL), DDL tables, triggers, and migrations."""
    if not db_path:
        db_path = get_db_path()
        
    conn = get_connection(db_path)
    
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
        for col in ["valid_from DATETIME", "valid_to DATETIME", "metadata TEXT", "project_id TEXT", "context_id TEXT", "embedding_status TEXT DEFAULT 'pending'"]:
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
            
        from saltmdb.db.vector_schema import init_vector_schema
        try:
            init_vector_schema(conn)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Vector schema init deferred/failed: %s", e)
        
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
