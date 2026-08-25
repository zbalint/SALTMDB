import sqlite3
import logging
import uuid
from collections import Counter, defaultdict, deque
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying
from saltmdb.utils.text import compute_content_hash
from saltmdb.utils.predicate_vocabulary import (
    AGENT_SELECTABLE_PREDICATES,
    LEGACY_READONLY_PREDICATES,
    PREDICATE_ALIASES,
    RESERVED_PREDICATES,
)

logger = logging.getLogger(__name__)


def _migrate_legacy_lifecycle_invariants(  # noqa: C901, PLR0912
    conn, now: str
) -> dict[str, int]:
    """Repair deterministic lifecycle drift left by pre-v3 releases.

    A supersedes component is migrated only when every node has at most one incoming and one
    outgoing edge. Branching components are intentionally retained for human review.
    """
    current_sql = """
        (valid_from IS NULL OR datetime(valid_from) <= datetime(?))
        AND (valid_to IS NULL OR datetime(valid_to) > datetime(?))
        AND (valid_at IS NULL OR datetime(valid_at) <= datetime(?))
        AND (invalid_at IS NULL OR datetime(invalid_at) > datetime(?))
    """
    current_params = (now, now, now, now)
    self_rows = conn.execute(
        f"SELECT id FROM relations WHERE source_id=target_id AND {current_sql}", current_params
    ).fetchall()
    for (relation_id,) in self_rows:
        conn.execute(
            "UPDATE relations SET invalid_at=?, valid_to=? WHERE id=?", (now, now, relation_id)
        )

    rows = conn.execute(
        f"""SELECT id,source_id,target_id,created_at,valid_from FROM relations
             WHERE predicate='supersedes' AND source_id != target_id AND {current_sql}""",
        current_params,
    ).fetchall()
    incoming = Counter(row[2] for row in rows)
    outgoing = Counter(row[1] for row in rows)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for _rid, source_id, target_id, _created_at, _valid_from in rows:
        adjacency[source_id].add(target_id)
        adjacency[target_id].add(source_id)
    anomalous = {node for node, count in incoming.items() if count > 1} | {
        node for node, count in outgoing.items() if count > 1
    }
    safe_nodes: set[str] = set()
    ambiguous_components = 0
    seen: set[str] = set()
    for start in adjacency:
        if start in seen:
            continue
        component: set[str] = set()
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node in component:
                continue
            component.add(node)
            queue.extend(adjacency[node] - component)
        seen.update(component)
        component_edge_count = sum(1 for row in rows if row[1] in component)
        # Degree limits alone do not prove linearity: A->B plus B->A gives every node one
        # incoming/outgoing edge but is a closed cycle with no canonical winner.  A connected
        # linear component must also have exactly |V|-1 edges.
        if component & anomalous or component_edge_count != len(component) - 1:
            ambiguous_components += 1
        else:
            safe_nodes.update(component)

    from saltmdb.domain.services.embedding_service import (
        cancel_embedding_jobs_for_entity,
        cancel_retrieval_embedding_jobs_for_entity,
        clear_embedding_vectors_for_entity,
    )

    archived = 0
    for _rid, _source_id, target_id, created_at, valid_from in rows:
        if target_id not in safe_nodes:
            continue
        entity = conn.execute(
            "SELECT status,valid_from FROM entities WHERE id=?", (target_id,)
        ).fetchone()
        if not entity or entity[0] not in ("raw", "consolidated"):
            continue
        retired_at = (
            max(value for value in (entity[1], valid_from, created_at) if value)
            if any((entity[1], valid_from, created_at))
            else now
        )
        conn.execute(
            "UPDATE entities SET status='archived',embedding_status='archived',updated_at=?,"
            "valid_to=? WHERE id=? AND status IN ('raw','consolidated')",
            (now, retired_at, target_id),
        )
        cancel_embedding_jobs_for_entity(conn, target_id)
        clear_embedding_vectors_for_entity(conn, target_id, strict=True)
        cancel_retrieval_embedding_jobs_for_entity(conn, target_id, clear_vector=True)
        archived += 1

    conn.execute(
        "UPDATE entities SET valid_to=COALESCE(updated_at,valid_from,created_at) "
        "WHERE status='archived' AND valid_to IS NULL"
    )

    vector_ids: set[str] = set()
    for table in ("entity_embeddings", "entity_chunk_embeddings", "retrieval_embeddings"):
        vector_ids.update(
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT v.entity_id FROM {table} v LEFT JOIN entities e "
                "ON e.id=v.entity_id WHERE e.id IS NULL OR e.status='archived'"
            ).fetchall()
        )
    for entity_id in vector_ids:
        clear_embedding_vectors_for_entity(conn, entity_id, strict=True)
        conn.execute("DELETE FROM retrieval_embeddings WHERE entity_id=?", (entity_id,))

    return {
        "self_relations": len(self_rows),
        "archived_supersedes_targets": archived,
        "ambiguous_supersedes_components": ambiguous_components,
        "cleaned_vector_entities": len(vector_ids),
    }


def _add_column_if_missing(conn, table: str, column_def: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN: swallows the genuine 'already exists' case,
    re-raises anything else so real schema bugs don't vanish silently."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def};")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            raise


def _migrate_predicate_drift(conn, now: str) -> None:  # noqa: C901
    """Agent API redesign plan §7.1 (Phase 8 data migration): rewrites every relation edge whose
    predicate is a known drifted alias (saltmdb.utils.predicate_vocabulary.PREDICATE_ALIASES --
    the SAME 36-name mapping Phase 6's write-time gate already enforces for new writes, so a
    predicate rejected today migrates identically here) onto its canonical spelling, swapping
    source_id/target_id for the 6 aliases whose drifted verb reads the relationship from the
    opposite end (e.g. 'A resolved_by B' means 'B resolves A').

    Active rows (valid_to IS NULL) are protected by a partial UNIQUE index on
    (source_id, target_id, predicate) -- a bare UPDATE that would create a duplicate triple
    aborts the WHOLE statement (reproduced in isolated SQLite during the original plan review),
    so collisions are pre-detected here and resolved by closing the newer row (valid_to = now),
    processed in deterministic rowid (insertion) order -- never by INSERT OR IGNORE/UPDATE OR
    IGNORE, which would silently drop a row and violate the plan's §1.4 information-preservation
    law. Closed rows (valid_to IS NOT NULL) can never collide against the partial index, but
    still get the same rewrite (predicate label, and source/target for swap aliases) -- leaving
    a closed swap row's direction unrewritten would literally assert the reverse of what
    happened (a closed 'A resolved_by B' row relabeled to 'resolves' without swapping would
    claim A resolves B, the opposite of the original fact); the plan is explicit that a
    predicate rename is a vocabulary correction, not a factual change, and must apply to history
    too. Must run inside the caller's own write transaction.
    """
    active_rows = conn.execute(
        "SELECT rowid, id, source_id, target_id, predicate FROM relations WHERE valid_to IS NULL"
    ).fetchall()

    # Every ALREADY-canonical (or otherwise unrecognized, e.g. a genuinely custom pre-existing
    # predicate outside the closed universe) active triple is fixed and claims its slot first,
    # so an alias row that would collide with it is detected correctly regardless of rowid order.
    taken: set[tuple[str, str, str]] = {
        (source_id, target_id, predicate)
        for _, _, source_id, target_id, predicate in active_rows
        if predicate not in PREDICATE_ALIASES
    }

    for rowid, rid, source_id, target_id, predicate in sorted(active_rows, key=lambda r: r[0]):
        if predicate not in PREDICATE_ALIASES:
            continue
        canonical, swap = PREDICATE_ALIASES[predicate]
        new_source, new_target = (target_id, source_id) if swap else (source_id, target_id)
        triple = (new_source, new_target, canonical)
        if triple in taken:
            conn.execute("UPDATE relations SET valid_to = ? WHERE id = ?", (now, rid))
            continue
        conn.execute(
            "UPDATE relations SET source_id = ?, target_id = ?, predicate = ? WHERE id = ?",
            (new_source, new_target, canonical, rid),
        )
        taken.add(triple)

    closed_rows = conn.execute(
        "SELECT id, source_id, target_id, predicate FROM relations WHERE valid_to IS NOT NULL"
    ).fetchall()
    for rid, source_id, target_id, predicate in closed_rows:
        if predicate not in PREDICATE_ALIASES:
            continue
        canonical, swap = PREDICATE_ALIASES[predicate]
        new_source, new_target = (target_id, source_id) if swap else (source_id, target_id)
        conn.execute(
            "UPDATE relations SET source_id = ?, target_id = ?, predicate = ? WHERE id = ?",
            (new_source, new_target, canonical, rid),
        )


def _rebuild_predicate_registry(conn) -> None:
    """Agent API redesign plan §7.2: unconditionally repoints every known alias's canonical_id
    at its correct canonical row. The seed block earlier in init_db() already does this for a
    FRESH database, but only via `INSERT OR IGNORE` plus an `UPDATE ... WHERE canonical_id IS
    NULL` guard -- deliberately soft, to protect a future manual re-merge tool's decision on an
    already-migrated registry. That guard does NOT protect a pre-Phase-6 database, where
    relates_to/references may already carry a STALE canonical_id pointing at elaborates_on (the
    old, reversed alias target) rather than related_to. This is the deliberate, one-time,
    versioned rebuild the plan calls for -- unconditional by design, run once behind
    PRAGMA user_version, never on every init_db() call.
    """
    for alias_name, (canonical_name, _swap) in PREDICATE_ALIASES.items():
        canon_row = conn.execute(
            "SELECT id FROM predicates WHERE name = ?", (canonical_name,)
        ).fetchone()
        if canon_row:
            conn.execute(
                "UPDATE predicates SET canonical_id = ? WHERE name = ? AND id != ?",
                (canon_row[0], alias_name, canon_row[0]),
            )


def _backfill_scd_history_revises_edges(conn, now: str) -> None:
    """Agent API redesign plan §7.3: pre-immutable-identity SCD history rows
    (`<entity_id>_h_<suffix>`, created by the legacy in-place-update writer at
    memory_service/write.py before/around this redesign) predate `revises` edges and are
    otherwise unreachable from `get_lineage`. Recommended remedy per the plan: leave them in
    place (never delete authoritative content, §1.4) and backfill a `revises` edge from the
    live canonical entity to each history snapshot it superseded -- the same direction
    revise_memory's own hardcoded edge uses (new/current -> old/predecessor).

    `revises` is a reserved predicate (Phase 6, §5.8) that only lifecycle tools may create; a
    migration inserting it directly via a hardcoded literal, exactly like revise_memory/
    supersede_memory/consolidate_memories already do, is the established pattern for
    system-created reserved-predicate edges, not a bypass of the write-time gate (which exists
    to stop AGENT-submitted text from forging one).
    """
    history_rows = conn.execute(
        "SELECT id FROM entities WHERE id LIKE '%\\_h\\_%' ESCAPE '\\'"
    ).fetchall()
    for (hist_id,) in history_rows:
        base_id = hist_id.split("_h_", 1)[0]
        if base_id == hist_id:
            continue  # defensive: literal match required '_h_' to be present at all
        base_exists = conn.execute("SELECT 1 FROM entities WHERE id = ?", (base_id,)).fetchone()
        if not base_exists:
            continue  # the canonical entity this snapshot belonged to no longer exists
        conn.execute(
            """
            INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
            SELECT ?, ?, ?, 'revises', ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM relations
                WHERE source_id = ? AND target_id = ? AND predicate = 'revises'
            )
            """,
            (str(uuid.uuid4()), base_id, hist_id, now, now, base_id, hist_id),
        )


def init_db(db_path: str = None) -> sqlite3.Connection:  # noqa: C901, PLR0915
    """Initialize the local SQLite database with Write-Ahead Logging (WAL), DDL tables, triggers, and migrations."""
    if not db_path:
        db_path = get_db_path()

    # Bootstrap is the sole pre-coordinator writer.
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
    def _write(c):  # noqa: C901, PLR0912, PLR0915
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
            "agent_session_id TEXT",
            "last_touched_session_id TEXT",
            "embedding_status TEXT DEFAULT 'pending'",
            "content_hash TEXT",
            "quality_score REAL",
            "quality_status TEXT",
            "quality_flags TEXT",
            "memory_type TEXT CHECK(memory_type IN ('fact','event','procedure','decision','preference')) DEFAULT 'fact'",
            # Optional caller-supplied candidate-generation text.  It is intentionally nullable
            # and hash-separated from the authoritative full_content/content_hash pair.
            "retrieval_text TEXT",
            "retrieval_text_hash TEXT",
            # Core-memory bootstrap governance (see core_governance_service.py, the sole owner of
            # these columns' semantics). All nullable: a demoted normal memory may retain historical
            # lifecycle data, but enforcement/rendering ignore it entirely while is_core=0.
            "core_reason TEXT",
            "core_exit_condition TEXT",
            "core_review_after DATETIME",
            "core_last_reviewed_at DATETIME",
            "core_last_reviewed_by TEXT",
            "core_review_rationale TEXT",
            "core_detail_memory_ids TEXT",  # JSON array of full UUIDs; sole authority for the
            # 3-detail cap -- incidental elaborates_on graph edges are never silently adopted into it
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
            logger.debug(
                "entities.domain column drop skipped (already migrated or unsupported SQLite version): %s",
                e,
            )

        # Schema migration: attempt to add new columns to events table if they don't exist
        for col in ["agent_session_id TEXT", "context_id TEXT"]:
            _add_column_if_missing(conn, "events", col)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_events_session")
            conn.execute("ALTER TABLE events DROP COLUMN " + "session_" + "id")
        except sqlite3.OperationalError as e:
            logger.debug("events legacy session column drop skipped: %s", e)

        # Backfill embedding_status = 'archived' for any archived entities
        try:
            conn.execute(
                "UPDATE entities SET embedding_status = 'archived' WHERE status = 'archived' AND (embedding_status != 'archived' OR embedding_status IS NULL);"
            )
        except sqlite3.OperationalError:
            pass

        # Schema migration: backfill content_hash for legacy entities that predate the column.
        # content_hash was added as a nullable ALTER TABLE column above, so every entity written
        # before that migration ran started (and, until this backfill, stayed) NULL. That NULL
        # is not just cosmetic: embedding_service.write_entity_chunk_embeddings' staleness guard
        # is opt-in on expected_content_hash being non-None, and backfill_chunk_embeddings
        # forwards the entities.content_hash column value verbatim as that argument -- so a NULL
        # here silently disabled the exact stale-write guard the guard exists for, on every
        # legacy entity (Codex re-review finding, Foundation phase). Idempotent and cheap in
        # steady state: matches zero rows once every entity has a hash, since store_memory sets
        # content_hash on every write from this point on.
        try:
            for _eid, _content in conn.execute(
                "SELECT id, full_content FROM entities WHERE content_hash IS NULL OR content_hash = ''"
            ).fetchall():
                conn.execute(
                    "UPDATE entities SET content_hash = ? WHERE id = ?",
                    (compute_content_hash(_content or ""), _eid),
                )
        except sqlite3.OperationalError as e:
            logger.warning("content_hash backfill migration skipped/failed: %s", e)

        # Schema migration: project_id is retired in favor of context_id (kept as a physical
        # column for compatibility, but no longer written/read by application code past this backfill)
        try:
            conn.execute(
                "UPDATE entities SET context_id = project_id WHERE context_id IS NULL AND project_id IS NOT NULL;"
            )
        except sqlite3.OperationalError:
            pass

        # Durable embedding work.  Jobs deliberately have no FK: old releases
        # may retain historical entities, and job diagnostics must remain
        # inspectable even after an administrative cleanup.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS embedding_jobs (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            job_kind TEXT NOT NULL CHECK(job_kind IN ('entity', 'chunk')),
            source_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at DATETIME,
            lease_expires_at DATETIME,
            last_error TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            completed_at DATETIME,
            UNIQUE(entity_id, job_kind, source_hash)
        );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_jobs_due "
            "ON embedding_jobs(state, next_attempt_at, lease_expires_at)"
        )

        # Retrieval-text embeddings have an independent lifecycle from authoritative entity and
        # chunk embeddings.  Keeping a separate table means a caller can replace/clear this
        # optional signal without changing base embedding status or jobs.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_embedding_jobs (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at DATETIME,
            lease_expires_at DATETIME,
            last_error TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            completed_at DATETIME,
            UNIQUE(entity_id, source_hash)
        );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_retrieval_embedding_jobs_due "
            "ON retrieval_embedding_jobs(state, next_attempt_at, lease_expires_at)"
        )

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

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tags_normalized_name ON tags(normalized_name);"
        )

        # Schema migration (alpha.60): seed canonical top-level tags (episodic/semantic/procedural),
        # independent of each other -- no aliasing needed. Mirrors predicates' alpha.55 seeding
        # idempotency (INSERT OR IGNORE); canonical_id stays NULL, these ARE the canonical rows.
        for _seed_tag_name in ("episodic", "semantic", "procedural"):
            conn.execute(
                "INSERT OR IGNORE INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (str(uuid.uuid4()), _seed_tag_name, _seed_tag_name),
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
        #
        # Scoped to `valid_to IS NULL` (agent API redesign Phase 8 fix): the UNIQUE index this
        # backfill protects is itself PARTIAL (`WHERE valid_to IS NULL`, see below), so only
        # ACTIVE rows can ever collide against it -- deduping closed rows too was never necessary
        # for that index to succeed, and is actively destructive: a closed (historical) row can
        # legitimately share an identical (source_id, target_id, predicate) triple with an
        # unrelated active row (e.g. after §7.1's predicate-vocabulary migration renames a
        # collision-losing active row and closes it, its triple now matches the active winner's)
        # or with another closed row from a different point in time (created, invalidated,
        # later recreated, invalidated again). The original unscoped DELETE would silently drop
        # one of those on the very next startup, violating §1.4 (information is never lost) --
        # caught via test_predicate_migration.py's post-migration idempotency check.
        try:
            conn.execute("""
                DELETE FROM relations
                WHERE valid_to IS NULL
                  AND rowid NOT IN (
                    SELECT MIN(rowid) FROM relations
                    WHERE valid_to IS NULL
                    GROUP BY source_id, target_id, predicate
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
        # Closed predicate vocabulary (agent API redesign plan §5.8, Phase 6 item 25): the 15
        # canonical spellings (11 agent-selectable + 3 reserved/system-owned + 1 legacy-
        # read-only) plus every drifted spelling from the §7.1 migration mapping, seeded as
        # aliases of their canonical target. Mirrors saltmdb.utils.predicate_vocabulary exactly
        # -- that module is the single source of truth this block renders into the DB registry,
        # so search_tags/list_predicates and this seed can never drift apart.
        _canonical_pred_names = (
            *sorted(AGENT_SELECTABLE_PREDICATES),
            *sorted(RESERVED_PREDICATES),
            *sorted(LEGACY_READONLY_PREDICATES),
        )
        for _pred_name in _canonical_pred_names:
            conn.execute(
                "INSERT OR IGNORE INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (str(uuid.uuid4()), _pred_name, _pred_name),
            )
        for _alias_name, (_canon_name, _swap) in PREDICATE_ALIASES.items():
            conn.execute(
                "INSERT OR IGNORE INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                (str(uuid.uuid4()), _alias_name, _alias_name),
            )
            # Alias onto the canonical row. Guarded by canonical_id IS NULL so a future manual
            # re-merge tool's decision is never silently clobbered on restart -- this is a seed
            # default, not an unconditional re-assertion.
            _canon_row = conn.execute(
                "SELECT id FROM predicates WHERE name = ?", (_canon_name,)
            ).fetchone()
            if _canon_row:
                conn.execute(
                    "UPDATE predicates SET canonical_id = ? WHERE name = ? AND canonical_id IS NULL AND id != ?",
                    (_canon_row[0], _alias_name, _canon_row[0]),
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

        # Dedicated optional retrieval-text FTS index.  It intentionally contains no
        # authoritative title/content columns, and all writes are maintained transactionally by
        # the triggers below.  A best-effort backfill keeps upgrades idempotent.
        try:
            retrieval_cols = [
                r[1] for r in conn.execute("PRAGMA table_info(retrieval_fts)").fetchall()
            ]
            if not retrieval_cols or "retrieval_text" not in retrieval_cols:
                conn.execute("DROP TABLE IF EXISTS retrieval_fts")
                conn.execute("""
                CREATE VIRTUAL TABLE retrieval_fts USING fts5(
                    id UNINDEXED,
                    retrieval_text,
                    tokenize='porter'
                );
                """)
                conn.execute("""
                INSERT INTO retrieval_fts (id, retrieval_text)
                SELECT id, retrieval_text FROM entities
                WHERE status != 'archived' AND retrieval_text IS NOT NULL AND retrieval_text != '';
                """)
        except sqlite3.OperationalError:
            # FTS5 is available in supported SQLite builds; preserve the existing graceful
            # startup posture for unusual builds that omit it.
            pass

        from saltmdb.db.vector_schema import (
            init_vector_schema,
            init_entity_chunk_vector_schema,
            init_retrieval_vector_schema,
            migrate_entity_chunk_embeddings_schema,
        )

        # Codex-mandated (Phase 2 Part A0): this destructive drop+recreate migration must NOT
        # be swallowed by the broad best-effort try/except below. That catch-all exists for
        # Foundation's "vector features are best-effort, degrade gracefully" posture, which is
        # fine for a fresh CREATE ... IF NOT EXISTS but wrong for a destructive drop+recreate --
        # silently swallowing a failed migration here could leave a committed database with NO
        # entity_chunk_embeddings table at all. A failure here aborts init_db() loudly instead.
        # Runs inside this same _write closure / write_transaction_retrying transaction, so a
        # raised exception here still triggers the outer ROLLBACK -- the DROP never commits
        # without its recreate landing in the same transaction. Also covers the later
        # PARTITION KEY removal migration (SALTMDB memory `3e0c7a1e`) -- same drop+recreate
        # mechanism, same non-swallowed placement, now detecting either legacy-shape condition.
        migrate_entity_chunk_embeddings_schema(conn)

        try:
            init_vector_schema(conn)
            init_entity_chunk_vector_schema(conn)
            init_retrieval_vector_schema(conn)
        except Exception as e:
            logger.warning("Vector schema init deferred/failed: %s", e)

        # 5b. Usage telemetry (agent API redesign plan §5.9): a separate sink from `events`,
        # written automatically by the daemon on every dispatched tool call, never by an agent.
        # Metadata only -- tool name, which parameter names were present, result status, error
        # code, latency -- NEVER argument values (memory content routinely contains secrets; the
        # store's redaction posture assumes content passes through middleware, not a raw call
        # log). Strictly local, CLI-readable, not an MCP tool. No FK to entities/events -- a
        # telemetry row must remain inspectable even after the entity/event it describes is
        # gone, exactly like embedding_jobs' own no-FK rationale above.
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_call_telemetry (
            id TEXT PRIMARY KEY,
            timestamp DATETIME NOT NULL,
            tool_name TEXT NOT NULL,
            owner_id TEXT,
            param_names TEXT NOT NULL, -- JSON array of parameter names present in the call, never values
            status TEXT NOT NULL,
            error_code TEXT,
            latency_ms REAL NOT NULL
        );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_call_telemetry_tool_time "
            "ON tool_call_telemetry(tool_name, timestamp DESC)"
        )

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

        # 6b. Viewer Sessions Table for Reference-Counted Lifecycle
        conn.execute("""
        CREATE TABLE IF NOT EXISTS _viewer_sessions (
            port INTEGER NOT NULL,
            session_pid INTEGER NOT NULL,
            started_at DATETIME NOT NULL,
            PRIMARY KEY (port, session_pid)
        );
        """)

        # 6c. Agent Sessions Table for Last Session Bootstrap Digest
        conn.execute("""
        CREATE TABLE IF NOT EXISTS _agent_sessions (
            session_id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            started_at DATETIME NOT NULL
        );
        """)
        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_cwd_started
            ON _agent_sessions(cwd, started_at DESC);
        """)

        # Drop old triggers to recreate with search_aliases support
        conn.execute("DROP TRIGGER IF EXISTS insert_entity_fts")
        conn.execute("DROP TRIGGER IF EXISTS update_entity_fts")
        conn.execute("DROP TRIGGER IF EXISTS update_entity_fts_unarchived")
        conn.execute("DROP TRIGGER IF EXISTS insert_retrieval_fts")
        conn.execute("DROP TRIGGER IF EXISTS update_retrieval_fts")
        conn.execute("DROP TRIGGER IF EXISTS archive_retrieval_fts")
        conn.execute("DROP TRIGGER IF EXISTS delete_retrieval_fts")
        conn.execute("DROP TRIGGER IF EXISTS delete_retrieval_embedding_jobs")
        # Older development revisions briefly created vec0-referencing triggers.  Remove them
        # idempotently: ordinary daemon/read connections do not load sqlite-vec, so such triggers
        # make even unrelated entity INSERT/UPDATE statements fail with "no such module: vec0".
        conn.execute("DROP TRIGGER IF EXISTS archive_retrieval_embedding_vector")
        conn.execute("DROP TRIGGER IF EXISTS delete_retrieval_embedding_vector")

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

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS insert_retrieval_fts
        AFTER INSERT ON entities
        WHEN NEW.status != 'archived' AND NEW.retrieval_text IS NOT NULL AND NEW.retrieval_text != ''
        BEGIN
            INSERT INTO retrieval_fts(id, retrieval_text) VALUES (NEW.id, NEW.retrieval_text);
        END;
        """)

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS update_retrieval_fts
        AFTER UPDATE ON entities
        WHEN NEW.status != 'archived' AND OLD.status != 'archived'
        BEGIN
            DELETE FROM retrieval_fts WHERE id = OLD.id;
            INSERT INTO retrieval_fts(id, retrieval_text)
            SELECT NEW.id, NEW.retrieval_text
            WHERE NEW.retrieval_text IS NOT NULL AND NEW.retrieval_text != '';
        END;
        """)

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS archive_retrieval_fts
        AFTER UPDATE ON entities
        WHEN NEW.status = 'archived'
        BEGIN
            DELETE FROM retrieval_fts WHERE id = OLD.id;
        END;
        """)

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_retrieval_fts
        AFTER DELETE ON entities
        BEGIN
            DELETE FROM retrieval_fts WHERE id = OLD.id;
        END;
        """)

        # vec0 rows have no ordinary FK and are cleaned by the domain archive/delete paths.  The
        # job trigger below has no sqlite-vec dependency, so ordinary SQLite-only installations
        # retain lifecycle maintenance instead of failing writes when the optional extension is
        # unavailable.
        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS delete_retrieval_embedding_jobs
        AFTER DELETE ON entities
        BEGIN
            UPDATE retrieval_embedding_jobs
            SET state='cancelled', updated_at=CURRENT_TIMESTAMP, completed_at=CURRENT_TIMESTAMP,
                lease_expires_at=NULL
            WHERE entity_id=OLD.id AND state IN ('queued','running','retry_wait');
        END;
        """)

        # Performance indexes for high-traffic filtering columns
        for index_sql in [
            "CREATE INDEX IF NOT EXISTS idx_entities_status_updated ON entities(status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_entities_active_title ON entities(title) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_owner_scope ON entities(owner_id, scope)",
            "CREATE INDEX IF NOT EXISTS idx_entities_context ON entities(context_id)",
            "CREATE INDEX IF NOT EXISTS idx_entities_embedding ON entities(embedding_status) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_is_core ON entities(is_core) WHERE is_core = 1",
            # Additive, not a replacement for idx_entities_is_core above (that index does not
            # filter archived rows). Backs core_governance_service.load_active_cores' ordering
            # (overdue first, earliest upcoming review) over exactly the active-core set.
            "CREATE INDEX IF NOT EXISTS idx_entities_core_review "
            "ON entities(core_review_after, created_at) WHERE is_core = 1 AND status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_content_hash ON entities(owner_id, content_hash) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_retrieval_text_hash ON entities(retrieval_text_hash) WHERE status != 'archived' AND retrieval_text_hash IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_entities_memory_type ON entities(memory_type) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_entities_agent_session ON entities(agent_session_id, created_at DESC) WHERE status != 'archived'",
            "CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_id, type, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_agent_session ON events(agent_session_id, timestamp DESC)",
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

        # Track A store-time disposition rewrite (memory-core rework, see
        # scratch/plans/track_a_disposition_detailed.md §5): one-time retirement of the legacy
        # `consolidation_request`/`supersession_candidate` event backlog, now that store_memory no
        # longer emits either type. Reuses the existing `dismiss_events` mechanism (an
        # `event_dismissed` audit record per event, reason="track_a_migration") purely as a
        # historical audit trail -- agent API redesign Phase 6 removed both the public
        # `dismiss_event` MCP tool and `get_recent_events`' dismissed-suppression/status-
        # derivation logic (§3.5), so this sweep no longer has any effect on what `get_events`
        # returns; `dismiss_events` itself is kept only because this sweep still calls it.
        # Gated on PRAGMA user_version (unused elsewhere in this codebase) so this genuinely runs
        # once, ever, per DB -- NOT on every init_db() call -- otherwise it would silently
        # auto-dismiss any future legitimate event of either type too, not just this one-time
        # legacy backlog (Codex Track-A plan review round 2 finding). A crash mid-sweep leaves
        # user_version at 0 and the sweep safely retries next startup; a completed sweep never
        # re-runs and never touches a future event again.
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
                legacy_ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM events WHERE type IN ('consolidation_request', 'supersession_candidate')"
                    ).fetchall()
                ]
                if legacy_ids:
                    from saltmdb.domain.services.event_service import dismiss_events

                    dismiss_events(
                        event_ids=legacy_ids,
                        reason="track_a_migration",
                        agent_id="system",
                        db_connection=conn,
                        _in_transaction=True,
                    )
                conn.execute("PRAGMA user_version = 1")
        except sqlite3.OperationalError as e:
            logger.warning(
                "Track A migration sweep skipped/failed (will retry next startup): %s", e
            )

        # Agent API redesign plan §7 (Phase 8 data migration): closed predicate vocabulary --
        # rewrite every drifted relation edge onto its canonical spelling (§7.1), rebuild the
        # canonical predicate registry (§7.2), and backfill revises edges for pre-immutable-
        # identity SCD history rows (§7.3). Ships as a `user_version = 2` block inside init_db(),
        # never a hand-run script (§7.0): a script migrates one database and nothing else, not
        # third-party clones, not temp test DBs; running inside init_db() also solves daemon
        # concurrency for free (the daemon holds the DB open and serializes writes through the
        # coordinator, so this runs at the right moment by construction). Same crash-safety shape
        # as the Track A sweep above: a crash mid-migration leaves user_version at 1 and the
        # whole block safely retries next startup; a completed migration never re-runs.
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
                from datetime import UTC, datetime

                now = datetime.now(UTC).isoformat()
                _migrate_predicate_drift(conn, now)
                _rebuild_predicate_registry(conn)
                _backfill_scd_history_revises_edges(conn, now)
                conn.execute("PRAGMA user_version = 2")
        except sqlite3.OperationalError as e:
            logger.warning(
                "Phase 8 predicate-vocabulary migration skipped/failed (will retry next "
                "startup): %s",
                e,
            )

        # v3 repairs deterministic lifecycle drift from releases that allowed agents to create
        # supersedes edges directly and retained vectors after archival. Ambiguous graph branches
        # are reported but never guessed at automatically.
        try:
            # Never leapfrog a failed prerequisite migration.  The earlier blocks deliberately
            # catch OperationalError so startup can continue; an exact gate is therefore needed
            # to prevent v3 from marking an unfinished v1/v2 database as fully migrated.
            if conn.execute("PRAGMA user_version").fetchone()[0] == 2:
                from datetime import UTC, datetime

                now = datetime.now(UTC).isoformat()
                conn.execute("SAVEPOINT lifecycle_invariants_v3")
                try:
                    # CREATE INDEX IF NOT EXISTS does not update an existing definition.  Some
                    # upgraded databases therefore still have the retired two-column shape
                    # (context_id, project_id).  Rebuild it inside the versioned/savepointed
                    # migration so the live schema actually matches fresh databases.
                    conn.execute("DROP INDEX IF EXISTS idx_entities_context")
                    conn.execute("CREATE INDEX idx_entities_context ON entities(context_id)")
                    summary = _migrate_legacy_lifecycle_invariants(conn, now)
                    conn.execute("PRAGMA user_version = 3")
                except Exception:
                    # The surrounding OperationalError handler intentionally permits startup to
                    # continue.  Roll this migration back first so that retryability does not
                    # mean committing a half-archived/half-cleaned database.
                    conn.execute("ROLLBACK TO lifecycle_invariants_v3")
                    conn.execute("RELEASE lifecycle_invariants_v3")
                    raise
                else:
                    conn.execute("RELEASE lifecycle_invariants_v3")
                if summary["ambiguous_supersedes_components"]:
                    logger.warning(
                        "Lifecycle migration left %d ambiguous supersedes components for review",
                        summary["ambiguous_supersedes_components"],
                    )
                logger.info("Lifecycle invariant migration completed: %s", summary)
        except sqlite3.OperationalError as e:
            logger.warning(
                "Lifecycle invariant migration skipped/failed (will retry next startup): %s", e
            )

    write_transaction_retrying(conn, _write)
    return conn
