"""Entity lifecycle operations for memory_service: fetch, touch, archive, orphan scan.

Pure code-motion extraction (see refactor plan). No cross-module dependencies
within this package.
"""

from datetime import datetime, UTC

from saltmdb.config import get_db_path
from saltmdb.db.connection import (
    get_connection,
    is_coordinator_connection,
    write_transaction_retrying,
    close_connection,
)
from saltmdb.utils.text import resolve_entity_id, resolve_id_prefix
from saltmdb.utils.text import resolve_entity_ref
from saltmdb.utils.envelope import error as envelope_error, ok as envelope_ok, rejected

from ._shared import logger


def _lineage_nodes(result, direction: str) -> list[dict]:
    """Extract the node list from either graph-service lineage shape.

    Phase 3 changes the graph API from ``analyze_lineage()['ancestors']`` to the
    direction-specific ``get_lineage`` result.  Keeping this small adapter here lets
    explicit-memory reads remain compatible while the daemon/viewer callers migrate.
    """
    if not isinstance(result, dict) or result.get("error"):
        return []
    candidates = result.get("nodes")
    if candidates is None:
        candidates = result.get(direction)
    if candidates is None:
        candidates = result.get("ancestors" if direction == "ancestors" else "descendants")
    if not isinstance(candidates, list):
        return []
    return [node for node in candidates if isinstance(node, dict)]


def _memory_lineage(entity_id: str, conn, max_depth: int = 10) -> dict[str, list[dict]]:
    """Read both lifecycle directions without ever substituting a successor.

    ``get_lineage`` is the Phase 3 relation-service entry point.  The fallback is
    intentionally limited to the pre-Phase-3 ancestor API so old in-process callers
    keep working during a rolling upgrade; it does not redirect the requested entity.
    """
    from saltmdb.domain.services import relation_service

    get_lineage = getattr(relation_service, "get_lineage", None)
    if get_lineage is not None:
        ancestors = get_lineage(
            entity_id=entity_id,
            direction="ancestors",
            max_depth=max_depth,
            db_connection=conn,
        )
        descendants = get_lineage(
            entity_id=entity_id,
            direction="descendants",
            max_depth=max_depth,
            db_connection=conn,
        )
    else:
        ancestors = relation_service.analyze_lineage(entity_id=entity_id, db_connection=conn)
        descendants = {}
    return {
        # The legacy ancestor response includes the addressed root at depth zero;
        # explicit-memory lineage describes neighbours, so omit that duplicate.
        "ancestors": [
            node for node in _lineage_nodes(ancestors, "ancestors") if node.get("id") != entity_id
        ],
        "descendants": [
            node
            for node in _lineage_nodes(descendants, "descendants")
            if node.get("id") != entity_id
        ],
    }


def get_memory(
    entity_id: str = None,
    db_connection=None,
    db_path: str = None,
    *,
    max_depth: int = 10,
) -> dict:
    """Return one explicitly addressed memory, including archived history.

    Unlike search and the old ``fetch_memory_chunk`` path, this operation always
    returns a structured envelope and never follows a successor.  Prefix resolution
    is delegated to :func:`resolve_entity_ref`, so ambiguous IDs are actionable and
    do not accidentally disclose one of several matching memories.
    """
    if not entity_id or not isinstance(entity_id, str) or not entity_id.strip():
        return rejected(
            [envelope_error("MISSING_ENTITY_ID", "entity_id is mandatory.", "entity_id")]
        )

    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(db_path or get_db_path())
        should_close = True

    try:
        resolved_id, candidates, truncated = resolve_entity_ref(conn, entity_id)
        if candidates:
            item = envelope_error(
                "AMBIGUOUS_ID_PREFIX",
                f"ID prefix '{entity_id}' matches multiple memories; provide a longer prefix or full UUID.",
                "entity_id",
            )
            item["candidates"] = candidates
            if truncated:
                item["candidates_truncated"] = True
            return rejected([item])
        if not resolved_id:
            return rejected(
                [
                    envelope_error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{entity_id}'.",
                        "entity_id",
                    )
                ]
            )

        row = conn.execute(
            """
            SELECT id, title, full_content, status, created_at, updated_at,
                   last_accessed_at, owner_id, scope, is_core, parent_ids,
                   valid_from, valid_to, metadata, context_id, memory_type,
                   quality_score, quality_status, quality_flags
            FROM entities WHERE id = ?
            """,
            (resolved_id,),
        ).fetchone()
        if not row:
            return rejected(
                [
                    envelope_error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{entity_id}'.",
                        "entity_id",
                    )
                ]
            )

        # Explicit retrieval is still a memory access. Preserve the old
        # search_memory(entity_id=...) contract's access-time bookkeeping while
        # moving the read to its dedicated Phase 3 tool.
        accessed_at = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE entities SET last_accessed_at = ? WHERE id = ?",
            (accessed_at, resolved_id),
        )
        if not is_coordinator_connection(conn):
            conn.commit()

        lineage = _memory_lineage(resolved_id, conn, max_depth=max_depth)
        data = {
            "id": row[0],
            "title": row[1],
            "content": row[2],
            "status": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "last_accessed_at": accessed_at,
            "owner_id": row[7],
            "scope": row[8],
            "is_core": bool(row[9]),
            "parent_ids": row[10],
            "valid_from": row[11],
            "valid_to": row[12],
            "metadata": row[13],
            "context_id": row[14],
            "memory_type": row[15],
            "quality_score": row[16],
            "quality_status": row[17],
            "quality_flags": row[18],
            "lineage": lineage,
        }
        return envelope_ok(data)
    except Exception as exc:
        logger.error("Error getting memory: %s", exc)
        return rejected([envelope_error("MEMORY_READ_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def fetch_memory_chunk(  # noqa: C901, PLR0911
    entity_id: str = None, db_connection=None, db_path: str = None, *, touch: bool = True
) -> str:
    """Returns full markdown text of a memory."""
    if not entity_id:
        return "Error: entity_id is mandatory."
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    def _load_and_touch(id_val):
        cursor = conn.execute(
            """
            SELECT id, title, full_content, status, created_at, updated_at, owner_id, scope, metadata
            FROM entities WHERE id = ?
        """,
            (id_val,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        if touch:
            now = datetime.now(UTC).isoformat()
            conn.execute("UPDATE entities SET last_accessed_at = ? WHERE id = ?", (now, id_val))
            if not is_coordinator_connection(conn):
                conn.commit()
        return row[2]

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return f"Error: Could not resolve memory entity for input '{entity_id}'."

        content = _load_and_touch(resolved_id)
        if content is not None:
            return content

        # Fallback: short hex-prefix resolution (e.g. "77aef47e" -> full UUID). Only reached
        # when the exact/UUID/title resolution above already missed -- precedence unchanged.
        prefix_id, candidates, truncated = resolve_id_prefix(conn, entity_id)
        if candidates:
            lines = [f"  {c['id']} — {c['title']!r} [{c['status']}]" for c in candidates]
            return (
                f"Error: Ambiguous ID prefix '{entity_id}' matches "
                f"{len(candidates)}{'+' if truncated else ''} memories:\n" + "\n".join(lines)
            )
        if prefix_id:
            content = _load_and_touch(prefix_id)
            if content is not None:
                return content

        return f"Memory not found for ID: {resolved_id}"
    except Exception as e:
        logger.error("Error fetching memory chunk: %s", e)
        return f"Error fetching memory chunk: {e}"
    finally:
        if should_close:
            close_connection(conn)


def touch_memory_access(entity_id: str, db_connection) -> None:
    """Writer-side half of a fetch access-time touch."""
    resolved_id = resolve_entity_id(db_connection, entity_id)
    target_id = resolved_id
    if resolved_id:
        cursor = db_connection.execute("SELECT 1 FROM entities WHERE id = ?", (resolved_id,))
        if not cursor.fetchone():
            # Exact/UUID/title resolution didn't land on a real row -- try short-prefix
            # resolution. An ambiguous prefix (or no match) resolves to None here, which
            # is a deliberate silent no-op below: never raises, never guesses a candidate.
            prefix_id, _candidates, _truncated = resolve_id_prefix(db_connection, entity_id)
            target_id = prefix_id
    if target_id:
        db_connection.execute(
            "UPDATE entities SET last_accessed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), target_id),
        )


def _archive_entity_unchecked(conn, resolved_id: str) -> None:
    """The archival write itself, with NO ownership/status-guard checks -- callers are
    responsible for deciding whether this id may be archived. Must run inside an already-open
    write transaction (the caller's own BEGIN IMMEDIATE), never opens its own.

    Extracted out of archive_memory's `_do_archive` closure (memory-core rework, core-governance
    plan resolved gap #3) so review_core_memory's `archive` outcome can reuse the exact same
    archival mechanics through an ownership-NEUTRAL path -- a reviewing agent's owner_id need not
    match the entity's own owner_id, which archive_memory's public ownership-mismatch guard would
    otherwise incorrectly reject. archive_memory itself keeps that guard for its own callers
    (unchanged below); only this internal body is shared.
    """
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE entities
        SET status = 'archived', embedding_status = 'archived', updated_at = ?, valid_to = ?
        WHERE id = ? AND status != 'archived'
    """,
        (now, now, resolved_id),
    )
    conn.execute(
        """
        UPDATE relations
        SET valid_to = ?
        WHERE (source_id = ? OR target_id = ?) AND valid_to IS NULL
    """,
        (now, resolved_id, resolved_id),
    )
    from saltmdb.domain.services.embedding_service import (
        cancel_embedding_jobs_for_entity,
        cancel_retrieval_embedding_jobs_for_entity,
    )

    cancel_embedding_jobs_for_entity(conn, resolved_id)
    cancel_retrieval_embedding_jobs_for_entity(conn, resolved_id, clear_vector=True)


def archive_memory(  # noqa: PLR0911
    entity_id: str = None,
    owner_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Explicitly archives (retires) a long-term memory.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_archive_memory, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).
    """
    if not entity_id:
        return "Error: entity_id parameter is mandatory."
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return f"Error: Could not resolve entity '{entity_id}'"

        cursor = conn.execute(
            "SELECT owner_id, scope, status FROM entities WHERE id = ?", (resolved_id,)
        )
        row = cursor.fetchone()
        if not row:
            return f"Error: Memory '{resolved_id}' not found."

        existing_owner, scope, status = row
        if status == "archived":
            return f"Memory '{resolved_id}' is already archived."
        if owner_id and existing_owner and existing_owner != owner_id:
            return f"Error: Memory '{resolved_id}' owner mismatch."

        if _in_transaction:
            _archive_entity_unchecked(conn, resolved_id)
        else:

            def _write(c):
                _archive_entity_unchecked(conn, resolved_id)

            write_transaction_retrying(conn, _write)

        return f"Memory '{resolved_id}' was successfully archived."
    except Exception as e:
        logger.error("Error archiving memory: %s", e)
        return f"Error archiving memory: {e}"
    finally:
        if should_close:
            close_connection(conn)


def detect_orphaned_memories(owner_id: str = None, db_connection=None, db_path: str = None) -> dict:
    """Identifies active memories with zero relationship links."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        query = """
        SELECT e.id, e.title, e.owner_id
        FROM entities e
        LEFT JOIN relations r ON (e.id = r.source_id OR e.id = r.target_id) AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
        WHERE e.status = 'raw' AND r.id IS NULL
        """
        params = []
        if owner_id:
            query += " AND e.owner_id = ?"
            params.append(owner_id)

        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

        orphans = []
        for r in rows:
            orphans.append({"id": r[0], "title": r[1], "owner_id": r[2]})

        return {
            "total_orphans": len(orphans),
            "orphaned_memories": orphans,
        }
    except Exception as e:
        logger.error("Error detecting orphans: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)
