import logging
import sqlite3

logger = logging.getLogger(__name__)


def try_load_vector_extension(conn: sqlite3.Connection) -> bool:
    """Best-effort load of the sqlite_vec extension onto an existing connection.

    For callers (e.g. viewer HTTP routes) that open their own ad-hoc connection per
    request rather than going through init_db()/init_vector_schema(), and therefore
    need the extension loaded before querying the entity_embeddings vec0 virtual table.
    Returns False instead of raising if the extension can't be loaded, so callers can
    degrade gracefully the same way the rest of the vector-feature call sites do.
    """
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as e:
        logger.debug("sqlite_vec extension load skipped or failed: %s", e)
        return False


def init_vector_schema(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension and create the entity_embeddings virtual table.

    Kept separate from schema.py's relational DDL for the same reason entities_fts
    is a separate virtual table: different storage internals, same established pattern.

    Callers must ensure `import sqlite_vec` has already succeeded once in this process
    BEFORE opening any write transaction this runs inside (see schema.py's init_db, which
    pre-imports it ahead of BEGIN IMMEDIATE). The `import sqlite_vec` below relies on that --
    it's a no-op cache hit in the normal case, but if it ever runs as the *first* import of
    sqlite_vec/numpy in the process while already holding the write lock, a slow cold import
    (antivirus/EDR scanning the DLLs, concurrent native-module load contention) turns into an
    indefinite hold on that lock, blocking every other writer against the database for as
    long as the import stalls.
    """
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings USING vec0(
            entity_id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        );
    """)
