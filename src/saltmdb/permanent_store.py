import sqlite3


class PermanentStore:
    def __init__(self) -> None:
        self._conn=sqlite3.connect("saltmdb.db", check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                agent_id TEXT NOT NULL,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                error_code TEXT
            );
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                valid_from DATETIME NOT NULL,
                valid_to DATETIME NOT NULL,
                last_accessed DATETIME NOT NULL,
                owner_id TEXT,
                scope TEXT CHECK(scope IN ('private','shared')) DEFAULT 'shared',
                is_core BOOLEAN DEFAULT 0,
                weight INTEGER DEFAULT 1,
                status TEXT CHECK(status IN ('raw', 'consolidated', 'archived')) DEFAULT 'raw',
                pranet_ids TEXT, -- JSON Array of parant ids
                title TEXT NOT NULL,
                full_content TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                canonical_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (canonical_id) REFERENCES tags(id) ON DELETE SET NULL
            );
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_tags(
                entity_id TEXT,
                tag_id TEXT,
                PRIMARY KEY (entity_id, tag_id),
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS relations(
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

            self._conn.execute("DROP TABLE IF EXISTS entities_fts")

            self._conn.execute("""
            CREATE VIRTUAL TABLE entities_fts USING fts5(
                id UNINDEXED,
                title,
                full_content,
                search_aliases,
                tokenize='porter'
            );
            """)

            self._conn.execute("""
            INSERT INTO entities_fts (id, title, full_content, search_aliases)
            SELECT id, title, full_content, coalesce(json_extract(metadata, '$.search_aliases'), '')
            FROM entities;
            """)

            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS _system_locks(
                task_name TEXT PRIMARY KEY,
                locked_at DATETIME,
                locked_by_pid INTEGER,
                last_run_at DATETIME
            );
            """)

            self._conn.execute("""
            INSERT OR IGNORE INTO _system_locks (task_name, locked_at, locked_by_pid, last_run_at)
            VALUES ('librarian_consolidation', NULL, NULL, NULL)
            """)

            self._conn.execute("DROP TRIGGER IF EXISTS insert_entity_fts")
            self._conn.execute("DROP TRIGGER IF EXISTS update_entity_fts")
            self._conn.execute("DROP TRIGGER IF EXISTS update_entity_fts_unarchived")
            self._conn.execute("DROP TRIGGER IF EXISTS archive_entity_fts")
            self._conn.execute("DROP TRIGGER IF EXISTS delete_entity_fts")
            

            self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS insert_entity_fts
            AFTER INSERT ON entities
            WHEN NEW.status != 'archived'
            BEGIN
                INSERT INTO entities_fts(id, title, full_content, search_aliases)
                VALUES (NEW.id, NEW.title, NEW.full_content, coalesce(json_extract(NEW.metadata, '$.search_aliases'), ''));
            END;
            """)

            self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS update_entity_fts
            AFTER UPDATE ON entities
            WHEN NEW.status != 'archived' AND OLD.status != 'archived'
            BEGIN
                UPDATE entities_fts
                SET title = NEW.title,
                    full_content = NEW.full_content,
                    search_aliases = coalesce(json_extract(NEW.metadata, '$.search_aliases'),'')
                WHERE id = OLD.id;
            END;
            """)

            self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS update_entity_fts_unarchived
            AFTER UPDATE ON entities
            WHEN NEW.status != 'archived' AND OLD.status = 'archived'
            BEGIN
                INSERT INTO entities_fts(id, title, full_content, search_aliases)
                VALUES (NEW.id, NEW.title, NEW.full_content, coalesce(json_extract(NEW.metadata, '$.search_aliases'), ''));
            END;
            """)

            self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS archive_entity_fts
            AFTER UPDATE ON entities
            WHEN NEW.status = 'archived'
            BEGIN
                DELETE FROM entities_fts WHERE id = OLD.id;
            END;
            """)

            self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS delete_entity_fts
            AFTER DELETE ON entities
            BEGIN
                DELETE FROM entities_fts WHERE id = OLD.id;
            END;
            """)
            