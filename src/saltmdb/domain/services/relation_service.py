import uuid
import json
import logging
import re
from datetime import datetime, UTC
from typing import Any, Literal
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import resolve_entity_id, compute_content_hash
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.domain.services.memory_service import check_duplicate_memories, resolve_or_create_tag

logger = logging.getLogger(__name__)


def _normalize_predicate_name(raw: str) -> str:
    """Shape-normalizes a predicate string (lowercase, non-alnum runs -> underscore, trimmed)."""
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")


def resolve_or_create_predicate(conn, predicate_name: str, agent_id: str = None) -> str | None:
    """Write-time predicate canonicalization. Must be called inside an open write transaction
    (mirrors resolve_or_create_tag's contract). Returns the resolved CANONICAL NAME STRING to
    store directly in relations.predicate (not a row id -- predicate is free text, no FK).

    Non-blocking: an unrecognized predicate is always auto-created and returned, never rejected.
    Returns None only when input has no salvageable characters after normalization -- caller
    falls back to the raw input string.

    Simpler than resolve_or_create_tag: no '#'-prefix handling, no plural/suffix fallback (seed
    vocabulary is short and already snake_case; a suffix heuristic risks false merges like
    resolves/resolved with no observed drift evidence to justify it).
    """
    raw = (predicate_name or "").strip()
    if not raw:
        return None
    normalized = _normalize_predicate_name(raw)
    if not normalized:
        return None

    row = conn.execute(
        "SELECT p.name, c.name FROM predicates p LEFT JOIN predicates c ON c.id = p.canonical_id "
        "WHERE p.name = ?",
        (normalized,),
    ).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    row = conn.execute(
        "SELECT p.name, c.name FROM predicates p LEFT JOIN predicates c ON c.id = p.canonical_id "
        "WHERE p.normalized_name = ?",
        (normalized,),
    ).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    conn.execute(
        "INSERT OR IGNORE INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
        (str(uuid.uuid4()), normalized, normalized),
    )
    row = conn.execute(
        "SELECT p.name, c.name FROM predicates p LEFT JOIN predicates c ON c.id = p.canonical_id "
        "WHERE p.name = ?",
        (normalized,),
    ).fetchone()
    if row:
        return row[1] if row[1] else row[0]
    return normalized


def get_canonical_predicates(
    query: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> list:
    """Mirrors memory_service.get_canonical_tags for the predicates table."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
    try:
        if query:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL AND name LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            )
        else:
            cursor = conn.execute(
                "SELECT id, name FROM predicates WHERE canonical_id IS NULL LIMIT ?", (limit,)
            )
        return [{"id": r[0], "name": r[1]} for r in cursor.fetchall()]
    except Exception as e:
        logger.error("Error fetching canonical predicates: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def store_relation(  # noqa: C901
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    valid_at: str | None = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Stores a directional relationship edge between two knowledge entities.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_store_relations, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).
    """
    if not source_id or not target_id or not predicate:
        return "Error: source_id, target_id, and predicate are mandatory parameters."

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    resolved_source = resolve_entity_id(conn, source_id)
    resolved_target = resolve_entity_id(conn, target_id)

    if not resolved_source or not resolved_target:
        if should_close:
            close_connection(conn)
        return "Error: Could not resolve target entity IDs."

    if resolved_source == resolved_target:
        if should_close:
            close_connection(conn)
        return "Error: Self-referential relations (source_id == target_id) are forbidden."

    relation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:

        def _do_store():
            normalized_requested = _normalize_predicate_name(predicate)
            canonical_predicate = resolve_or_create_predicate(conn, predicate) or predicate
            note = (
                f" [canonicalized: requested '{predicate}', stored as '{canonical_predicate}']"
                if predicate
                and normalized_requested
                and normalized_requested != canonical_predicate
                else ""
            )
            effective_valid_at = valid_at or now
            cursor = conn.execute(
                """
                INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, predicate) WHERE valid_to IS NULL DO NOTHING
            """,
                (
                    relation_id,
                    resolved_source,
                    resolved_target,
                    canonical_predicate,
                    now,
                    now,
                    effective_valid_at,
                ),
            )
            if cursor.rowcount == 0:
                existing = conn.execute(
                    "SELECT id FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ? AND valid_to IS NULL",
                    (resolved_source, resolved_target, canonical_predicate),
                ).fetchone()
                existing_id = existing[0] if existing else relation_id
                return f"Relation already exists (no-op): '{canonical_predicate}' between {resolved_source} and {resolved_target} (ID: {existing_id}){note}"
            return f"Relation successfully stored: '{canonical_predicate}' between {resolved_source} and {resolved_target} (ID: {relation_id}){note}"

        if _in_transaction:
            result_msg = _do_store()
        else:

            def _write(c):
                return _do_store()

            result_msg = write_transaction_retrying(conn, _write)
        return result_msg
    except Exception as e:
        logger.error("Error storing relation: %s", e)
        return f"Error storing relation: {e}"
    finally:
        if should_close:
            close_connection(conn)


def invalidate_relation(  # noqa: C901
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    invalid_at: str | None = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Invalidates an active relationship edge on the event/world-time axis (invalid_at).

    Does NOT touch valid_to (system/transaction time, driven by commit_consolidation).
    """
    if not source_id or not target_id or not predicate:
        return "Error: source_id, target_id, and predicate are mandatory parameters."

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    resolved_source = resolve_entity_id(conn, source_id)
    resolved_target = resolve_entity_id(conn, target_id)

    if not resolved_source or not resolved_target:
        if should_close:
            close_connection(conn)
        return "Error: Could not resolve target entity IDs."

    now = datetime.now(UTC).isoformat()
    try:

        def _do_invalidate():
            normalized_requested = _normalize_predicate_name(predicate)
            canonical_predicate = resolve_or_create_predicate(conn, predicate) or predicate
            note = (
                f" [canonicalized: requested '{predicate}', stored as '{canonical_predicate}']"
                if predicate
                and normalized_requested
                and normalized_requested != canonical_predicate
                else ""
            )
            existing = conn.execute(
                "SELECT id, invalid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ? AND valid_to IS NULL",
                (resolved_source, resolved_target, canonical_predicate),
            ).fetchone()
            if not existing:
                existing = conn.execute(
                    "SELECT id, invalid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ? AND invalid_at IS NOT NULL ORDER BY rowid DESC",
                    (resolved_source, resolved_target, canonical_predicate),
                ).fetchone()
            if not existing:
                return "Error: relation not found"

            rel_id, existing_invalid_at = existing
            if existing_invalid_at is not None:
                return (
                    f"Relation already invalidated (no-op) at {existing_invalid_at} (ID: {rel_id})"
                )

            effective_invalid_at = invalid_at or now
            conn.execute(
                "UPDATE relations SET invalid_at = ?, valid_to = ? WHERE id = ?",
                (effective_invalid_at, effective_invalid_at, rel_id),
            )
            return f"Relation invalidated: '{canonical_predicate}' between {resolved_source} and {resolved_target} at {effective_invalid_at} (ID: {rel_id}){note}"

        if _in_transaction:
            result_msg = _do_invalidate()
        else:

            def _write(c):
                return _do_invalidate()

            result_msg = write_transaction_retrying(conn, _write)
        return result_msg
    except Exception as e:
        logger.error("Error invalidating relation: %s", e)
        return f"Error invalidating relation: {e}"
    finally:
        if should_close:
            close_connection(conn)


def analyze_dependencies(
    root_entity_id: str = None,
    max_depth: int = 5,
    point_in_time: str = None,
    db_connection=None,
    db_path: str = None,
) -> dict:
    """Recursively traces downstream relational paths using SQL CTEs."""
    if not root_entity_id:
        return {"error": "root_entity_id is mandatory"}

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    root_id = resolve_entity_id(conn, root_entity_id)
    if not root_id:
        if should_close:
            close_connection(conn)
        return {"error": f"Could not resolve entity '{root_entity_id}'"}

    pit = point_in_time or datetime.now(UTC).isoformat()

    try:
        cursor = conn.execute("SELECT id, title, status FROM entities WHERE id = ?", (root_id,))
        root_row = cursor.fetchone()
        root_info = (
            {"id": root_row[0], "title": root_row[1], "status": root_row[2]}
            if root_row
            else {"id": root_id, "title": "Root", "status": "raw"}
        )

        query = """
        WITH RECURSIVE dependency_tree(id, source_id, target_id, predicate, depth, path) AS (
            SELECT r.id, r.source_id, r.target_id, r.predicate, 1, r.source_id || '->' || r.target_id
            FROM relations r
            WHERE r.source_id = ? AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))

            UNION ALL

            SELECT r.id, r.source_id, r.target_id, r.predicate, dt.depth + 1, dt.path || '->' || r.target_id
            FROM relations r
            JOIN dependency_tree dt ON r.source_id = dt.target_id
            WHERE dt.depth < ? AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
              AND dt.path NOT LIKE '%' || r.target_id || '%'
        )
        SELECT dt.id, dt.source_id, e1.title, dt.target_id, e2.title, dt.predicate, dt.depth, dt.path
        FROM dependency_tree dt
        JOIN entities e1 ON dt.source_id = e1.id
        JOIN entities e2 ON dt.target_id = e2.id
        ORDER BY dt.depth ASC;
        """
        cursor = conn.execute(query, (root_id, pit, pit, pit, pit, max_depth, pit, pit, pit, pit))
        rows = cursor.fetchall()

        nodes = [{"id": root_id, "title": root_info.get("title"), "depth": 0}]
        seen_nodes = {root_id}

        # dt.path (last column) is only needed by the SQL cycle guard (see CTE above) --
        # not serialized here. Callers reconstruct hierarchy from edges' source_id/target_id.
        edges = []
        for rel_id, src_id, src_title, tgt_id, tgt_title, pred, depth, _raw_path in rows:
            if tgt_id not in seen_nodes:
                nodes.append({"id": tgt_id, "title": tgt_title, "depth": depth})
                seen_nodes.add(tgt_id)

            edges.append(
                {
                    "relation_id": rel_id,
                    "source_id": src_id,
                    "source_title": src_title,
                    "target_id": tgt_id,
                    "target_title": tgt_title,
                    "predicate": pred,
                    "depth": depth,
                }
            )

        return {
            "root": root_info,
            "total_dependencies_found": len(edges),
            "graph_exhausted": len(edges) == 0
            or max([e["depth"] for e in edges], default=0) < max_depth,
            "dependencies": nodes,
            "edges": edges,
            "point_in_time": pit,
        }
    except Exception as e:
        logger.error("Error analyzing dependencies: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


def analyze_lineage(
    entity_id: str = None, point_in_time: str = None, db_connection=None, db_path: str = None
) -> dict:
    """Traverses full multi-generation consolidation and derivation ancestry.

    Note: `parent_ids` on entities is now derived/display-only -- the `relations` table's
    `consolidated_from` edges are the authoritative lineage source used for traversal here.
    """
    if not entity_id:
        return {"error": "entity_id is mandatory"}

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    target_id = resolve_entity_id(conn, entity_id)
    if not target_id:
        if should_close:
            close_connection(conn)
        return {"error": f"Could not resolve entity '{entity_id}'"}

    pit = point_in_time or datetime.now(UTC).isoformat()

    try:
        cursor = conn.execute(
            "SELECT id, title, status, owner_id, updated_at FROM entities WHERE id = ?",
            (target_id,),
        )
        root_row = cursor.fetchone()
        root_info = (
            {
                "id": root_row[0],
                "title": root_row[1],
                "status": root_row[2],
                "owner_id": root_row[3],
                "updated_at": root_row[4],
                "generation_depth": 0,
            }
            if root_row
            else {
                "id": target_id,
                "title": "Root",
                "status": "raw",
                "owner_id": None,
                "updated_at": None,
                "generation_depth": 0,
            }
        )

        query = """
        WITH RECURSIVE lineage(id, source_id, target_id, depth, path) AS (
            SELECT r.id, r.source_id, r.target_id, 1, r.source_id || '->' || r.target_id
            FROM relations r
            WHERE r.source_id = ? AND r.predicate = 'consolidated_from'
              AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
            UNION ALL
            SELECT r.id, r.source_id, r.target_id, l.depth + 1, l.path || '->' || r.target_id
            FROM relations r
            JOIN lineage l ON r.source_id = l.target_id
            WHERE r.predicate = 'consolidated_from' AND l.depth < 10
              AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND l.path NOT LIKE '%' || r.target_id || '%'
        )
        SELECT l.target_id, e.title, e.status, e.owner_id, e.updated_at, l.depth
        FROM lineage l JOIN entities e ON l.target_id = e.id
        ORDER BY l.depth ASC;
        """
        cursor = conn.execute(query, (target_id, pit, pit, pit, pit, pit, pit))
        rows = cursor.fetchall()

        ancestry = [root_info]
        seen_nodes = {target_id}
        for r in rows:
            aid = r[0]
            if aid in seen_nodes:
                continue
            seen_nodes.add(aid)
            ancestry.append(
                {
                    "id": aid,
                    "title": r[1],
                    "status": r[2],
                    "owner_id": r[3],
                    "updated_at": r[4],
                    "generation_depth": r[5],
                }
            )

        return {
            "entity_id": target_id,
            "total_ancestors": max(len(ancestry) - 1, 0),
            "ancestors": ancestry,
            "point_in_time": pit,
        }
    except Exception as e:
        logger.error("Error analyzing lineage: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


def commit_consolidation(  # noqa: C901, PLR0911, PLR0912, PLR0915
    parent_ids: list[str],
    title: str,
    content: str,
    tags: list[str] = None,
    scope: Literal["private", "shared"] = "shared",
    weight: int = 1,
    is_core: bool = None,
    owner_id: str = None,
    context_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents and repointing relations.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_commit_consolidation, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).
    """
    if not parent_ids or not isinstance(parent_ids, list):
        return "Error: parent_ids must be a non-empty list of UUID strings."
    if not title or not content:
        return "Error: title and content are mandatory."

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    resolved_parents = []
    seen = set()
    for p in parent_ids:
        res = resolve_entity_id(conn, str(p))
        if res and res not in seen:
            seen.add(res)
            resolved_parents.append(res)

    if resolved_parents:
        placeholders_exist = ",".join("?" for _ in resolved_parents)
        existing_rows = conn.execute(
            f"SELECT id FROM entities WHERE id IN ({placeholders_exist})", resolved_parents
        ).fetchall()
        existing_set = {r[0] for r in existing_rows}
        resolved_parents = [p for p in resolved_parents if p in existing_set]

    if not resolved_parents:
        if should_close:
            close_connection(conn)
        return "Error: None of the provided parent_ids could be resolved."

    if is_core is None:
        placeholders_core = ",".join("?" for _ in resolved_parents)
        core_row = conn.execute(
            f"SELECT 1 FROM entities WHERE id IN ({placeholders_core}) AND is_core = 1 LIMIT 1",
            resolved_parents,
        ).fetchone()
        is_core_val = 1 if core_row else 0
    else:
        is_core_val = 1 if is_core in (True, 1, "true", "1", "True") else 0

    redacted_content = redact_secrets(content)
    clean_title = redact_secrets(title)
    owner_val = owner_id or "system"

    # Execute Tier 1 & Tier 2 Quality Gate on consolidated content
    quality_res = evaluate_memory_quality(redacted_content, clean_title)
    if quality_res["status"] == "REJECT":
        if should_close:
            close_connection(conn)
        return f"Error: Consolidation quality check rejected (Score: {quality_res['quality_score']:.2f}). Reason: {quality_res['reason']}"

    content_hash = compute_content_hash(redacted_content)
    quality_score = quality_res["quality_score"]
    quality_status = quality_res["status"]
    quality_flags_str = json.dumps(quality_res["quality_flags"])

    # Stage A Exact Hash Collision Lookup (excluding resolved parent IDs)
    try:
        placeholders_p = ",".join("?" for _ in resolved_parents)
        query_sql = f"""
            SELECT id FROM entities
            WHERE content_hash = ? AND owner_id = ? AND status != 'archived'
              AND id NOT IN ({placeholders_p})
        """
        cursor = conn.execute(query_sql, [content_hash, owner_val] + resolved_parents)
        row = cursor.fetchone()
        if row:
            if should_close:
                close_connection(conn)
            return f"Error: REJECT_EXACT_DUPLICATE - Consolidated memory exact hash matches existing entity ID: {row[0]}"
    except Exception:
        pass

    # Stage B Near-Duplicate Check (excluding resolved parent IDs)
    try:
        dup_check = check_duplicate_memories(
            title=clean_title,
            content=redacted_content,
            owner_id=owner_val,
            exclude_ids=resolved_parents,
            db_connection=conn,
        )
        if dup_check.get("duplicate_found") and "error" not in dup_check:
            top = dup_check["potential_duplicates"][0]
            logger.warning(
                "Consolidation potential near-duplicate detected against unrelated memory '%s' (ID: %s)",
                top["title"],
                top["id"],
            )
    except Exception:
        pass

    consolidated_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    try:

        def _do_commit():
            conn.execute(
                """
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, context_id, content_hash, quality_score, quality_status, quality_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'consolidated', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    consolidated_id,
                    now,
                    now,
                    now,
                    owner_val,
                    scope,
                    is_core_val,
                    weight,
                    json.dumps(resolved_parents),
                    clean_title,
                    redacted_content,
                    now,
                    context_id,
                    content_hash,
                    quality_score,
                    quality_status,
                    quality_flags_str,
                ),
            )

            if tags:
                for tag_name in tags:
                    tag_id = resolve_or_create_tag(conn, tag_name, agent_id=owner_val)
                    if not tag_id:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                        (consolidated_id, tag_id),
                    )

            if is_core_val:
                core_tag_id = resolve_or_create_tag(conn, "#core", agent_id=owner_val)
                if core_tag_id:
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                        (consolidated_id, core_tag_id),
                    )

            placeholders = ",".join("?" for _ in resolved_parents)
            conn.execute(
                f"""
                UPDATE entities
                SET status = 'archived', embedding_status = 'archived', updated_at = ?, valid_to = ?
                WHERE id IN ({placeholders})
            """,
                [now, now] + resolved_parents,
            )

            parent_set = set(resolved_parents)
            active_touching_rows = conn.execute(
                f"""
                SELECT id, source_id, target_id, predicate, valid_at, invalid_at
                FROM relations
                WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                  AND valid_to IS NULL
                  AND predicate != 'consolidated_from'
            """,
                resolved_parents + resolved_parents,
            ).fetchall()

            for rel_id, src, tgt, pred, old_valid_at, old_invalid_at in active_touching_rows:
                conn.execute("UPDATE relations SET valid_to = ? WHERE id = ?", (now, rel_id))

                new_src = consolidated_id if src in parent_set else src
                new_tgt = consolidated_id if tgt in parent_set else tgt
                if new_src == new_tgt:
                    continue  # self-loop guard: edge was directly between two parents in this batch

                conn.execute(
                    """
                    INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at, invalid_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, target_id, predicate) WHERE valid_to IS NULL DO NOTHING
                """,
                    (
                        str(uuid.uuid4()),
                        new_src,
                        new_tgt,
                        pred,
                        now,
                        now,
                        old_valid_at,
                        old_invalid_at,
                    ),
                )

            for parent_id in resolved_parents:
                rel_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                    VALUES (?, ?, ?, 'consolidated_from', ?, ?)
                """,
                    (rel_id, consolidated_id, parent_id, now, now),
                )

        if _in_transaction:
            _do_commit()
        else:

            def _write(c):
                _do_commit()

            write_transaction_retrying(conn, _write)

        try:
            target_db = conn.execute("PRAGMA database_list").fetchone()[2]
        except Exception:
            target_db = db_path or get_db_path()
        if target_db:
            from saltmdb.domain.services import embedding_service
            from saltmdb.domain.services.memory_service import _embed_pool

            _embed_pool.submit(
                embedding_service.embed_entity_async,
                consolidated_id,
                clean_title,
                redacted_content,
                target_db,
            )

        return f"Successfully committed consolidated memory with ID: {consolidated_id}"
    except Exception as e:
        logger.error("Error committing consolidation: %s", e)
        return f"Error committing consolidation: {e}"
    finally:
        if should_close:
            close_connection(conn)


def bulk_commit_consolidation(
    consolidations: list, db_connection=None, db_path: str = None
) -> list:
    """Executes multiple consolidation commits atomically in a single transaction -- all-or-nothing.

    If any item raises (or would otherwise be reported as an error), the whole batch rolls
    back, so no partial set of consolidations is ever left committed. Because a single
    failure unwinds every prior "successful" item in the same batch, a mixed per-item
    success/error list would misrepresent the outcome -- so on failure this returns a
    single top-level error result instead of claiming any individual items succeeded.
    """
    if not consolidations or not isinstance(consolidations, list):
        return [{"status": "error", "error": "consolidations must be a non-empty array of objects"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    results: list[Any] = []
    try:

        def _write(conn_arg):
            results.clear()
            for item in consolidations:
                p_ids = item.get("parent_ids", [])
                t = item.get("title")
                c = item.get("content")
                tags = item.get("tags", [])
                scope = item.get("scope", "shared")
                w = item.get("weight", 1)
                is_core = item.get("is_core")

                res = commit_consolidation(
                    parent_ids=p_ids,
                    title=t,
                    content=c,
                    tags=tags,
                    scope=scope,
                    weight=w,
                    is_core=is_core,
                    db_connection=conn,
                    _in_transaction=True,
                )
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk consolidation aborted (all-or-nothing): {res}")
                new_id = res.split("ID: ")[-1].strip()
                results.append(
                    {"status": "success", "entity_id": new_id, "title": t, "result": res}
                )

        write_transaction_retrying(conn, _write)
        return results
    except Exception as e:
        logger.error(
            "Bulk commit consolidation error (batch rolled back, no items consolidated): %s", e
        )
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def bulk_store_relations(relations: list, db_connection=None, db_path: str = None) -> list:
    """Executes multiple relation insertions atomically in a single transaction -- all-or-nothing.

    If any item raises (or would otherwise be reported as an error), the whole batch rolls
    back, so no partial set of relations is ever left committed. Because a single failure
    unwinds every prior "successful" item in the same batch, a mixed per-item success/error
    list would misrepresent the outcome -- so on failure this returns a single top-level
    error result instead of claiming any individual items succeeded.
    """
    if not relations or not isinstance(relations, list):
        return [{"status": "error", "error": "relations must be a non-empty array of objects"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    results: list[Any] = []
    try:

        def _write(c):
            results.clear()
            for r in relations:
                src = r.get("source_id")
                tgt = r.get("target_id")
                pred = r.get("predicate")
                valid_at = r.get("valid_at")
                res = store_relation(
                    source_id=src,
                    target_id=tgt,
                    predicate=pred,
                    valid_at=valid_at,
                    db_connection=conn,
                    _in_transaction=True,
                )
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk relation store aborted (all-or-nothing): {res}")
                status = "duplicate" if res.startswith("Relation already exists") else "success"
                results.append(
                    {
                        "status": status,
                        "source": src,
                        "target": tgt,
                        "predicate": pred,
                        "result": res,
                    }
                )

        write_transaction_retrying(conn, _write)
        return results
    except Exception as e:
        logger.error("Bulk store relations error (batch rolled back, no items stored): %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
