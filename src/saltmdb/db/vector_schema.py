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


def init_entity_chunk_vector_schema(conn: sqlite3.Connection) -> None:
    """Create the entity_chunk_embeddings virtual table (chunk-level embeddings, memory-core
    rework Foundation phase -- see plans/ and SALTMDB memory `5c09effa`).

    Coexists with entity_embeddings (entity-level, one vector per entity); does NOT replace it.
    Every existing entity_embeddings reader/writer is untouched by this table's existence.

    `id` is a deterministic composite `f"{entity_id}::{chunk_index}"`, so re-embedding an entity
    is a clean DELETE-then-INSERT by entity_id (see embedding_service.write_entity_chunk_embeddings).
    `entity_id` is declared PARTITION KEY (not a plain column) so point-filtering, deleting, and
    KNN-scoping all chunks belonging to one entity stay efficient -- this is the operation later
    rework phases (search_memory, consolidate_vector_clusters) will lean on most. chunk_index,
    char_start, char_end are auxiliary (`+`) columns: stored and retrievable, but not part of the
    vector index itself.

    Loads the sqlite_vec extension onto this connection itself (mirrors init_vector_schema and
    every other vec0 call site in this codebase -- embed_entity_async, consolidate_vector_clusters,
    scout_consolidated_supersessions all self-load defensively rather than assume a prior call
    already attached the extension to this specific connection object). Loading twice on the same
    connection is a harmless no-op, so calling this right after init_vector_schema(conn) (as
    schema.py's init_db does) costs nothing extra while making this function safe to call
    standalone -- from a test, a future script, or any other caller that never went through
    init_vector_schema first.
    """
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entity_chunk_embeddings USING vec0(
            id TEXT PRIMARY KEY,
            entity_id TEXT PARTITION KEY,
            embedding FLOAT[384],
            +chunk_index INTEGER,
            +char_start INTEGER,
            +char_end INTEGER,
            +content_hash TEXT
        );
    """)


def migrate_entity_chunk_embeddings_content_hash(conn: sqlite3.Connection) -> None:
    """One-time atomic drop+recreate migration adding a `content_hash` auxiliary column to
    entity_chunk_embeddings (memory-core rework Phase 2, Part A0 -- see plans/ and SALTMDB
    memory `5c09effa`).

    Every row this table stores from here on carries the `entities.content_hash` value that
    produced it, which is what lets the startup repair sweep (Part A3's
    backfill_chunk_embeddings) distinguish "chunk rows exist and are current" from "chunk rows
    exist but are stale" -- a distinction a presence-only `NOT EXISTS` check cannot make.

    vec0 virtual tables reject `ALTER TABLE ... ADD COLUMN` outright (confirmed directly against
    the pinned sqlite-vec 0.1.9: raises `sqlite3.OperationalError: virtual tables may not be
    altered`), so unlike every other schema migration in this codebase this one cannot use
    `_add_column_if_missing`. Detects whether a migration is needed by reading the table's
    declared DDL straight from sqlite_master rather than attempt-and-catch:
      - no row for this table name -> table doesn't exist yet (fresh install); nothing to
        migrate here, no sqlite_vec load needed either -- the normal
        `CREATE VIRTUAL TABLE IF NOT EXISTS` in init_entity_chunk_vector_schema (called right
        after this, inside the existing best-effort try/except) creates it fresh with the new
        column, preserving graceful degradation for installs where sqlite_vec can't load at all.
      - row exists and its `sql` text already mentions `content_hash` -> already migrated, no-op.
      - row exists without `content_hash` -> the only case that needs a real migration: DROP +
        recreate. Since nothing outside this dev branch depends on the old (Foundation-era,
        columnless) shape yet ("rework" branch, unmerged), dropping and losing existing chunk
        rows is acceptable here -- and it happens to double as the one-time atomic clear of all
        chunk rows a staleness migration would need anyway, so no separate DELETE step.

    Must be called on a connection that is already inside the caller's own write transaction
    (schema.py's init_db runs this from inside write_transaction_retrying's BEGIN
    IMMEDIATE/COMMIT) and must NOT open its own nested BEGIN/COMMIT -- sqlite3 connections don't
    support nested transactions, and the outer write_transaction() context manager already
    ROLLBACKs the whole init_db transaction on any exception raised here, which is exactly the
    atomicity this migration needs (never let the DROP commit without its recreate landing in
    the same transaction). Callers must NOT wrap this call in a broad try/except -- a failed
    migration must abort init_db() loudly, not silently leave a committed database with no
    entity_chunk_embeddings table at all.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_chunk_embeddings'"
    ).fetchone()
    if row is None or (row[0] and "content_hash" in row[0]):
        return  # doesn't exist yet, or already migrated -- nothing to do

    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("DROP TABLE entity_chunk_embeddings")
    conn.execute("""
        CREATE VIRTUAL TABLE entity_chunk_embeddings USING vec0(
            id TEXT PRIMARY KEY,
            entity_id TEXT PARTITION KEY,
            embedding FLOAT[384],
            +chunk_index INTEGER,
            +char_start INTEGER,
            +char_end INTEGER,
            +content_hash TEXT
        );
    """)
