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


def _entity_chunk_embeddings_ddl(if_not_exists: bool) -> str:
    """Shared DDL for entity_chunk_embeddings, used by both the fresh-install path
    (init_entity_chunk_vector_schema) and the drop+recreate migration path
    (migrate_entity_chunk_embeddings_schema). Kept as
    one function so the two call sites can never drift out of sync again -- exactly that
    duplication (two copies of this DDL, one of which kept `PARTITION KEY` after the other was
    fixed) is how the PARTITION KEY storage-blowup bug shipped in the first place.

    `entity_id` is a plain auxiliary (`+`) column, NOT a PARTITION KEY. It was declared
    PARTITION KEY through the Foundation phase, but vec0's PARTITION KEY physically isolates a
    full chunk region (vec0's default chunk capacity, ~1.5MB @ 384-dim float32) per DISTINCT
    partition value regardless of how many rows that value actually has -- and real entities
    typically have only 1-4 chunks each, the worst-case usage pattern for a partition key
    (sqlite-vec's own guidance: partition keys "work best with 100's or 1000's of vectors per
    partition value"). Empirically proven and benchmarked (SALTMDB memory `3e0c7a1e` and the
    scratch-DB candidate comparison that followed it): 150 test entities under PARTITION KEY ->
    150 physical chunks / ~228MB; the same data as a plain `+entity_id` aux column -> 1 physical
    chunk / ~3.4MB, because dropping the partition key lets all entities share the table's normal
    global chunk allocation instead of each getting its own isolated, near-empty one. Every
    production reader/writer of this table already does manual `WHERE entity_id = ?` / `IN (...)`
    SQL filtering (write_entity_chunk_embeddings, backfill_chunk_embeddings,
    rerank_candidates_by_topic, get_fresh_entity_centroids) rather than vec0-native
    partition-scoped KNN (`MATCH ... AND k = N AND entity_id = ?`), so this table never actually
    used the one capability PARTITION KEY buys -- the partitioning was pure storage overhead with
    no offsetting production benefit. chunk_index, char_start, char_end, content_hash remain
    auxiliary (`+`) columns: stored and retrievable, but not part of the vector index itself.
    """
    exists_clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE VIRTUAL TABLE {exists_clause}entity_chunk_embeddings USING vec0(
            id TEXT PRIMARY KEY,
            +entity_id TEXT,
            embedding FLOAT[384],
            +chunk_index INTEGER,
            +char_start INTEGER,
            +char_end INTEGER,
            +content_hash TEXT
        );
    """


def init_entity_chunk_vector_schema(conn: sqlite3.Connection) -> None:
    """Create the entity_chunk_embeddings virtual table (chunk-level embeddings, memory-core
    rework Foundation phase -- see plans/ and SALTMDB memory `5c09effa`).

    Coexists with entity_embeddings (entity-level, one vector per entity); does NOT replace it.
    Every existing entity_embeddings reader/writer is untouched by this table's existence.

    `id` is a deterministic composite `f"{entity_id}::{chunk_index}"`, so re-embedding an entity
    is a clean DELETE-then-INSERT by entity_id (see embedding_service.write_entity_chunk_embeddings).
    See `_entity_chunk_embeddings_ddl`'s docstring for why `entity_id` is a plain auxiliary (`+`)
    column rather than a PARTITION KEY.

    Loads the sqlite_vec extension onto this connection itself (mirrors init_vector_schema and
    every other vec0 call site in this codebase that self-loads defensively rather than assume a
    prior call already attached the extension to this specific connection object -- e.g.
    embed_entity_async). Loading twice on the same connection is a harmless no-op, so calling this
    right after init_vector_schema(conn) (as schema.py's init_db does) costs nothing extra while
    making this function safe to call standalone -- from a test, a future script, or any other
    caller that never went through init_vector_schema first.

    Memory-core rework Phase 4: consolidate_vector_clusters and scout_consolidated_supersessions no
    longer self-load the extension directly here -- both go through
    cohesion_service.get_fresh_entity_centroids, which calls try_load_vector_extension internally
    and degrades to a per-entity fallback (on-demand embedding) rather than raising if the load
    fails, instead of the old bespoke try/except that silently abandoned the whole pass.
    """
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute(_entity_chunk_embeddings_ddl(if_not_exists=True))


def migrate_entity_chunk_embeddings_schema(conn: sqlite3.Connection) -> None:
    """One-time atomic drop+recreate migration bringing an existing entity_chunk_embeddings
    table up to the current DDL (memory-core rework Phase 2 Part A0's `content_hash` column, and
    the later PARTITION KEY removal -- see plans/ and SALTMDB memory `5c09effa` / `3e0c7a1e`).
    Named `..._schema` rather than `..._content_hash` (its original, narrower name) since it now
    detects and fixes two independent legacy-shape conditions with the same drop+recreate
    mechanism, not just one.

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
      - row exists, its `sql` text already mentions `content_hash`, AND it does NOT mention
        `PARTITION KEY` -> already fully migrated, no-op.
      - row exists without `content_hash`, OR row exists and still declares `PARTITION KEY` ->
        needs a real migration: DROP + recreate on the current DDL (which has neither gap). This
        covers three real-world starting shapes with one mechanism: the original Foundation-era
        table (no content_hash, still partitioned), the Phase 2 Part A0 shape (content_hash
        added, still partitioned -- today's actual production shape, per SALTMDB memory
        `76da44ca`), and a hypothetical content_hash-but-unpartitioned shape that never actually
        shipped. Since nothing outside this dev branch depends on any of these old shapes yet
        ("rework" branch, unmerged), dropping and losing existing chunk rows is acceptable here
        -- and it happens to double as the one-time atomic clear of all chunk rows a staleness
        migration would need anyway, so no separate DELETE step.

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
    if row is None:
        return  # doesn't exist yet -- nothing to migrate
    sql = row[0] or ""
    if "content_hash" in sql and "PARTITION KEY" not in sql:
        return  # already fully migrated -- nothing to do

    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("DROP TABLE entity_chunk_embeddings")
    conn.execute(_entity_chunk_embeddings_ddl(if_not_exists=False))
