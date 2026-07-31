import sqlite3
import logging
import uuid
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying

logger = logging.getLogger(__name__)

def _add_column_if_missing(conn, table: str, column_def: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN: swallows the genuine 'already exists' case,
    re-raises anything else so real schema bugs don't vanish silently."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def};")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise

def init_db(db_path: str = None) -> sqlite3.Connection:
    """Initialize the local SQLite database with Write-Ahead Logging (WAL), DDL tables, triggers, and migrations."""
    if not db_path:
        db_path = get_db_path()
        
    conn = get_connection(db_path)

    # Force sqlite_vec (and its numpy dependency, a large native extension) to finish
    # importing BEFORE the write transaction below opens BEGIN IMMEDIATE. init_vector_schema()
    # (called from inside _write, further down) also does `import sqlite_vec`, but Python
    # caches successful imports in sys.modules -- so as long as the first import in this
    # process happens here, unlocked, that later import is just a cache hit. Getting this
    # backwards is not cosmetic: a plain module import can stall for a long time on a cold
    # import cache (e.g. antivirus/EDR scanning the DLLs on first load, or OS-level
    # contention if another process is importing the same native module at the same time),
    # and if that stall happens *inside* BEGIN IMMEDIATE, the process is holding the real
    # SQLite write lock for the entire stall -- turning an ordinary slow import into an
    # indefinite lock hold that blocks every other writer against the database. This was
    # confirmed live: a librarian subprocess's sqlite_vec/numpy import stalled for 9+ hours
    # mid-transaction, and every other session's store_memory/log_event call failed with
    # "database is locked" on every attempt for the entire time -- exactly matching the
    # reported symptom of one session's activity permanently wedging another session, even
    # after the first session goes idle (its background subprocess kept holding the lock).
    try:
        import sqlite_vec  # noqa: F401
    except Exception as e:
        logger.warning("sqlite_vec import failed (vector search will be unavailable): %s", e)

    # Wrapped in write_transaction_retrying (BEGIN IMMEDIATE + retry/backoff) rather than left
    # as a bare `with conn:`. Since get_connection() sets isolation_level=None, a bare `with conn:`
    # on this connection no longer groups these statements into one transaction at all (no implicit
    # BEGIN happens under isolation_level=None, so each statement autocommits individually) --
    # that's a real atomicity regression versus the pre-isolation_level=None behavior, not just a
    # cosmetic difference. Wrapping restores single-transaction atomicity for the whole schema init.
    # This is safe to retry-from-scratch on "database is locked": every statement in this block is
    # idempotent (CREATE TABLE IF NOT EXISTS, guarded ALTER TABLE, idempotent UPDATE backfills,
    # INSERT OR IGNORE, CREATE INDEX/TRIGGER IF NOT EXISTS), and BEGIN IMMEDIATE acquires the write
    # lock up front, so a retry either fails at the BEGIN itself (nothing applied yet) or succeeds
    # and runs the full idempotent block to completion -- there is no partially-applied state that
    # a retry could see or corrupt. This also directly helps the multi-agent concurrent-startup
    # contention that motivated the busy_timeout bump (commit 548d170), since concurrent init_db()
    # calls are exactly where BEGIN IMMEDIATE + retry pays off most.
    def _write(c):
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
            parent_ids TEXT, -- JSON array of ancestor IDs (derived/display-only; not authoritative for lineage traversal, which uses the relations table)
            title TEXT NOT NULL,
            full_content TEXT NOT NULL,
            valid_from DATETIME,
            valid_to DATETIME,
            metadata TEXT
        );
        """)
        
        # Schema migration: attempt to add new columns to entities table if they don't exist
        for col in [
            "valid_from DATETIME",
            "valid_to DATETIME",
            "metadata TEXT",
            "project_id TEXT",
            "context_id TEXT",
            "embedding_status TEXT DEFAULT 'pending'",
            "content_hash TEXT",
            "quality_score REAL",
            "quality_status TEXT",
            "quality_flags TEXT",
            "memory_type TEXT CHECK(memory_type IN ('fact','event','procedure','decision','preference')) DEFAULT 'fact'",
        ]:
            _add_column_if_missing(conn, "entities", col)

        # Schema migration (alpha.61 / Schema Version 13): remove the entities.domain
        # classification column entirely. It shipped in alpha.58 as a closed vocabulary
        # (VALID_DOMAINS) hardcoded to one operator's personal project/life-area split, which
        # doesn't generalize to other installs and duplicates what tags already cover. Adoption
        # was negligible in practice. DROP INDEX first since SQLite's ALTER TABLE DROP COLUMN
        # refuses to run while an index still references the column. Guarded for SQLite < 3.35
        # (no DROP COLUMN support) and for repeated runs against an already-migrated DB (both
        # raise OperationalError, which is expected and safe to ignore here).
        try:
            conn.execute("DROP INDEX IF EXISTS idx_entities_domain")
            conn.execute("ALTER TABLE entities DROP COLUMN domain")
        except sqlite3.OperationalError as e:
            logger.debug("entities.domain column drop skipped (already migrated or unsupported SQLite version): %s", e)

        # Schema migration: attempt to add new columns to events table if they don't exist
        for col in ["session_id TEXT", "context_id TEXT"]:
            _add_column_if_missing(conn, "events", col)

        # Backfill embedding_status = 'archived' for any archived entities
        try:
            conn.execute("UPDATE entities SET embedding_status = 'archived' WHERE status = 'archived' AND (embedding_status != 'archived' OR embedding_status IS NULL);")
        except sqlite3.OperationalError:
            pass

        # Schema migration: project_id is retired in favor of context_id (kept as a physical
        # column for compatibility, but no longer written/read by application code past this backfill)
        try:
            conn.execute("UPDATE entities SET context_id = project_id WHERE context_id IS NULL AND project_id IS NOT NULL;")
        except sqlite3.OperationalError:
            pass
        
        # 3. Tags Table (Folksonomy with support for canonical aliases)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            normalized_name TEXT,
            canonical_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (canonical_id) REFERENCES tags(id) ON DELETE SET NULL
        );
        """)
        
        _add_column_if_missing(conn, "tags", "normalized_name TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_normalized_name ON tags(normalized_name);")
        
        # Schema migration (alpha.60): seed canonical top-level tags (episodic/semantic/procedural),
        # independent of each other -- no aliasing needed. Mirrors predicates' alpha.55 seeding
        # idempotency (INSERT OR IGNORE); canonical_id stays NULL, these ARE the canonical rows.
        for _seed_tag_name in ("episodic", "semantic", "procedural"):
            conn.execute(
                "INSERT OR IGNORE INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (str(uuid.uuid4()), _seed_tag_name, _seed_tag_name)
            )

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

        # One-time dedup backfill: collapse pre-existing duplicate (source_id, target_id, predicate)
        # rows to the earliest-inserted one before the UNIQUE index below is created (a raw
        # CREATE UNIQUE INDEX would otherwise fail at startup on any DB with existing dupes).
        # Uses rowid (monotonic insertion order) not MIN(id) -- id is a random UUID with no
        # relationship to insertion order. Idempotent: matches zero rows on every run after the first.
        try:
            conn.execute("""
                DELETE FROM relations
                WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM relations GROUP BY source_id, target_id, predicate
                );
            """)
        except sqlite3.OperationalError as e:
            logger.warning("Relations dedup backfill skipped/failed: %s", e)

        # Schema migration (alpha.57 / Schema Version 10): the UNIQUE index on relations must become
        # a PARTIAL index (WHERE valid_to IS NULL) now that commit_consolidation starts populating
        # valid_to via expire-then-insert repointing. SQLite has no ALTER INDEX, and CREATE UNIQUE
        # INDEX IF NOT EXISTS is a silent no-op against an already-existing same-named index even when
        # its definition differs -- so this must unconditionally DROP + recreate to actually replace
        # the old (Quick Wins round) non-partial index. Idempotent: cheap no-op once already migrated.
        try:
            conn.execute("DROP INDEX IF EXISTS idx_relations_unique_edge")
            conn.execute(
                "CREATE UNIQUE INDEX idx_relations_unique_edge "
                "ON relations(source_id, target_id, predicate) WHERE valid_to IS NULL"
            )
        except sqlite3.OperationalError as e:
            logger.warning("Relations partial unique index migration skipped/failed: %s", e)

        # Schema migration (alpha.60 / Schema Version 12): bi-temporal event/world-time axis for
        # relations, independent of valid_from/valid_to (system/transaction time, consolidation-only).
        for col in ["valid_at DATETIME", "invalid_at DATETIME"]:
            _add_column_if_missing(conn, "relations", col)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS predicates (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            normalized_name TEXT,
            canonical_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (canonical_id) REFERENCES predicates(id) ON DELETE SET NULL
        );
        """)
        for _pred_name in ("resolves", "depends_on", "references", "elaborates_on",
                            "consolidated_from", "supersedes", "relates_to"):
            conn.execute(
                "INSERT OR IGNORE INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (str(uuid.uuid4()), _pred_name, _pred_name)
            )
        # Pre-alias observed drift (relates_to/references used interchangeably with elaborates_on)
        # onto elaborates_on as canonical. Guarded by canonical_id IS NULL so a future manual
        # re-merge tool's decision is never silently clobbered on restart.
        _canon_row = conn.execute("SELECT id FROM predicates WHERE name = 'elaborates_on'").fetchone()
        if _canon_row:
            conn.execute(
                "UPDATE predicates SET canonical_id = ? WHERE name IN ('relates_to', 'references') AND canonical_id IS NULL AND id != ?",
                (_canon_row[0], _canon_row[0])
            )

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
            logger.warning("Vector schema init deferred/failed: %s", e)
        
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
        _add_column_if_missing(conn, "_system_locks", "last_run_at DATETIME")
            
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
        CREATE TRIGGER IF NOT EXISTS archive_memory_embedding_status
        AFTER UPDATE ON entities
        WHEN NEW.status = 'archived' AND (NEW.embedding_status IS NULL OR NEW.embedding_status != 'archived')
        BEGIN
            UPDATE entities SET embedding_status = 'archived' WHERE id = NEW.id;
        END;
        """)
        
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_entity_fts
        AFTER DELETE ON entities
        BEGIN
            DELETE FROM entities_fts WHERE id = OLD.id;
        END;
        """)
        
        # Performance indexes for high-traffic filtering columns
        for index_sql in [
            "CREATE INDEX IF NOT EXISTS idx_entities_status_updated ON entities(status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_entities_owner_scope ON entities(owner_id, scope)",
            "CREATE INDEX IF NOT EXISTS idx_entities_context ON entities(context_id)",
            "CREATE INDEX IF NOT EXISTS idx_entities_embedding ON entities(embedding_status) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_is_core ON entities(is_core) WHERE is_core = 1",
            "CREATE INDEX IF NOT EXISTS idx_entities_content_hash ON entities(owner_id, content_hash) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_memory_type ON entities(memory_type) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_id, type, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_id)",
            "CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_id)",
            "CREATE INDEX IF NOT EXISTS idx_relations_predicate ON relations(predicate)",
            "CREATE INDEX IF NOT EXISTS idx_tags_canonical ON tags(canonical_id)",
            "CREATE INDEX IF NOT EXISTS idx_entity_tags_tag_id ON entity_tags(tag_id)",
            "CREATE INDEX IF NOT EXISTS idx_predicates_normalized_name ON predicates(normalized_name)",
            "CREATE INDEX IF NOT EXISTS idx_predicates_canonical ON predicates(canonical_id)",
        ]:
            try:
                conn.execute(index_sql)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "already exists" not in msg and "already an index" not in msg:
                    logger.error("Failed to create index (%s): %s", index_sql, e)

    write_transaction_retrying(conn, _write)
    return conn
