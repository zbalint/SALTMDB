import uuid
import json
import logging
import re
import sqlite3
from datetime import datetime, UTC
from typing import Any, Literal
from saltmdb.config import (
    get_db_path,
    COHESION_MIN_PAIRWISE_THRESHOLD,
    COHESION_OVERRIDE_MIN_LENGTH,
    RELATION_GATE_MIN_SIMILARITY_THRESHOLD,
    RELATION_GATE_STRONG_PREDICATES,
    RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS,
)
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import resolve_entity_id, compute_content_hash
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.domain.services.memory_service import check_duplicate_memories, resolve_or_create_tag
from saltmdb.domain.services.cohesion_service import (
    get_fresh_entity_centroids,
    min_pairwise_cohesion,
)
from saltmdb.domain.services.event_service import log_event

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


def store_relation(  # noqa: C901, PLR0915
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    valid_at: str | None = None,
    override_justification: str | None = None,
    owner_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> str:
    """Stores a directional relationship edge between two knowledge entities.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_store_relations, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).

    Memory-core rework Phase 5 governance gate (see plans/structured-finding-matsumoto.md and
    SALTMDB memory `5c09effa`/`6490fe88`): for RELATION_GATE_STRONG_PREDICATES
    (elaborates_on/resolves/supersedes), the source/target chunk-embedding centroids must clear
    RELATION_GATE_MIN_SIMILARITY_THRESHOLD, and no contradictory predicate pair
    (RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS) may already exist on the same directional edge
    -- either violation rejects the call as REJECT_LOW_RELATION_SIMILARITY /
    REJECT_CONTRADICTORY_PREDICATE unless override_justification (>= COHESION_OVERRIDE_MIN_LENGTH
    chars) is supplied, in which case the relation is stored and a relation_gate_override event
    is logged atomically (agent_id = owner_id or "system"). An already-active identical edge is
    always a no-op, checked BEFORE the gate -- re-submitting it never requires an override.
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

        def _do_store():  # noqa: C901
            normalized_requested = _normalize_predicate_name(predicate)
            canonical_predicate = resolve_or_create_predicate(conn, predicate) or predicate
            note = (
                f" [canonicalized: requested '{predicate}', stored as '{canonical_predicate}']"
                if predicate
                and normalized_requested
                and normalized_requested != canonical_predicate
                else ""
            )
            owner_val = owner_id or "system"

            # D1 (Phase 5 R2 fix #4): an existing active identical edge is a legitimate no-op --
            # short-circuit BEFORE any gate check runs, so re-submitting an already-active edge
            # (e.g. a low-similarity strong-predicate edge accepted before this gate existed)
            # never demands an override for creating nothing. Same match shape as the
            # ON CONFLICT(...) WHERE valid_to IS NULL constraint the INSERT below relies on.
            existing_edge = conn.execute(
                "SELECT id FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ? AND valid_to IS NULL",
                (resolved_source, resolved_target, canonical_predicate),
            ).fetchone()
            if existing_edge:
                return f"Relation already exists (no-op): '{canonical_predicate}' between {resolved_source} and {resolved_target} (ID: {existing_edge[0]}){note}"

            # D3: similarity gate, strong (judgment) predicates only. An unresolved entity
            # (archived, no usable content) forces a gate failure requiring override -- same
            # convention as Phase 3/4's unresolved-centroid handling, not a silent pass.
            sim, offending = 1.0, None
            unresolved: dict[str, str] = {}
            if canonical_predicate in RELATION_GATE_STRONG_PREDICATES:
                centroids, unresolved, _observed_state = get_fresh_entity_centroids(
                    [resolved_source, resolved_target], conn, db_path or get_db_path()
                )
                if unresolved:
                    sim, offending = (
                        0.0,
                        (next(iter(unresolved)), f"<{next(iter(unresolved.values()))}>"),
                    )
                else:
                    sim, offending = min_pairwise_cohesion(centroids)

            # D4: contradictory-predicate check, same directional edge only (MVP scope).
            # Existing predicates are canonicalized (Phase 5 R2 fix #3) before comparison, so a
            # pre-canonicalization-era row holding a raw legacy-alias string (e.g. "references",
            # which aliases to elaborates_on) is still caught. Applies regardless of predicate
            # strength -- a contradictory pair is a structural problem, not a similarity one.
            existing_raw = {
                r[0]
                for r in conn.execute(
                    "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
                    (resolved_source, resolved_target),
                ).fetchall()
            }
            existing_predicates = {
                resolve_or_create_predicate(conn, ep) or ep for ep in existing_raw
            }
            contradictions = [
                ep
                for ep in existing_predicates
                if frozenset({ep, canonical_predicate})
                in RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS
            ]

            # D5: one unified override, one audit event covering both checks.
            violations = []
            if (
                canonical_predicate in RELATION_GATE_STRONG_PREDICATES
                and sim < RELATION_GATE_MIN_SIMILARITY_THRESHOLD
            ):
                violations.append("low_similarity")
            if contradictions:
                violations.append("contradictory_predicate")

            justification = (override_justification or "").strip()
            if violations:
                if len(justification) < COHESION_OVERRIDE_MIN_LENGTH:
                    reasons = []
                    if "low_similarity" in violations:
                        reasons.append(
                            f"REJECT_LOW_RELATION_SIMILARITY (similarity={sim:.4f} < "
                            f"{RELATION_GATE_MIN_SIMILARITY_THRESHOLD}, offending={offending}"
                            f"{', unresolved=' + str(unresolved) if unresolved else ''})"
                        )
                    if "contradictory_predicate" in violations:
                        reasons.append(
                            f"REJECT_CONTRADICTORY_PREDICATE (predicate='{canonical_predicate}' "
                            f"conflicts with existing {contradictions} on this edge)"
                        )
                    return (
                        "Error: "
                        + " ".join(reasons)
                        + f" Pass override_justification (>= {COHESION_OVERRIDE_MIN_LENGTH} "
                        "chars) to force this relation."
                    )

                # Atomic audit trail -- written as the first write for this call, before the
                # relation itself, so any log_event failure rolls back the whole transaction
                # (same fail-fast-atomicity reasoning as commit_consolidation's override audit).
                audit_result = log_event(
                    agent_id=owner_val,
                    type="relation_gate_override",
                    content=json.dumps(
                        {
                            "source_id": resolved_source,
                            "target_id": resolved_target,
                            "predicate": canonical_predicate,
                            "violations": violations,
                            "similarity": sim,
                            "similarity_threshold": RELATION_GATE_MIN_SIMILARITY_THRESHOLD,
                            "contradicting_predicates": contradictions,
                            "justification": justification,
                            "relation_id": relation_id,
                        }
                    ),
                    db_connection=conn,
                    _in_transaction=True,
                )
                if audit_result.startswith("Error"):
                    raise RuntimeError(
                        f"Failed to record relation gate override audit event: {audit_result}"
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
                # Defensive backstop only now (D1 above already handles the primary no-op path)
                # -- a same-transaction race where a concurrent writer resolved this exact edge
                # between the D1 check and this INSERT.
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
        seen_edges = set()
        for rel_id, src_id, src_title, tgt_id, tgt_title, pred, depth, _raw_path in rows:
            if tgt_id not in seen_nodes:
                nodes.append({"id": tgt_id, "title": tgt_title, "depth": depth})
                seen_nodes.add(tgt_id)

            # Multiple converging paths in a diamond-shaped graph can revisit the same
            # relation once per incoming path (the CTE's cycle guard only stops a single
            # path from looping, not distinct paths reconverging). Dedupe on the relation's
            # own id, keeping the first (shallowest, per ORDER BY dt.depth ASC) occurrence.
            if rel_id in seen_edges:
                continue
            seen_edges.add(rel_id)

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


def _resolve_and_filter_parent_ids(conn, parent_ids: list) -> list[str]:
    """Resolves each parent_id to its canonical entity id (via resolve_entity_id), dedupes,
    and filters out any id that doesn't currently exist in entities.

    Single source of truth for this resolution, shared by commit_consolidation's own
    single-item path and bulk_commit_consolidation's pre-transaction cross-item union (Codex
    correction R3 -- resolve-before-union: mixing raw and resolved ids when slicing the
    precomputed centroid/unresolved/observed_state maps per item is unsafe, since a raw alias
    would miss the entry actually stored under the canonical id). Exactly one resolution code
    path, not two that could drift.
    """
    resolved_parents = []
    seen = set()
    for p in parent_ids or []:
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

    return resolved_parents


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
    override_justification: str | None = None,
    metadata: dict | None = None,
    memory_type: str | None = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
    _precomputed_centroids: dict[str, list[float]] | None = None,
    _precomputed_unresolved: dict[str, str] | None = None,
    _precomputed_observed_state: dict[str, tuple[str, str]] | None = None,
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents and repointing relations.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_commit_consolidation, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).

    A pairwise-cohesion gate runs before the write: parent_ids' chunk-embedding centroids must
    clear COHESION_MIN_PAIRWISE_THRESHOLD (MIN, not MEAN, pairwise cosine similarity) or the
    call is rejected with REJECT_LOW_COHESION, unless override_justification (>=
    COHESION_OVERRIDE_MIN_LENGTH chars) is supplied to force the merge (audited atomically, see
    _do_commit below). The `_precomputed_*` params are never set by external callers or the MCP
    surface -- they exist only so bulk_commit_consolidation can hoist expensive centroid
    computation before its write transaction opens, while every item still routes through this
    same single code path.

    `metadata`/`memory_type` (Track A, see scratch/plans/track_a_disposition_detailed.md): added
    so disposition_service.py's consolidate-disposition path can carry a proposed write's
    metadata/memory_type through into the consolidated entity instead of silently dropping them
    (this function previously had no columns for either) -- both default None/unset, matching
    every existing caller's behavior exactly (memory_type still resolves to 'fact' via the same
    COALESCE the plain store path uses).
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

    resolved_parents = _resolve_and_filter_parent_ids(conn, parent_ids)

    if not resolved_parents:
        if should_close:
            close_connection(conn)
        return "Error: None of the provided parent_ids could be resolved."

    # Pairwise cohesion gate (memory-core rework Phase 3, Part A). Centroid + MIN aggregation:
    # cheap at realistic parent_ids scale, and MIN (not MEAN) directly targets the "one diluted
    # outlier" failure mode that let `6a8fec3d` force-merge 37+ unrelated memories.
    #
    # P1 (Codex review, bf4qtkp7j / 7a5eba85): the gate is a no-op by contract for fewer than
    # two parents -- there's no pairwise comparison to make. That must be a short-circuit BEFORE
    # any centroid work, not just min_pairwise_cohesion's own len(centroids) < 2 trivial-pass:
    # with only one parent, an `unresolved` entry (e.g. empty/unembeddable content) used to still
    # force min_sim=0.0 below and reject the merge, even though there was nothing to compare it
    # against. cohesion_gate_applicable also gates the TOCTOU revalidation in _do_commit below --
    # no cohesion decision means no observed_state snapshot to revalidate against.
    cohesion_gate_applicable = len(resolved_parents) >= 2
    centroids: dict[str, list[float]]
    unresolved: dict[str, str]
    observed_state: dict[str, tuple[str, str]]
    if not cohesion_gate_applicable:
        centroids, unresolved, observed_state = {}, {}, {}
        min_sim, offending_pair = 1.0, None
    else:
        if (
            _precomputed_centroids is not None
            and _precomputed_unresolved is not None
            and _precomputed_observed_state is not None
        ):
            centroids = _precomputed_centroids
            unresolved = _precomputed_unresolved
            observed_state = _precomputed_observed_state
        else:
            centroids, unresolved, observed_state = get_fresh_entity_centroids(
                resolved_parents, conn, db_path or get_db_path()
            )
        if unresolved:
            min_sim, offending_pair = (
                0.0,
                (
                    next(iter(unresolved)),
                    f"<{next(iter(unresolved.values()))}>",
                ),
            )

        else:
            min_sim, offending_pair = min_pairwise_cohesion(centroids)

    justification = (override_justification or "").strip()
    override_applied = min_sim < COHESION_MIN_PAIRWISE_THRESHOLD
    if override_applied and len(justification) < COHESION_OVERRIDE_MIN_LENGTH:
        if should_close:
            close_connection(conn)
        return (
            f"Error: REJECT_LOW_COHESION - parent set fails pairwise similarity gate "
            f"(min={min_sim:.4f} < {COHESION_MIN_PAIRWISE_THRESHOLD}, weakest pair={offending_pair}"
            f"{', unresolved=' + str(unresolved) if unresolved else ''}). "
            f"Pass override_justification (>= {COHESION_OVERRIDE_MIN_LENGTH} chars) to force this merge."
        )

    if override_applied:
        # A3: baked into `content` BEFORE redact_secrets/compute_content_hash run below, so the
        # override is part of the committed content_hash, not a side-channel annotation. Kept
        # to plain prose sentences deliberately (no raw UUIDs/dict dumps): the full technical
        # detail (parent_ids, offending_pair, unresolved) is already recorded precisely in the
        # consolidation_gate_override audit event below -- packing long underscore-joined
        # identifiers or raw UUID tokens into this annotation would itself skew the consolidated
        # content's Coleman-Liau readability score (evaluate_memory_quality, run just below)
        # high enough to trip its own quality gate on otherwise-unremarkable content.
        content = (
            f"{content}\n\n---\n"
            f"[Consolidation Override] This merge was forced past the automatic cohesion gate. "
            f"The minimum pairwise similarity between parents was {min_sim:.4f}, below the "
            f"required threshold of {COHESION_MIN_PAIRWISE_THRESHOLD}. "
            f"{'One or more parents had no usable content for scoring. ' if unresolved else ''}"
            f"Justification: {justification}"
        )

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
    except sqlite3.Error as exc:
        logger.warning(
            "Consolidation exact-hash lookup unavailable; continuing with duplicate checks: %s", exc
        )

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
    except (sqlite3.Error, ValueError) as exc:
        logger.warning("Consolidation near-duplicate check unavailable; continuing: %s", exc)

    consolidated_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    try:

        def _do_commit():  # noqa: C901, PLR0912
            # A4: TOCTOU revalidation, first statements, before any destructive write. Re-fetch
            # content_hash/status for every resolved parent and compare against observed_state
            # -- the dict returned from the EXACT read that produced (or failed to produce)
            # each centroid, never a separately-later-taken snapshot. Any mismatch (content
            # changed, status no longer eligible, or a resolved parent with no observed_state
            # entry at all -- meaning it was unresolved/archived, or its read never completed
            # cleanly) aborts the whole transaction. This is also what catches an earlier item
            # in the same bulk batch having already archived a shared parent: that parent's
            # current status reads back 'archived', mismatching its recorded observed_state.
            # Gated on cohesion_gate_applicable (P1, Codex review bf4qtkp7j / 7a5eba85): for a
            # single-parent commit the gate never ran, so observed_state is deliberately empty --
            # there is no cohesion snapshot to revalidate against, and requiring one here would
            # make every single-parent commit spuriously fail this check.
            if cohesion_gate_applicable and resolved_parents:
                placeholders_reval = ",".join("?" for _ in resolved_parents)
                current_rows = conn.execute(
                    f"SELECT id, content_hash, status FROM entities WHERE id IN ({placeholders_reval})",
                    resolved_parents,
                ).fetchall()
                current_state = {r[0]: (r[1], r[2]) for r in current_rows}
                for pid in resolved_parents:
                    if observed_state.get(pid) != current_state.get(pid):
                        raise RuntimeError(
                            f"Consolidation aborted: parent {pid} state changed since the "
                            f"cohesion decision was made (observed={observed_state.get(pid)}, "
                            f"current={current_state.get(pid)})"
                        )

            # A3: atomic audit trail for an override commit -- written as the first destructive
            # step for fail-fast clarity. log_event catches its own exceptions and RETURNS an
            # "Error: ..." string rather than raising, so the result must be checked and raised
            # on here -- because this runs inside the surrounding write_transaction_retrying
            # (directly for a single-item call, or one level up in bulk_commit_consolidation for
            # a batch item), any exception raised anywhere in _do_commit rolls back the whole
            # transaction, which is what actually makes this audit atomic with the merge itself.
            if override_applied:
                audit_result = log_event(
                    agent_id=owner_val,
                    type="consolidation_gate_override",
                    content=json.dumps(
                        {
                            "parent_ids": resolved_parents,
                            "min_pairwise_similarity": min_sim,
                            "threshold": COHESION_MIN_PAIRWISE_THRESHOLD,
                            "offending_pair": offending_pair,
                            "unresolved": unresolved,
                            "justification": justification,
                            "consolidated_id": consolidated_id,
                        }
                    ),
                    db_connection=conn,
                    _in_transaction=True,
                )
                if audit_result.startswith("Error"):
                    raise RuntimeError(
                        f"Failed to record consolidation override audit event: {audit_result}"
                    )

            metadata_str = json.dumps(metadata) if metadata else None
            conn.execute(
                """
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, context_id, content_hash, quality_score, quality_status, quality_flags, metadata, memory_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'consolidated', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'))
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
                    metadata_str,
                    memory_type,
                ),
            )

            # The new consolidated entity and all archived parents transition
            # with their durable embedding work in this one transaction.
            from saltmdb.domain.services.embedding_service import (
                cancel_embedding_jobs_for_entity,
                enqueue_embedding_jobs_for_entity,
            )

            enqueue_embedding_jobs_for_entity(
                conn, consolidated_id, clean_title, redacted_content, content_hash
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
            for parent_id in resolved_parents:
                cancel_embedding_jobs_for_entity(conn, parent_id)

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

    A4 (memory-core rework Phase 3): every item's parent_ids is resolved/deduped FIRST via the
    same helper commit_consolidation itself uses (_resolve_and_filter_parent_ids), so the
    cross-item union built from them is keyed on canonical entity ids, never raw aliases. The
    union's chunk-embedding centroids are then computed ONCE, before write_transaction_retrying
    opens -- no write lock (BEGIN IMMEDIATE) held yet, so an on-demand embedding fallback for an
    uncached parent never blocks other writers. Each per-item commit_consolidation(...) call
    inside the transaction receives its own slice of the precomputed centroids/unresolved/
    observed_state maps -- the gate decision itself is then pure numpy inside the lock, no I/O,
    no model calls -- and re-validates that slice's observed_state against the entities table's
    current state as the first statements of its own _do_commit (see commit_consolidation's A4
    docstring), which is what actually catches an earlier item in the same batch having already
    archived a shared parent.
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
        item_resolved_parents: list[list[str]] = []
        union_ids: list[str] = []
        seen_union: set[str] = set()
        for item in consolidations:
            p_ids = _resolve_and_filter_parent_ids(conn, item.get("parent_ids", []))
            item_resolved_parents.append(p_ids)
            for pid in p_ids:
                if pid not in seen_union:
                    seen_union.add(pid)
                    union_ids.append(pid)

        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            union_ids, conn, db_path or get_db_path()
        )

        def _write(conn_arg):
            results.clear()
            for item, p_ids in zip(consolidations, item_resolved_parents):
                t = item.get("title")
                c = item.get("content")
                tags = item.get("tags", [])
                scope = item.get("scope", "shared")
                w = item.get("weight", 1)
                is_core = item.get("is_core")
                override_justification = item.get("override_justification")

                item_centroids = {pid: centroids[pid] for pid in p_ids if pid in centroids}
                item_unresolved = {pid: unresolved[pid] for pid in p_ids if pid in unresolved}
                item_observed_state = {
                    pid: observed_state[pid] for pid in p_ids if pid in observed_state
                }

                res = commit_consolidation(
                    parent_ids=p_ids,
                    title=t,
                    content=c,
                    tags=tags,
                    scope=scope,
                    weight=w,
                    is_core=is_core,
                    override_justification=override_justification,
                    db_connection=conn,
                    _in_transaction=True,
                    _precomputed_centroids=item_centroids,
                    _precomputed_unresolved=item_unresolved,
                    _precomputed_observed_state=item_observed_state,
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
                # Phase 5 D8: per-item, not a single top-level value for the whole batch -- a
                # bulk call can legitimately mix relations attributed to different agents/owners
                # or needing different override justifications, same reasoning as
                # commit_consolidation's per-item override_justification.
                override_justification = r.get("override_justification")
                owner_id = r.get("owner_id")
                res = store_relation(
                    source_id=src,
                    target_id=tgt,
                    predicate=pred,
                    valid_at=valid_at,
                    override_justification=override_justification,
                    owner_id=owner_id,
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
