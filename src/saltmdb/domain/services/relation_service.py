import uuid
import json
import logging
import re
import sqlite3
from datetime import datetime, UTC
from typing import Any, Literal, NamedTuple, cast
from dataclasses import dataclass
from saltmdb.config import (
    get_db_path,
    COHESION_MIN_PAIRWISE_THRESHOLD,
    COHESION_OVERRIDE_MIN_LENGTH,
    RELATION_GATE_MIN_SIMILARITY_THRESHOLD,
    RELATION_GATE_STRONG_PREDICATES,
    RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS,
    MAX_MEMORY_CONTENT_CHARS,
)
from saltmdb.db.connection import (
    get_connection,
    managed_connection,
    write_transaction_retrying,
    close_connection,
    is_coordinator_connection,
    rollback_to_and_release,
    savepoint,
)
from saltmdb.utils import error_codes
from saltmdb.utils.envelope import (
    error as envelope_error,
    ok as envelope_ok,
    rejected,
    warning as envelope_warning,
)
from saltmdb.utils import envelope
from saltmdb.utils.text import resolve_entity_ref, compute_content_hash
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.domain.services.memory_service import (
    check_duplicate_memories,
    resolve_or_create_tag,
    Provenance,
    CoreDeclaration,
    EntityAnnotations,
)
from saltmdb.domain.services.cohesion_service import (
    get_fresh_entity_centroids,
    min_pairwise_cohesion,
)
from saltmdb.domain.services.event_service import log_event
from saltmdb.domain.services import core_governance_service
from saltmdb.utils.predicate_vocabulary import AGENT_SELECTABLE_PREDICATES, classify_predicate

logger = logging.getLogger(__name__)


def _entity_ref_error_code(error_message: str) -> str:
    if "AMBIGUOUS_ID_PREFIX" in error_message:
        return "AMBIGUOUS_ID_PREFIX"
    elif "UNKNOWN_ENTITY_ID" in error_message:
        return "UNKNOWN_ENTITY_ID"
    else:
        return error_codes.VALIDATION_ERROR


def _normalize_predicate_name(raw: str) -> str:
    """Shape-normalizes a predicate string (lowercase, non-alnum runs -> underscore, trimmed)."""
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")


def resolve_or_create_predicate(conn, predicate_name: str) -> str | None:
    """Closed-vocabulary predicate lookup (agent API redesign plan §5.8, Phase 6 item 25).

    INVERTED CONTRACT: this used to auto-create any unrecognized predicate; it now never writes
    to the `predicates` table at all. Returns the canonical name for anything in the closed
    51-name universe (saltmdb.utils.predicate_vocabulary: 11 agent-selectable + 3 reserved + 1
    legacy-read-only + 36 known drifted aliases) -- including aliases, since this function is
    also the READ-side canonicalizer used to compare an EXISTING relations row against the
    contradictory-predicate-pair gate (store_relation's D4 check) and to look up an existing
    edge by canonical name (invalidate_relation). Those callers must keep resolving a legacy
    spelling already sitting in the DB even though NEW writes of that spelling are rejected
    elsewhere (store_relation's write-time gate, added alongside this inversion).

    Falls back to a DB predicates-table lookup (by exact name, never inserting) for anything
    outside the closed universe, so a pre-existing custom row from before this closure (a
    genuinely older clone/DB) still resolves for read purposes. Returns None when nothing
    matches at all -- caller falls back to the raw input string, same contract as before.
    """
    raw = (predicate_name or "").strip()
    if not raw:
        return None
    normalized = _normalize_predicate_name(raw)
    if not normalized:
        return None

    disposition = classify_predicate(normalized)
    if disposition.canonical:
        return disposition.canonical

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

    return None


def list_predicates(
    query: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> dict:
    """Lists the closed relation-predicate vocabulary (agent API redesign plan §5.12, Phase 6
    item 27: renamed from get_canonical_predicates). Mirrors memory_service.search_tags for the
    predicates table."""
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
        rows = []
        for row in cursor.fetchall():
            disposition = classify_predicate(row[1])
            rows.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "disposition": disposition.status,
                    "lifecycle_tool": disposition.lifecycle_tool,
                }
            )
        return envelope_ok(rows)
    except Exception as e:
        logger.error("Error fetching canonical predicates: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)


def _resolve_relation_endpoint(conn, raw_id: str, label: str) -> tuple[str | None, str | None]:
    """Resolves one manage_relation endpoint (source_id/target_id) through the shared
    resolve_entity_ref contract (§4.4), returning (resolved_id, error_message). error_message
    is None on success. Replaces the bare 'Could not resolve target entity IDs' string that
    previously let an unresolved id reach the INSERT and fail with a raw
    'FOREIGN KEY constraint failed' (§3.15) -- callers must check resolved_id is not None."""
    resolved, candidates, truncated = resolve_entity_ref(conn, raw_id)
    if resolved:
        return resolved, None
    if candidates:
        lines = [f"  {c['id']} — {c['title']!r} [{c['status']}]" for c in candidates]
        return None, (
            f"Error: AMBIGUOUS_ID_PREFIX - {label} '{raw_id}' matches "
            f"{len(candidates)}{'+' if truncated else ''} memories:\n" + "\n".join(lines)
        )
    return None, f"Error: UNKNOWN_ENTITY_ID - could not resolve {label} '{raw_id}'."


def _relation_gate_rejection(
    canonical_predicate: str,
    sim: float,
    offending,
    unresolved: dict[str, str],
    contradictions: list[str],
    violations: list[str],
):
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
    message = (
        "Error: "
        + " ".join(reasons)
        + f" Pass override_justification (>= {COHESION_OVERRIDE_MIN_LENGTH} "
        "chars) to force this relation."
    )
    code = (
        "REJECT_LOW_RELATION_SIMILARITY"
        if "low_similarity" in violations
        else "REJECT_CONTRADICTORY_PREDICATE"
    )
    return rejected([envelope_error(code, message)])


def _apply_relation_gate(
    conn,
    resolved_source: str,
    resolved_target: str,
    canonical_predicate: str,
    sim: float,
    offending,
    unresolved: dict[str, str],
    contradictions: list[str],
    override_justification: str | None,
    agent_val: str,
    relation_id: str,
):
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
            return _relation_gate_rejection(
                canonical_predicate,
                sim,
                offending,
                unresolved,
                contradictions,
                violations,
            )

        # Atomic audit trail -- written as the first write for this call, before the
        # relation itself, so any log_event failure rolls back the whole transaction
        # (same fail-fast-atomicity reasoning as commit_consolidation's override audit).
        audit_result = log_event(
            agent_id=agent_val,
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
        if envelope.is_rejected(audit_result):
            raise RuntimeError(
                "Failed to record relation gate override audit event: "
                f"{audit_result['errors'][0]['message']}"
            )
    return None


def _relation_gate_measurements(
    conn,
    resolved_source: str,
    resolved_target: str,
    canonical_predicate: str,
):
    # D3: similarity gate, strong (judgment) predicates only. An unresolved entity
    # (archived, no usable content) forces a gate failure requiring override -- same
    # convention as Phase 3/4's unresolved-centroid handling, not a silent pass.
    sim, offending = 1.0, None
    unresolved: dict[str, str] = {}
    if canonical_predicate in RELATION_GATE_STRONG_PREDICATES:
        centroids, unresolved, _ = get_fresh_entity_centroids(
            [resolved_source, resolved_target], conn
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
    existing_predicates = {resolve_or_create_predicate(conn, ep) or ep for ep in existing_raw}
    contradictions = [
        ep
        for ep in existing_predicates
        if frozenset({ep, canonical_predicate}) in RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS
    ]
    return sim, offending, unresolved, contradictions


def _check_core_relation_guard(
    conn,
    resolved_target: str,
    canonical_predicate: str,
    allow_core_elaborates_on: bool,
):
    # D1b (core-memory governance, resolved gap #1): a NEW elaborates_on edge into an
    # active core is governed exclusively by that core's own core_detail_memory_ids
    # declaration -- reject any other path from creating one. Checked after the no-op
    # short-circuit above (rule 39: repeating the relation remains a no-op) and before
    # the ordinary similarity/contradiction gates below.
    if canonical_predicate == "elaborates_on" and not allow_core_elaborates_on:
        target_row = conn.execute(
            "SELECT is_core, status FROM entities WHERE id = ?", (resolved_target,)
        ).fetchone()
        if target_row and bool(target_row[0]) and target_row[1] != "archived":
            return rejected(
                [
                    envelope_error(
                        "REJECT_CORE_ELABORATES_ON",
                        (
                            "Error: REJECT_CORE_ELABORATES_ON - elaborates_on edges into an active "
                            "core memory are governed exclusively by that core's own "
                            "core_detail_memory_ids declaration (store_memory/commit_consolidation) "
                            "-- manage_relation cannot create them directly."
                        ),
                        "predicate",
                    )
                ]
            )
    return None


def _insert_relation_write(
    conn,
    resolved_source: str,
    resolved_target: str,
    canonical_predicate: str,
    valid_at: str | None,
    relation_id: str,
    now: str,
    repoint_stale_edge_id,
    note: str,
):
    # C-fix (continued): every gate above has now passed without an early return, so the
    # new edge is confirmed about to be created below -- safe to invalidate the stale
    # repoint target now, in the same transaction. See the detection comment above for
    # why this must happen here and not earlier.
    if repoint_stale_edge_id:
        conn.execute(
            "UPDATE relations SET invalid_at = ?, valid_to = ? WHERE id = ?",
            (now, now, repoint_stale_edge_id),
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
        message = (
            f"Relation already exists (no-op): '{canonical_predicate}' between "
            f"{resolved_source} and {resolved_target} (ID: {existing_id}){note}"
        )
        return envelope_ok(
            {"relation_id": existing_id, "message": message},
            warnings=[envelope_warning(error_codes.ALREADY_DONE, message)],
        )
    return envelope_ok(
        {
            "relation_id": relation_id,
            "message": (
                f"Relation successfully stored: '{canonical_predicate}' between "
                f"{resolved_source} and {resolved_target} (ID: {relation_id}){note}"
            ),
        }
    )


def _store_relation_write(
    conn,
    resolved_source: str,
    resolved_target: str,
    predicate: str,
    valid_at: str | None,
    override_justification: str | None,
    agent_id: str | None,
    relation_id: str,
    now: str,
    allow_core_elaborates_on: bool,
):
    normalized_requested = _normalize_predicate_name(predicate)
    canonical_predicate = resolve_or_create_predicate(conn, predicate) or predicate
    note = (
        f" [canonicalized: requested '{predicate}', stored as '{canonical_predicate}']"
        if predicate and normalized_requested and normalized_requested != canonical_predicate
        else ""
    )
    agent_val = agent_id or "system"

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
        message = (
            f"Relation already exists (no-op): '{canonical_predicate}' between "
            f"{resolved_source} and {resolved_target} (ID: {existing_edge[0]}){note}"
        )
        return envelope_ok(
            {"relation_id": existing_edge[0], "message": message},
            warnings=[envelope_warning(error_codes.ALREADY_DONE, message)],
        )

    # C-fix (cold-start agent-experience review, Issue C): detect whether this call is a
    # manual repoint -- same source + a single-active-target-invariant predicate, but a
    # different existing active target (e.g. re-linking an elaborates_on edge after the
    # old target was superseded) -- so the stale edge can be invalidated once the new one
    # is confirmed about to be created. Scoped to RELATION_GATE_STRONG_PREDICATES: the
    # other agent-selectable predicates (related_to, depends_on, part_of, etc.) are
    # legitimately many-to-many, and auto-invalidating on any new same-source/same-
    # predicate call there would silently destroy a valid second edge, not fix a repoint.
    # Detection only happens here (cheap, no write); the actual invalidation is deferred
    # until every gate below has already passed, immediately alongside the INSERT --
    # invalidating here instead would silently destroy a live edge even when a later gate
    # (D3/D4) rejects the new one, since gate rejections `return` rather than `raise` and
    # write_transaction commits on any normal return, not only a raised exception.
    repoint_stale_edge_id = None
    if canonical_predicate in RELATION_GATE_STRONG_PREDICATES:
        repoint_row = conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND predicate = ? AND target_id != ? AND valid_to IS NULL",
            (resolved_source, canonical_predicate, resolved_target),
        ).fetchone()
        repoint_stale_edge_id = repoint_row[0] if repoint_row else None
    core_rejection = _check_core_relation_guard(
        conn, resolved_target, canonical_predicate, allow_core_elaborates_on
    )
    if core_rejection is not None:
        return core_rejection
    sim, offending, unresolved, contradictions = _relation_gate_measurements(
        conn, resolved_source, resolved_target, canonical_predicate
    )

    gate_result = _apply_relation_gate(
        conn,
        resolved_source,
        resolved_target,
        canonical_predicate,
        sim,
        offending,
        unresolved,
        contradictions,
        override_justification,
        agent_val,
        relation_id,
    )
    if gate_result is not None:
        return gate_result

    return _insert_relation_write(
        conn,
        resolved_source,
        resolved_target,
        canonical_predicate,
        valid_at,
        relation_id,
        now,
        repoint_stale_edge_id,
        note,
    )


def _resolve_store_relation_endpoints(
    conn,
    source_id: str,
    target_id: str,
):
    resolved_source, source_error = _resolve_relation_endpoint(conn, source_id, "source_id")
    resolved_target, target_error = _resolve_relation_endpoint(conn, target_id, "target_id")

    if not resolved_source or not resolved_target:
        error_message = (
            source_error or target_error or "Error: Could not resolve target entity IDs."
        )
        error_code = _entity_ref_error_code(error_message)
        return None, None, rejected([envelope_error(error_code, error_message)])

    if resolved_source == resolved_target:
        return (
            None,
            None,
            rejected(
                [
                    envelope_error(
                        error_codes.VALIDATION_ERROR,
                        "Error: Self-referential relations (source_id == target_id) are forbidden.",
                    )
                ]
            ),
        )
    return resolved_source, resolved_target, None


def _store_relation_predicate_error(predicate: str):
    # Closed predicate vocabulary write-time gate (plan §5.8, Phase 6 item 25). This is the
    # domain-layer backstop -- mcp/tools.py's manage_relation runs the same classification
    # earlier, before any backend call, so it can emit a schema-derived corrected_call (this
    # layer cannot: it has no reference to the MCP tool_func to build one from). Both layers
    # exist so a caller that bypasses the adapter (an internal service, a direct domain-layer
    # test) still cannot create a non-canonical predicate row.
    disposition = classify_predicate(predicate)
    if disposition.status == "reserved":
        return rejected(
            [
                envelope_error(
                    "RESERVED_PREDICATE",
                    (
                        f"Error: RESERVED_PREDICATE - '{predicate}' is reserved; it is created only by "
                        f"{disposition.lifecycle_tool}, never directly via manage_relation."
                    ),
                    "predicate",
                )
            ]
        )
    if disposition.status == "legacy_readonly":
        return rejected(
            [
                envelope_error(
                    "LEGACY_READONLY_PREDICATE",
                    (
                        f"Error: LEGACY_READONLY_PREDICATE - '{predicate}' edges are legacy; existing ones "
                        "remain readable but no new ones may be created."
                    ),
                    "predicate",
                )
            ]
        )
    if disposition.status == "alias":
        swap_note = (
            f" with source_id/target_id swapped (canonical direction is "
            f"'{disposition.canonical}' from the current target to the current source)"
            if disposition.swap
            else ""
        )
        return rejected(
            [
                envelope_error(
                    "NONCANONICAL_PREDICATE",
                    (
                        f"Error: NONCANONICAL_PREDICATE - '{predicate}' is not canonical; the canonical "
                        f"form is '{disposition.canonical}'{swap_note}. Retry with predicate="
                        f"'{disposition.canonical}'."
                    ),
                    "predicate",
                )
            ]
        )
    if disposition.status == "unknown":
        return rejected(
            [
                envelope_error(
                    "UNKNOWN_PREDICATE",
                    (
                        f"Error: UNKNOWN_PREDICATE - '{predicate}' is not part of the closed predicate "
                        f"vocabulary. Valid predicates: {sorted(AGENT_SELECTABLE_PREDICATES)}."
                    ),
                    "predicate",
                )
            ]
        )
    return None


def _close_owned_relation_connection(conn, should_close: bool) -> None:
    if should_close:
        close_connection(conn)


def store_relation(
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    valid_at: str | None = None,
    override_justification: str | None = None,
    agent_id: str = None,
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    _in_transaction: bool = False,
    _allow_core_elaborates_on: bool = False,
) -> dict:
    """Stores a directional relationship edge between two knowledge entities.

    Core-memory governance (see core_governance_service.py, resolved gap #1): an `elaborates_on`
    edge whose target is an active core memory may only be created through that core's own
    `core_detail_memory_ids` declaration (store_memory/commit_consolidation), never directly via
    this function -- `_allow_core_elaborates_on=True` is set ONLY by
    core_governance_service.reconcile_detail_relations' internal reconciliation calls, never by
    manage_relation or any other external caller. Re-submitting an edge that already exists
    remains an idempotent no-op regardless of this flag (checked before the guard below).

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
    is logged atomically (agent_id = agent_id or "system"). An already-active identical edge is
    always a no-op, checked BEFORE the gate -- re-submitting it never requires an override.
    """
    if not source_id or not target_id or not predicate:
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "Error: source_id, target_id, and predicate are mandatory parameters.",
                )
            ]
        )

    conn, db_path, should_close = _open_relation_connection(db_connection, db_path)

    resolved_source, resolved_target, validation_error = _resolve_store_relation_endpoints(
        conn, source_id, target_id
    )
    if validation_error is not None:
        _close_owned_relation_connection(conn, should_close)
        return validation_error

    predicate_error = _store_relation_predicate_error(predicate)
    if predicate_error is not None:
        _close_owned_relation_connection(conn, should_close)
        return predicate_error
    resolved_source = cast(str, resolved_source)
    resolved_target = cast(str, resolved_target)

    relation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        if _in_transaction:
            result_msg = _store_relation_write(
                conn,
                resolved_source,
                resolved_target,
                predicate,
                valid_at,
                override_justification,
                agent_id,
                relation_id,
                now,
                _allow_core_elaborates_on,
            )
        else:
            result_msg = write_transaction_retrying(
                conn,
                lambda tx_conn: _store_relation_write(
                    tx_conn,
                    resolved_source,
                    resolved_target,
                    predicate,
                    valid_at,
                    override_justification,
                    agent_id,
                    relation_id,
                    now,
                    _allow_core_elaborates_on,
                ),
            )
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path, coordinator=coordinator)
        return result_msg
    except Exception as e:
        logger.error("Error storing relation: %s", e)
        return rejected(
            [envelope_error(error_codes.INTERNAL_ERROR, f"Error storing relation: {e}")]
        )
    finally:
        if should_close:
            close_connection(conn)


def _invalidate_relation_write(
    conn,
    resolved_source: str,
    resolved_target: str,
    predicate: str,
    invalid_at: str | None,
    now: str,
) -> dict:
    normalized_requested = _normalize_predicate_name(predicate)
    canonical_predicate = resolve_or_create_predicate(conn, predicate) or predicate
    note = (
        f" [canonicalized: requested '{predicate}', stored as '{canonical_predicate}']"
        if predicate and normalized_requested and normalized_requested != canonical_predicate
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
        return rejected([envelope_error(error_codes.NOT_FOUND, "Error: relation not found")])

    rel_id, existing_invalid_at = existing
    if existing_invalid_at is not None:
        message = f"Relation already invalidated (no-op) at {existing_invalid_at} (ID: {rel_id})"
        return envelope_ok(
            {"relation_id": rel_id, "message": message},
            warnings=[envelope_warning(error_codes.ALREADY_DONE, message)],
        )

    effective_invalid_at = invalid_at or now
    conn.execute(
        "UPDATE relations SET invalid_at = ?, valid_to = ? WHERE id = ?",
        (effective_invalid_at, effective_invalid_at, rel_id),
    )
    message = (
        f"Relation invalidated: '{canonical_predicate}' between {resolved_source} "
        f"and {resolved_target} at {effective_invalid_at} (ID: {rel_id}){note}"
    )
    return envelope_ok({"relation_id": rel_id, "message": message})


def _open_relation_connection(db_connection, db_path: str | None):
    if db_connection:
        return db_connection, db_path, False
    resolved_path = db_path or get_db_path()
    return get_connection(resolved_path), resolved_path, True


def invalidate_relation(
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    invalid_at: str | None = None,
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    _in_transaction: bool = False,
) -> dict:
    """Invalidates an active relationship edge on the event/world-time axis (invalid_at).

    Does NOT touch valid_to (system/transaction time, driven by commit_consolidation).
    """
    if not source_id or not target_id or not predicate:
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "Error: source_id, target_id, and predicate are mandatory parameters.",
                )
            ]
        )

    conn, db_path, should_close = _open_relation_connection(db_connection, db_path)

    resolved_source, source_error = _resolve_relation_endpoint(conn, source_id, "source_id")
    resolved_target, target_error = _resolve_relation_endpoint(conn, target_id, "target_id")

    if not resolved_source or not resolved_target:
        if should_close:
            close_connection(conn)
        error_message = (
            source_error or target_error or "Error: Could not resolve target entity IDs."
        )

        error_code = _entity_ref_error_code(error_message)
        return rejected([envelope_error(error_code, error_message)])

    now = datetime.now(UTC).isoformat()
    try:
        if _in_transaction:
            result_msg = _invalidate_relation_write(
                conn, resolved_source, resolved_target, predicate, invalid_at, now
            )
        else:
            result_msg = write_transaction_retrying(
                conn,
                lambda tx_conn: _invalidate_relation_write(
                    tx_conn, resolved_source, resolved_target, predicate, invalid_at, now
                ),
            )
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path, coordinator=coordinator)
        return result_msg
    except Exception as e:
        logger.error("Error invalidating relation: %s", e)
        return rejected(
            [envelope_error(error_codes.INTERNAL_ERROR, f"Error invalidating relation: {e}")]
        )
    finally:
        if should_close:
            close_connection(conn)


def _assert_relation_transaction(conn, message: str) -> None:
    if not conn.in_transaction:
        raise RuntimeError(message)


class RelationOutcome(NamedTuple):
    kind: Literal["created", "duplicate", "invalidated"]
    item: dict[str, Any]


def _relation_rejection(result: dict[str, Any]) -> list[dict[str, Any]]:
    errors = result.get("errors") or [
        envelope_error(error_codes.INTERNAL_ERROR, "relation operation failed")
    ]
    internal_message = next(
        (
            entry.get("message", "relation operation failed")
            for entry in errors
            if entry.get("code") == error_codes.INTERNAL_ERROR
        ),
        None,
    )
    if internal_message is not None:
        raise RuntimeError(internal_message)
    return errors


def _prefix_relation_errors(errors: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    prefixed = []
    for entry in errors:
        inner_field = entry.get("field")
        field = f"relations[{index}].{inner_field}" if inner_field else f"relations[{index}]"
        prefixed.append({**entry, "field": field})
    return prefixed


def _apply_relation_item(
    tx_conn,
    item: dict[str, Any],
    agent_id: str | None,
    coordinator,
) -> RelationOutcome | list[dict[str, Any]]:
    is_invalidate = bool(item.get("invalidate", False))
    source_id = item.get("source_id")
    target_id = item.get("target_id")
    predicate = item.get("predicate")
    if is_invalidate:
        result = invalidate_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            invalid_at=item.get("invalid_at"),
            db_connection=tx_conn,
            coordinator=coordinator,
            _in_transaction=True,
        )
    else:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            valid_at=item.get("valid_at"),
            override_justification=item.get("override_justification"),
            agent_id=agent_id,
            db_connection=tx_conn,
            coordinator=coordinator,
            _in_transaction=True,
        )
    if result.get("status") == "rejected":
        return _relation_rejection(result)
    data = result.get("data") or {}
    is_duplicate = any(
        warning.get("code") == error_codes.ALREADY_DONE for warning in result.get("warnings", [])
    )
    kind: Literal["created", "duplicate", "invalidated"] = "created"
    if is_duplicate:
        kind = "duplicate"
    elif is_invalidate:
        kind = "invalidated"
    return RelationOutcome(
        kind,
        {
            "status": "duplicate" if is_duplicate else "success",
            "source_id": source_id,
            "target_id": target_id,
            "predicate": predicate,
            "action": "invalidate" if is_invalidate else "store",
            "result": data,
        },
    )


def _apply_relations_transaction(
    tx_conn,
    relations: list,
    agent_id: str | None,
    coordinator,
) -> dict:
    _assert_relation_transaction(tx_conn, "relation batch requires an active transaction")
    savepoint = f"relation_batch_{uuid.uuid4().hex}"
    tx_conn.execute(f"SAVEPOINT {savepoint}")
    batch_items: list[dict] = []
    batch_errors: list[dict] = []
    counts = {"created_count": 0, "duplicate_count": 0, "invalidated_count": 0}
    for index, item in enumerate(relations):
        if not isinstance(item, dict):
            batch_errors.append(
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "relation item must be an object",
                    f"relations[{index}]",
                )
            )
            continue
        outcome = _apply_relation_item(tx_conn, item, agent_id, coordinator)
        if isinstance(outcome, list):
            batch_errors.extend(_prefix_relation_errors(outcome, index))
            continue
        counts[f"{outcome.kind}_count"] += 1
        batch_items.append(outcome.item)
    if batch_errors:
        _assert_relation_transaction(tx_conn, "relation batch transaction ended before rollback")
        rollback_to_and_release(tx_conn, savepoint)
        return rejected(batch_errors)
    _assert_relation_transaction(tx_conn, "relation batch transaction ended before release")
    tx_conn.execute(f"RELEASE {savepoint}")
    return envelope_ok({"items": batch_items, **counts})


def apply_relations(
    relations: list,
    agent_id: str | None = None,
    db_connection=None,
    db_path: str | None = None,
    coordinator=None,
) -> dict:
    """Apply relation operations atomically, collecting expected rejections."""
    if not isinstance(relations, list) or not relations:
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "relations must be a non-empty array",
                    "relations",
                )
            ]
        )
    conn, resolved_path, should_close = _open_relation_connection(db_connection, db_path)
    try:
        result = write_transaction_retrying(
            conn,
            lambda tx_conn: _apply_relations_transaction(tx_conn, relations, agent_id, coordinator),
        )
        if result.get("status") == "ok" and (
            result["data"]["created_count"] or result["data"]["invalidated_count"]
        ):
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=resolved_path, coordinator=coordinator)
        return result
    except Exception as exc:
        logger.error("Error applying relation batch: %s", exc)
        if is_coordinator_connection(conn):
            raise
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(exc))])
    finally:
        _close_owned_relation_connection(conn, should_close)


def _dependency_cte_sql(
    direction: Literal["outbound", "inbound"],
    root_id: str,
    point_in_time: str,
    max_depth: int,
    agent_id: str | None,
) -> tuple[str, list[str | int]]:
    """Builds the recursive-CTE query for one traversal direction. Parametrized purely by
    string substitution of which endpoint anchors/recurses/guards -- for direction="outbound"
    this produces text byte-identical to the query this function replaces, so that path is a
    pure refactor with zero behavior change. "inbound" is the direction-reversed mirror: anchor
    on r.target_id (edges pointing INTO the root) instead of r.source_id, recurse by matching
    each new edge's target_id against the previous hop's source_id, and guard/accumulate the
    path using the newly-*discovered* node id at each step (r.source_id for inbound, since
    that's the node being walked toward moving away from root) -- not always r.target_id, which
    would silently check/record the wrong endpoint for inbound-discovered rows (a correctness
    bug in a first draft of this fix, caught before landing: it would let the cycle guard miss
    real cycles reached via the inbound arm)."""
    if direction == "outbound":
        anchor_where = "r.source_id = ?"
        recursive_join = "r.source_id = dt.target_id"
        newly_reached = "r.target_id"
    else:
        anchor_where = "r.target_id = ?"
        recursive_join = "r.target_id = dt.source_id"
        newly_reached = "r.source_id"
    visibility = (
        "AND EXISTS (SELECT 1 FROM entities e WHERE e.id = r.source_id "
        "AND (e.agent_id = ? OR e.scope = 'shared')) "
        "AND EXISTS (SELECT 1 FROM entities e WHERE e.id = r.target_id "
        "AND (e.agent_id = ? OR e.scope = 'shared'))"
        if agent_id is not None
        else ""
    )
    params: list[str | int] = [root_id, *([point_in_time] * 4)]
    if agent_id is not None:
        params.extend([agent_id, agent_id])
    params.extend([max_depth, *([point_in_time] * 4)])
    if agent_id is not None:
        params.extend([agent_id, agent_id])
    return (
        f"""
    WITH RECURSIVE dependency_tree(id, source_id, target_id, predicate, depth, path) AS (
        SELECT r.id, r.source_id, r.target_id, r.predicate, 1, r.source_id || '->' || {newly_reached}
        FROM relations r
        WHERE {anchor_where} AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
          AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
          AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
          AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?)) {visibility}

        UNION ALL

        SELECT r.id, r.source_id, r.target_id, r.predicate, dt.depth + 1, dt.path || '->' || {newly_reached}
        FROM relations r
        JOIN dependency_tree dt ON {recursive_join}
        WHERE dt.depth < ? AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
          AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
          AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
          AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?)) {visibility}
          AND dt.path NOT LIKE '%' || {newly_reached} || '%'
    )
    SELECT dt.id, dt.source_id, e1.title, dt.target_id, e2.title, dt.predicate, dt.depth, dt.path
    FROM dependency_tree dt
    JOIN entities e1 ON dt.source_id = e1.id
    JOIN entities e2 ON dt.target_id = e2.id
    ORDER BY dt.depth ASC;
    """,
        params,
    )


def _append_dependency_row(
    conn,
    direction: Literal["outbound", "inbound"],
    row: tuple,
    include_inspect: bool,
    max_depth: int,
    agent_id: str | None,
    seen_nodes: set[str],
    seen_edges: set[str],
    nodes: list[dict],
    edges: list[dict],
) -> None:
    rel_id, src_id, src_title, tgt_id, tgt_title, pred, depth, _raw_path = row
    # row[6] == depth
    reached_id, reached_title = (
        (tgt_id, tgt_title) if direction == "outbound" else (src_id, src_title)
    )
    # The node newly discovered by this hop is the target for an outbound row, but the
    # *source* for an inbound row -- inbound walks backward along each edge, so the node
    # moving away from root is the edge's source, not its target.
    if reached_id not in seen_nodes:
        node = {"id": reached_id, "title": reached_title, "depth": depth}
        if include_inspect:
            from saltmdb.domain.services.memory_service.lifecycle import _assemble_memory_record

            inspected = _assemble_memory_record(
                conn,
                reached_id,
                include_content=False,
                include_lineage=False,
                touch=False,
                max_depth=max_depth,
                agent_id=agent_id,
            )
            if inspected is not None:
                node.update(inspected)
        nodes.append(node)
        seen_nodes.add(reached_id)
    if rel_id in seen_edges:
        # Multiple converging paths in a diamond-shaped graph can revisit the same
        # relation once per incoming path (the CTE's cycle guard only stops a single
        # path from looping, not distinct paths reconverging). Dedupe on the relation's
        # own id, keeping the first (shallowest, per the sort above) occurrence.
        return
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


def _build_dependency_result(
    conn,
    root_id: str,
    root_info: dict,
    tagged_rows: list,
    include_inspect: bool,
    max_depth: int,
    agent_id: str | None,
    point_in_time: str,
) -> dict:
    tagged_rows.sort(key=lambda item: item[1][6])
    # Global shallowest-first ordering across both directions' independently-ordered result
    # sets, so the edge-dedup below keeps the shallowest occurrence regardless of which
    # direction's query happened to find a convergently-reachable relation first (matches
    # the single-query ORDER BY dt.depth ASC invariant the pre-existing dedup comment below
    # already documents for the outbound-only case).
    nodes = [{"id": root_id, "title": root_info.get("title"), "depth": 0}]
    if include_inspect:
        from saltmdb.domain.services.memory_service.lifecycle import _assemble_memory_record

        root_inspect = _assemble_memory_record(
            conn,
            root_id,
            include_content=False,
            include_lineage=False,
            touch=False,
            max_depth=max_depth,
            agent_id=agent_id,
        )
        if root_inspect is not None:
            nodes[0].update(root_inspect)
    seen_nodes = {root_id}
    edges: list[dict] = []
    seen_edges: set[str] = set()
    for direction, row in tagged_rows:
        _append_dependency_row(
            conn,
            direction,
            row,
            include_inspect,
            max_depth,
            agent_id,
            seen_nodes,
            seen_edges,
            nodes,
            edges,
        )
    return {
        "root": root_info,
        "total_dependencies_found": len(edges),
        "graph_exhausted": len(edges) == 0
        or max([e["depth"] for e in edges], default=0) < max_depth,
        "dependencies": nodes,
        "edges": edges,
        "point_in_time": point_in_time,
    }


def _fetch_dependency_rows(
    conn,
    direction: Literal["outbound", "inbound", "both"],
    root_id: str,
    point_in_time: str,
    max_depth: int,
    agent_id: str | None,
) -> list[tuple[Literal["outbound", "inbound"], tuple]]:
    directions: tuple[Literal["outbound", "inbound"], ...] = (
        ("outbound", "inbound") if direction == "both" else (direction,)
    )
    tagged_rows: list[tuple[Literal["outbound", "inbound"], tuple]] = []
    for current_direction in directions:
        sql, params = _dependency_cte_sql(
            current_direction, root_id, point_in_time, max_depth, agent_id
        )
        tagged_rows.extend((current_direction, row) for row in conn.execute(sql, params).fetchall())
    return tagged_rows


def analyze_dependencies(
    root_entity_id: str = None,
    max_depth: int = 5,
    point_in_time: str = None,
    direction: Literal["outbound", "inbound", "both"] = "outbound",
    db_connection=None,
    db_path: str = None,
    include_inspect: bool = False,
    agent_id: str | None = None,
) -> dict:
    """Recursively traces relational paths using SQL CTEs.

    direction="outbound" (default, unchanged behavior): downstream walk, following each edge's
    source_id -> target_id starting from the root -- this is the original, sole behavior before
    this parameter existed. direction="inbound": upstream walk, following edges backward
    (target_id -> source_id); an entity that is only ever a relation's *target* -- previously
    always reporting zero dependencies regardless of real inbound edges (cold-start review Issue
    B) -- now surfaces them. direction="both" runs the outbound and inbound queries
    independently (each keeping its own, already-correct single-direction cycle guard
    untouched) and unions/dedupes the results in Python. This is deliberately NOT a single
    unified mixed-direction graph traversal: a true zigzag path that changes direction mid-walk
    (e.g. A->B, C->B, C->D reached starting from A) is out of scope here -- the existing
    string-LIKE path cycle guard (already a documented source of one real prior bug in this
    file's supersession-chain resolution, see memory_service/ranking.py's own module docs) would
    need a harder redesign (tracking visited-node identity rather than a direction-specific path
    string) to do that safely, and isn't needed to fix the verified bug this option addresses.
    """
    if not root_entity_id:
        return {"error": "root_entity_id is mandatory"}

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    root_id, _root_candidates, _root_truncated = resolve_entity_ref(
        conn, root_entity_id, agent_id=agent_id
    )
    if not root_id:
        if should_close:
            close_connection(conn)
        return {"error": f"Could not resolve entity '{root_entity_id}'"}

    pit = point_in_time or datetime.now(UTC).isoformat()

    try:
        root_row = conn.execute(
            "SELECT id, title, status FROM entities WHERE id = ?"
            + (" AND (agent_id = ? OR scope = 'shared')" if agent_id is not None else ""),
            (root_id, agent_id) if agent_id is not None else (root_id,),
        ).fetchone()
        if not root_row and agent_id is not None:
            return {"error": f"Could not resolve entity '{root_entity_id}'"}
        root_info = (
            {"id": root_row[0], "title": root_row[1], "status": root_row[2]}
            if root_row
            else {"id": root_id, "title": "Root", "status": "raw"}
        )

        tagged_rows = _fetch_dependency_rows(conn, direction, root_id, pit, max_depth, agent_id)
        return _build_dependency_result(
            conn, root_id, root_info, tagged_rows, include_inspect, max_depth, agent_id, pit
        )
    except Exception as e:
        logger.error("Error analyzing dependencies: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


_LINEAGE_PREDICATES = ("revises", "supersedes", "consolidated_from")


def _lineage_node(conn, entity_id: str, depth: int = 0) -> dict:
    """Return the stable, non-content fields shared by graph responses."""
    row = conn.execute(
        "SELECT id, title, status, agent_id, updated_at FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        return {
            "id": entity_id,
            "title": "Unknown",
            "status": "unknown",
            "agent_id": None,
            "updated_at": None,
            "depth": depth,
            "generation_depth": depth,
        }
    return {
        "id": row[0],
        "title": row[1],
        "status": row[2],
        "agent_id": row[3],
        "updated_at": row[4],
        # Keep both names: depth is the graph contract, while generation_depth is
        # consumed by the existing viewer until its Phase 3 adapter is updated.
        "depth": depth,
        "generation_depth": depth,
    }


def _lineage_query(
    direction: Literal["ancestors", "descendants"],
    target_id: str,
    max_depth: int,
    point_in_time: str,
    agent_id: str | None,
) -> tuple[str, list[Any]]:
    predicate_placeholders = ",".join("?" for _ in _LINEAGE_PREDICATES)
    # The path is delimited so IDs cannot match as a substring of another ID.
    # Predicate names are constants from _LINEAGE_PREDICATES, never caller input.
    validity = """
            AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
            AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
            AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
            AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
        """
    visibility = (
        "AND EXISTS (SELECT 1 FROM entities e WHERE e.id = r.source_id "
        "AND (e.agent_id = ? OR e.scope = 'shared')) "
        "AND EXISTS (SELECT 1 FROM entities e WHERE e.id = r.target_id "
        "AND (e.agent_id = ? OR e.scope = 'shared'))"
        if agent_id is not None
        else ""
    )
    if direction == "ancestors":
        seed_join = "r.source_id = ?"
        next_join = "r.source_id = l.next_id"
        next_expression = "r.target_id"
    else:
        seed_join = "r.target_id = ?"
        next_join = "r.target_id = l.next_id"
        next_expression = "r.source_id"
    query = f"""
            WITH RECURSIVE lineage(
                relation_id, source_id, target_id, predicate, depth, next_id, path
            ) AS (
                SELECT r.id, r.source_id, r.target_id, r.predicate, 1,
                       {next_expression}, '|' || ? || '|' || {next_expression} || '|'
                FROM relations r
                WHERE {seed_join} AND r.predicate IN ({predicate_placeholders})
                  {validity} {visibility}
                UNION ALL
                SELECT r.id, r.source_id, r.target_id, r.predicate, l.depth + 1,
                       {next_expression}, l.path || {next_expression} || '|'
                FROM relations r JOIN lineage l ON {next_join}
                WHERE l.depth < ? AND r.predicate IN ({predicate_placeholders})
                  {validity} {visibility}
                  AND instr(l.path, '|' || {next_expression} || '|') = 0
            )
            SELECT relation_id, source_id, target_id, predicate, depth, next_id
            FROM lineage ORDER BY depth ASC, relation_id ASC
        """
    # Root is repeated in the path seed. Each validity predicate receives pit in
    # SQL order; keep the parameter construction explicit to avoid binding drift.
    params: list[Any] = [target_id, target_id, *_LINEAGE_PREDICATES, point_in_time]
    params.extend([point_in_time, point_in_time, point_in_time])
    if agent_id is not None:
        params.extend([agent_id, agent_id])
    params.extend([max_depth, *_LINEAGE_PREDICATES, point_in_time])
    params.extend([point_in_time, point_in_time, point_in_time])
    if agent_id is not None:
        params.extend([agent_id, agent_id])
    return query, params


def _assemble_lineage_result(
    conn,
    target_id: str,
    direction: Literal["ancestors", "descendants"],
    root_info: dict,
    rows: list,
    max_depth: int,
    point_in_time: str,
) -> dict:
    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    nodes_by_id: dict[str, dict] = {target_id: root_info}
    for relation_id, source_id, target_id_row, predicate, depth, next_id in rows:
        # dt.path (last column) is only needed by the SQL cycle guard (see CTE above) --
        # not serialized here. Callers reconstruct hierarchy from edges' source_id/target_id.
        if relation_id in seen_edges:
            continue
        seen_edges.add(relation_id)
        source_node = _lineage_node(conn, source_id, depth if source_id == next_id else 0)
        target_node = _lineage_node(conn, target_id_row, depth if target_id_row == next_id else 0)
        nodes_by_id.setdefault(source_id, source_node)
        nodes_by_id.setdefault(target_id_row, target_node)
        existing = nodes_by_id.get(next_id)
        # The first path is ordered shallowest; preserve that depth when a diamond
        # reaches a node through a second, longer path.
        if existing is None or depth < existing["depth"]:
            nodes_by_id[next_id] = _lineage_node(conn, next_id, depth)
        edges.append(
            {
                "relation_id": relation_id,
                "source_id": source_id,
                "target_id": target_id_row,
                "predicate": predicate,
                "depth": depth,
                "source_title": source_node["title"],
                "source_status": source_node["status"],
                "target_title": target_node["title"],
                "target_status": target_node["status"],
            }
        )
    nodes = sorted(nodes_by_id.values(), key=lambda n: (n["depth"], n["id"]))
    return {
        "entity_id": target_id,
        "direction": direction,
        "root": root_info,
        "nodes": nodes,
        "edges": edges,
        "total": len(edges),
        "total_nodes": len(nodes),
        "graph_exhausted": not edges or max(e["depth"] for e in edges) < max_depth,
        "point_in_time": point_in_time,
        "max_depth": max_depth,
    }


def _lineage_root_result(
    target_id: str,
    direction: Literal["ancestors", "descendants"],
    root_info: dict,
    point_in_time: str,
    max_depth: int,
) -> dict:
    return {
        "entity_id": target_id,
        "direction": direction,
        "root": root_info,
        "nodes": [root_info],
        "edges": [],
        "total": 0,
        "total_nodes": 1,
        "graph_exhausted": True,
        "point_in_time": point_in_time,
        "max_depth": max_depth,
    }


def _get_lineage_raw(  # noqa: PLR0911
    entity_id: str = None,
    direction: Literal["ancestors", "descendants"] = "ancestors",
    max_depth: int = 10,
    point_in_time: str = None,
    db_connection=None,
    db_path: str = None,
    agent_id: str | None = None,
) -> dict:
    """Traverse lifecycle lineage in either direction.

    Lifecycle edges point from a replacement to the version it replaces, i.e.
    ``new --revises/supersedes/consolidated_from--> old``.  Ancestor traversal follows
    ``source -> target``; descendant traversal follows the inverse relation direction.
    The bitemporal predicates are applied to every hop, and the path column prevents a
    malformed cyclic graph from causing unbounded recursion.  Archived entities are
    deliberately returned with their current status: explicit graph traversal is the
    sanctioned way to inspect historical material.
    """
    if not entity_id:
        return {"error": "entity_id is mandatory"}
    if direction not in ("ancestors", "descendants"):
        return {"error": "direction must be 'ancestors' or 'descendants'"}
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        return {"error": "max_depth must be a non-negative integer"}

    conn, db_path, should_close = _open_relation_connection(db_connection, db_path)

    target_id, _target_candidates, _target_truncated = resolve_entity_ref(
        conn, entity_id, agent_id=agent_id
    )
    if not target_id:
        if should_close:
            close_connection(conn)
        return {"error": f"Could not resolve entity '{entity_id}'"}

    pit = point_in_time or datetime.now(UTC).isoformat()

    try:
        root_info = _lineage_node(conn, target_id)
        if max_depth == 0:
            return _lineage_root_result(target_id, direction, root_info, pit, max_depth)
        query, params = _lineage_query(direction, target_id, max_depth, pit, agent_id)
        rows = conn.execute(query, params).fetchall()
        return _assemble_lineage_result(conn, target_id, direction, root_info, rows, max_depth, pit)
    except Exception as e:
        logger.error("Error analyzing lineage: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


def get_lineage(
    entity_id: str = None,
    direction: Literal["ancestors", "descendants"] = "ancestors",
    max_depth: int = 10,
    point_in_time: str = None,
    db_connection=None,
    db_path: str = None,
    agent_id: str | None = None,
) -> dict:
    """Envelope-shaped wrapper around :func:`_get_lineage_raw`.

    Kept as a separate thin function, not an in-place rewrite of the raw traversal, because
    three other services (`analyze_lineage` in this same module, `conflict_set_service`,
    `lineage_assembly_service`) reuse the raw bare-dict shape directly and must not be forced
    through envelope unwrapping on every internal call.
    """
    result = _get_lineage_raw(
        entity_id=entity_id,
        direction=direction,
        max_depth=max_depth,
        point_in_time=point_in_time,
        db_connection=db_connection,
        db_path=db_path,
        agent_id=agent_id,
    )
    if "error" in result:
        return rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])
    return envelope_ok(result)


def analyze_lineage(
    entity_id: str = None, point_in_time: str = None, db_connection=None, db_path: str = None
) -> dict:
    """Backward-compatible ancestor projection of :func:`get_lineage`.

    The viewer and older internal callers still consume ``ancestors`` and
    ``generation_depth``.  Keeping this thin projection lets the new graph contract land
    independently while those callers migrate to ``get_lineage``.
    """
    result = _get_lineage_raw(
        entity_id=entity_id,
        direction="ancestors",
        max_depth=10,
        point_in_time=point_in_time,
        db_connection=db_connection,
        db_path=db_path,
    )
    if "error" in result:
        return result
    ancestors = []
    for node in result["nodes"]:
        # Existing consumers expect the root in this list and no edge metadata.
        ancestors.append(
            {
                "id": node["id"],
                "title": node["title"],
                "status": node["status"],
                "agent_id": node["agent_id"],
                "updated_at": node["updated_at"],
                "generation_depth": node["depth"],
            }
        )
    return {
        "entity_id": result["entity_id"],
        "total_ancestors": max(len(ancestors) - 1, 0),
        "ancestors": ancestors,
        "point_in_time": result["point_in_time"],
    }


def get_related_memories(
    entity_id: str = None,
    max_depth: int = 5,
    point_in_time: str = None,
    direction: Literal["outbound", "inbound", "both"] = "both",
    db_connection=None,
    db_path: str = None,
    include_inspect: bool = False,
    agent_id: str | None = None,
) -> dict:
    """Named graph API for semantic neighbours, backed by ``analyze_dependencies``.

    Defaults to direction="both" (unlike analyze_dependencies' own outbound-only default) --
    this tool's name and its own docstring promise general relation lookup, not a directed
    downstream-only dependency walk, and an entity that is only ever a relation's target
    previously always reported zero related memories regardless of real inbound edges. See
    analyze_dependencies' docstring for exactly what "both" does and does not cover.
    """
    result = analyze_dependencies(
        root_entity_id=entity_id,
        max_depth=max_depth,
        point_in_time=point_in_time,
        direction=direction,
        include_inspect=include_inspect,
        agent_id=agent_id,
        db_connection=db_connection,
        db_path=db_path,
    )
    if "error" in result:
        return rejected([envelope_error(error_codes.VALIDATION_ERROR, result["error"])])
    payload = {key: value for key, value in result.items() if key != "dependencies"}
    payload["entity_id"] = result["root"]["id"]
    payload["related_memories"] = result["dependencies"]
    payload["total_related_found"] = result["total_dependencies_found"]
    payload["max_depth"] = max_depth
    return envelope_ok(payload)


def _resolve_and_filter_parent_ids(conn, parent_ids: list) -> list[str]:
    """Resolves each parent_id to its canonical entity id (via resolve_entity_ref, §4.4 --
    falls back to short hex-prefix matching when the exact/UUID/title pass doesn't land on a
    real row), dedupes, and drops any id that still doesn't resolve to an existing entity
    (including an ambiguous-prefix id, which resolve_entity_ref never guesses at -- silently
    dropped here exactly like an outright non-match; §3.8/§5.3 will replace this silent drop
    with a hard-fail-with-named-error in a later phase).

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
        res, _candidates, _truncated = resolve_entity_ref(conn, str(p))
        if res and res not in seen:
            seen.add(res)
            resolved_parents.append(res)

    return resolved_parents


def _validate_parent_reference(
    conn, raw_parent: object, seen: set[str]
) -> tuple[str | None, dict[str, str] | None]:
    raw = str(raw_parent)
    entity_id, candidates, truncated = resolve_entity_ref(conn, raw)
    if not entity_id:
        if candidates:
            return None, {
                "code": "AMBIGUOUS_PARENT_ID",
                "message": (
                    f"parent_id '{raw}' is ambiguous; it matches "
                    f"{len(candidates)}{'+' if truncated else ''} memories."
                ),
            }
        return None, {
            "code": "UNKNOWN_PARENT_ID",
            "message": f"could not resolve parent_id '{raw}'.",
        }
    if entity_id in seen:
        return entity_id, None
    seen.add(entity_id)
    # Defensive: resolve_entity_ref should only return existing rows, but retain the
    # hard-fail invariant if its implementation changes.
    row = conn.execute("SELECT status FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return None, {
            "code": "UNKNOWN_PARENT_ID",
            "message": f"could not resolve parent_id '{raw}'.",
        }
    if row[0] != "raw":
        return None, {
            "code": "INACTIVE_PARENT",
            "message": (
                f"parent '{entity_id}' is inactive (status='{row[0]}'); inspect its "
                "successor with get_lineage(direction='descendants') before retrying. "
                f"To correct stale/wrong information on an inactive (consolidated/archived) memory without reviving it, store a new store_memory fact with the correction, then manage_relation(predicate='corrects', source_id=<new memory>, target_id='{entity_id}') -- no consolidate_memories call is needed for a single correction."
            ),
        }
    return entity_id, None


def _resolve_and_validate_parent_ids(
    conn, parent_ids: list
) -> tuple[list[str], dict[str, str] | None]:
    """Resolve every submitted consolidation parent before any transaction starts.

    The old helper intentionally filtered unresolved IDs out.  That is unsafe for a
    destructive lifecycle operation: a typo could silently turn a requested merge into a
    different merge.  Keep the old helper for legacy bulk callers, but make the Phase 4 path
    fail closed with an actionable, named error for unknown, ambiguous, or inactive parents.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_parent in parent_ids or []:
        entity_id, parent_error = _validate_parent_reference(conn, raw_parent, seen)
        if parent_error is not None:
            return [], parent_error
        if entity_id is not None and entity_id not in resolved:
            resolved.append(entity_id)
    if len(resolved) < 2:
        return [], {
            "code": "REJECT_PARENT_COUNT",
            "message": (
                "consolidate_memories requires at least 2 distinct active parents; "
                "use revise_memory for a single parent."
            ),
        }
    return resolved, None


def _observe_parent_state(conn, entity_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Captures {entity_id: (content_hash, status)} for every id, eligible-status-filtered the
    same way cohesion_service.get_fresh_entity_centroids' fresh-join path is (`status !=
    'archived'`) -- gives single-parent consolidation (which never runs the cohesion gate, so it
    never gets a centroid-path observed_state) an equivalent pre-transaction snapshot for
    _do_commit's TOCTOU revalidation (resolved review finding #8). An id that is already archived
    at observation time deliberately gets NO entry here, exactly like an unresolved/archived
    parent in the >=2-parent centroid path -- so it fails the same "missing observed_state entry"
    revalidation in _do_commit, never silently passing because its archived status happened not
    to change again before commit."""
    if not entity_ids:
        return {}
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT id, content_hash, status FROM entities WHERE id IN ({placeholders}) "
        "AND status != 'archived'",
        entity_ids,
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@dataclass(frozen=True, slots=True)
class _BulkContext:
    precomputed_centroids: dict[str, list[float]] | None
    precomputed_unresolved: dict[str, str] | None
    precomputed_observed_state: dict[str, tuple[str, str]] | None


@dataclass(frozen=True)
class _ConsolidationCoreRequest:
    scope: str
    is_core_requested: bool | None
    core_reason: str | None
    core_exit_condition: str | None
    core_review_after: str | None
    detail_memory_ids: list[str] | None


@dataclass(frozen=True)
class _ConsolidationCohesionResult:
    override_applied: bool
    min_sim: float
    offending_pair: Any
    unresolved: dict[str, str]
    justification: str


@dataclass(frozen=True)
class _ConsolidationEntityValues:
    consolidated_id: str
    clean_title: str
    redacted_content: str
    agent_val: str
    scope: str
    weight: int
    context_id: str | None
    agent_session_id: str | None
    content_hash: str
    quality_score: float
    quality_status: str
    quality_flags_str: str
    metadata: dict[str, Any] | None
    memory_type: str | None
    now: str


def _consolidation_rejected(code: str, message: str) -> dict:
    """Build the Phase 4 mutation envelope for a validation rejection."""
    return rejected([envelope_error(code, message)])


def _consolidation_revalidate(conn, resolved_parents, observed_state):
    # A4: TOCTOU revalidation, first statements, before any destructive write. Re-fetch
    # content_hash/status for every resolved parent and compare against observed_state
    # -- the dict returned from the EXACT read that produced (or failed to produce)
    # each centroid, never a separately-later-taken snapshot. Any mismatch (content
    # changed, status no longer eligible, or a resolved parent with no observed_state
    # entry at all -- meaning it was unresolved/archived, or its read never completed
    # cleanly) aborts the whole transaction. This is also what catches an earlier item
    # in the same bulk batch having already archived a shared parent: that parent's
    # current status reads back 'archived', mismatching its recorded observed_state.
    # Resolved review finding #8: state revalidation must not be coupled to whether the
    # cohesion gate ran -- only the cosine-similarity comparison itself is legitimately
    # parent-count-gated. observed_state is now populated for every resolved parent
    # regardless of parent count (get_fresh_entity_centroids for >=2 parents, the
    # equivalent _observe_parent_state helper for a single parent), so this revalidation
    # runs unconditionally whenever there are resolved parents at all.
    if resolved_parents:
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


def _consolidation_core_state(
    conn,
    resolved_parents,
    core_request,
    redacted_content,
    entity_values,
):
    declaration = CoreDeclaration(
        is_core=core_request.is_core_requested,
        core_reason=core_request.core_reason,
        core_exit_condition=core_request.core_exit_condition,
        core_review_after=core_request.core_review_after,
        detail_memory_ids=core_request.detail_memory_ids,
    )
    return core_governance_service.resolve_derived_core_state(
        conn,
        source_ids=resolved_parents,
        declaration=declaration,
        content=redacted_content,
        scope=core_request.scope,
        new_entry={
            "id": entity_values.consolidated_id,
            "title": entity_values.clean_title,
            "memory_type": entity_values.memory_type or "fact",
            "core_reason": core_request.core_reason,
            "core_exit_condition": core_request.core_exit_condition,
            "core_review_after": core_request.core_review_after,
            "full_content": redacted_content,
            "agent_id": entity_values.agent_val,
        },
    )


def _consolidation_audit_override(
    conn,
    resolved_parents,
    override_applied,
    min_sim,
    offending_pair,
    unresolved,
    justification,
    consolidated_id,
    agent_val,
):
    # A3: atomic audit trail for an override commit -- written as the first destructive
    # step for fail-fast clarity. log_event catches its own exceptions and RETURNS an
    # "Error: ..." string rather than raising, so the result must be checked and raised
    # on here -- because this runs inside the surrounding write_transaction_retrying
    # (directly for a single-item call, or one level up in bulk_commit_consolidation for
    # a batch item), any exception raised anywhere in _do_commit rolls back the whole
    # transaction, which is what actually makes this audit atomic with the merge itself.
    if override_applied:
        audit_result = log_event(
            agent_id=agent_val,
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
        if envelope.is_rejected(audit_result):
            raise RuntimeError(
                "Failed to record consolidation override audit event: "
                f"{audit_result['errors'][0]['message']}"
            )


def _consolidation_insert_entity(
    conn,
    resolved_parents,
    core_state,
    entity_values,
):
    is_core_val = 1 if core_state["is_core"] else 0
    metadata_str = json.dumps(entity_values.metadata) if entity_values.metadata else None
    conn.execute(
        """
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, context_id, agent_session_id, last_touched_session_id, content_hash, quality_score, quality_status, quality_flags, metadata, memory_type, core_reason, core_exit_condition, core_review_after, core_detail_memory_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'consolidated', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'), ?, ?, ?, ?)
            """,
        (
            entity_values.consolidated_id,
            entity_values.now,
            entity_values.now,
            entity_values.now,
            entity_values.agent_val,
            entity_values.scope,
            is_core_val,
            entity_values.weight,
            json.dumps(resolved_parents),
            entity_values.clean_title,
            entity_values.redacted_content,
            entity_values.now,
            entity_values.context_id,
            entity_values.agent_session_id,
            entity_values.agent_session_id,
            entity_values.content_hash,
            entity_values.quality_score,
            entity_values.quality_status,
            entity_values.quality_flags_str,
            metadata_str,
            entity_values.memory_type,
            core_state["core_reason"],
            core_state["core_exit_condition"],
            core_state["core_review_after"],
            json.dumps(core_state["core_detail_memory_ids"])
            if core_state["core_detail_memory_ids"]
            else None,
        ),
    )

    if core_state["is_core"] and core_state["core_detail_memory_ids"]:
        core_governance_service.reconcile_detail_relations(
            conn,
            core_id=entity_values.consolidated_id,
            agent_id=entity_values.agent_val,
            new_detail_ids=core_state["core_detail_memory_ids"],
            previous_detail_ids=[],
        )

    return is_core_val


def _consolidation_archive_parents(
    conn,
    resolved_parents,
    consolidated_id,
    clean_title,
    redacted_content,
    content_hash,
    tags,
    agent_val,
    is_core_val,
    now,
    placeholders,
):
    # The new consolidated entity and all archived parents transition
    # with their durable embedding work in this one transaction.
    from saltmdb.domain.services.embedding_service import (
        cancel_embedding_jobs_for_entity,
        cancel_retrieval_embedding_jobs_for_entity,
        enqueue_embedding_jobs_for_entity,
    )

    enqueue_embedding_jobs_for_entity(
        conn, consolidated_id, clean_title, redacted_content, content_hash
    )

    if tags:
        for tag_name in tags:
            tag_id = resolve_or_create_tag(conn, tag_name, agent_id=agent_val)
            if not tag_id:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                (consolidated_id, tag_id),
            )

    if is_core_val:
        core_tag_id = resolve_or_create_tag(conn, "#core", agent_id=agent_val)
        if core_tag_id:
            conn.execute(
                "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                (consolidated_id, core_tag_id),
            )

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
        cancel_retrieval_embedding_jobs_for_entity(conn, parent_id, clear_vector=False)


def _clear_archived_parent_vectors(conn, parent_ids: list[str]) -> None:
    from saltmdb.domain.services.embedding_service import (
        cancel_retrieval_embedding_jobs_for_entity,
        clear_embedding_vectors_for_entity,
    )

    for parent_id in parent_ids:
        clear_embedding_vectors_for_entity(conn, parent_id)
        cancel_retrieval_embedding_jobs_for_entity(conn, parent_id, clear_vector=True)


def _consolidation_collect_worklist(conn, resolved_parents, orphaned_edges, placeholders):
    parent_set = set(resolved_parents)
    # Semantic relations remain historical claims about the archived parents.  Capture
    # them as an optional replay worklist, but do not close, copy, or repoint them.
    active_touching_rows = conn.execute(
        f"""
                SELECT id, source_id, target_id, predicate
                FROM relations
                WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                  AND valid_to IS NULL
                  AND predicate NOT IN ({",".join("?" for _ in _LINEAGE_PREDICATES)})
                """,
        resolved_parents + resolved_parents + list(_LINEAGE_PREDICATES),
    ).fetchall()
    seen_relation_ids: set[str] = set()
    for rel_id, src, tgt, pred in active_touching_rows:
        if rel_id in seen_relation_ids:
            continue
        seen_relation_ids.add(rel_id)
        originating_parent = src if src in parent_set else tgt
        other_endpoint = tgt if src in parent_set else src
        orphaned_edges.append(
            {
                "predicate": pred,
                "other_endpoint": other_endpoint,
                "originating_parent": originating_parent,
                # Preserve direction so an agent can replay this item through
                # manage_relation without guessing which side was the parent.
                "source_id": src,
                "target_id": tgt,
            }
        )


def _consolidation_insert_lineage(conn, resolved_parents, consolidated_id, now):
    for parent_id in resolved_parents:
        rel_id = str(uuid.uuid4())
        conn.execute(
            """
                    INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                    VALUES (?, ?, ?, 'consolidated_from', ?, ?)
                """,
            (rel_id, consolidated_id, parent_id, now, now),
        )


def _consolidation_commit(
    conn,
    resolved_parents,
    observed_state,
    core_request,
    cohesion_result,
    entity_values,
    tags,
    orphaned_edges,
):
    placeholders = ",".join("?" for _ in resolved_parents)
    _consolidation_revalidate(conn, resolved_parents, observed_state)
    core_state = _consolidation_core_state(
        conn, resolved_parents, core_request, entity_values.redacted_content, entity_values
    )
    _consolidation_audit_override(
        conn,
        resolved_parents,
        cohesion_result.override_applied,
        cohesion_result.min_sim,
        cohesion_result.offending_pair,
        cohesion_result.unresolved,
        cohesion_result.justification,
        entity_values.consolidated_id,
        entity_values.agent_val,
    )
    is_core_val = _consolidation_insert_entity(conn, resolved_parents, core_state, entity_values)
    _consolidation_archive_parents(
        conn,
        resolved_parents,
        entity_values.consolidated_id,
        entity_values.clean_title,
        entity_values.redacted_content,
        entity_values.content_hash,
        tags,
        entity_values.agent_val,
        is_core_val,
        entity_values.now,
        placeholders,
    )
    _consolidation_collect_worklist(conn, resolved_parents, orphaned_edges, placeholders)
    _consolidation_insert_lineage(
        conn, resolved_parents, entity_values.consolidated_id, entity_values.now
    )


def _consolidation_measure_cohesion(
    conn,
    resolved_parents,
    precomputed_centroids,
    precomputed_unresolved,
    precomputed_observed_state,
):
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
        centroids, unresolved = {}, {}
        # Resolved review finding #8: single-parent consolidation still needs a TOCTOU
        # observed_state snapshot even though it never runs the cohesion gate.
        observed_state = _observe_parent_state(conn, resolved_parents)
        min_sim, offending_pair = 1.0, None
    else:
        if (
            precomputed_centroids is not None
            and precomputed_unresolved is not None
            and precomputed_observed_state is not None
        ):
            centroids = precomputed_centroids
            unresolved = precomputed_unresolved
            observed_state = precomputed_observed_state
        else:
            centroids, unresolved, observed_state = get_fresh_entity_centroids(
                resolved_parents, conn
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

    return min_sim, offending_pair, unresolved, observed_state


def _consolidation_apply_cohesion_override(
    conn,
    content,
    override_justification,
    should_close,
    min_sim,
    offending_pair,
    unresolved,
):
    justification = (override_justification or "").strip()
    override_applied = min_sim < COHESION_MIN_PAIRWISE_THRESHOLD
    if override_applied and len(justification) < COHESION_OVERRIDE_MIN_LENGTH:
        if should_close:
            close_connection(conn)
        return _consolidation_rejected(
            "REJECT_LOW_COHESION",
            f"parent set fails pairwise similarity gate (min={min_sim:.4f} < "
            f"{COHESION_MIN_PAIRWISE_THRESHOLD}, weakest pair={offending_pair}"
            f"{', unresolved=' + str(unresolved) if unresolved else ''}). Pass "
            f"override_justification (>= {COHESION_OVERRIDE_MIN_LENGTH} chars) to force this merge.",
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

    return content, justification, override_applied


def _consolidation_cohesion(
    conn,
    resolved_parents,
    override_justification,
    should_close,
    precomputed_centroids,
    precomputed_unresolved,
    precomputed_observed_state,
    content,
):
    min_sim, offending_pair, unresolved, observed_state = _consolidation_measure_cohesion(
        conn,
        resolved_parents,
        precomputed_centroids,
        precomputed_unresolved,
        precomputed_observed_state,
    )
    override_result = _consolidation_apply_cohesion_override(
        conn,
        content,
        override_justification,
        should_close,
        min_sim,
        offending_pair,
        unresolved,
    )
    if isinstance(override_result, dict):
        return override_result
    content, justification, override_applied = override_result
    return (
        content,
        justification,
        override_applied,
        min_sim,
        offending_pair,
        unresolved,
        observed_state,
    )


def _consolidation_content_preflight(
    conn,
    title,
    content,
    is_core,
    agent_id,
    should_close,
):
    try:
        core_governance_service.parse_is_core(is_core)
    except ValueError as e:
        if should_close:
            close_connection(conn)
        return _consolidation_rejected("INVALID_CORE_REQUEST", str(e))

    redacted_content = redact_secrets(content)
    clean_title = redact_secrets(title)
    agent_val = agent_id or "system"
    # Execute Tier 1 & Tier 2 Quality Gate on consolidated content
    quality_res = evaluate_memory_quality(redacted_content)
    if quality_res["status"] == "REJECT":
        if should_close:
            close_connection(conn)
        return _consolidation_rejected(
            "REJECT_QUALITY",
            f"Consolidation quality check rejected (Score: {quality_res['quality_score']:.2f}). "
            f"Reason: {quality_res['reason']}",
        )

    content_hash = compute_content_hash(redacted_content)
    quality_score = quality_res["quality_score"]
    quality_status = quality_res["status"]
    quality_flags_str = json.dumps(quality_res["quality_flags"])

    return (
        redacted_content,
        clean_title,
        agent_val,
        content_hash,
        quality_score,
        quality_status,
        quality_flags_str,
    )


def _consolidation_duplicate_preflight(
    conn,
    resolved_parents,
    clean_title,
    redacted_content,
    content_hash,
    agent_val,
    should_close,
):
    # Stage A Exact Hash Collision Lookup (excluding resolved parent IDs)
    try:
        placeholders_p = ",".join("?" for _ in resolved_parents)
        query_sql = f"""
            SELECT id FROM entities
            WHERE content_hash = ? AND agent_id = ? AND status != 'archived'
              AND id NOT IN ({placeholders_p})
        """
        cursor = conn.execute(query_sql, [content_hash, agent_val] + resolved_parents)
        row = cursor.fetchone()
        if row:
            if should_close:
                close_connection(conn)
            return _consolidation_rejected(
                "REJECT_EXACT_DUPLICATE",
                f"Consolidated memory exact hash matches existing entity ID: {row[0]}",
            )
    except sqlite3.Error as exc:
        logger.warning(
            "Consolidation exact-hash lookup unavailable; continuing with duplicate checks: %s", exc
        )

    # Stage B Near-Duplicate Check (excluding resolved parent IDs)
    try:
        dup_check = check_duplicate_memories(
            title=clean_title,
            content=redacted_content,
            agent_id=agent_val,
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

    return None


def _consolidation_validate_request(parent_ids, title, content):
    if not parent_ids or not isinstance(parent_ids, list):
        return _consolidation_rejected(
            "INVALID_PARENT_IDS", "parent_ids must be a non-empty list of UUID strings."
        )
    if not title or not content:
        return _consolidation_rejected(
            "MISSING_REQUIRED_FIELDS", "title and content are mandatory."
        )
    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        return _consolidation_rejected(
            "CONTENT_TOO_LONG",
            f"content exceeds {MAX_MEMORY_CONTENT_CHARS} characters (got {len(content)}).",
        )

    return None


def _consolidation_open_and_validate_parents(parent_ids, tags, db_connection, db_path):
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    # This is deliberately before quality/dedup work and before BEGIN IMMEDIATE: all parent
    # errors are deterministic validation failures with zero side effects.
    resolved_parents, parent_error = _resolve_and_validate_parent_ids(conn, parent_ids)
    if parent_error is not None:
        if should_close:
            close_connection(conn)
        return _consolidation_rejected(parent_error["code"], parent_error["message"])

    # Same pre-BEGIN, zero-side-effects convention as the parent_ids check above.
    from saltmdb.domain.services.memory_service.tags import validate_tag_names

    tag_error = validate_tag_names(tags)
    if tag_error is not None:
        if should_close:
            close_connection(conn)
        return _consolidation_rejected("INVALID_TAG_NAME", tag_error)

    return conn, should_close, resolved_parents


def _consolidation_preflight(
    parent_ids,
    title,
    content,
    tags,
    agent_id,
    db_connection,
    db_path,
    override_justification,
    core_request,
    precomputed_centroids,
    precomputed_unresolved,
    precomputed_observed_state,
):
    validation_error = _consolidation_validate_request(parent_ids, title, content)
    if validation_error is not None:
        return validation_error
    open_result = _consolidation_open_and_validate_parents(parent_ids, tags, db_connection, db_path)
    if isinstance(open_result, dict):
        return open_result
    conn, should_close, resolved_parents = open_result

    cohesion_result = _consolidation_cohesion(
        conn,
        resolved_parents,
        override_justification,
        should_close,
        precomputed_centroids,
        precomputed_unresolved,
        precomputed_observed_state,
        content,
    )
    if isinstance(cohesion_result, dict):
        return cohesion_result
    (
        content,
        justification,
        override_applied,
        min_sim,
        offending_pair,
        unresolved,
        observed_state,
    ) = cohesion_result

    content_result = _consolidation_content_preflight(
        conn,
        title,
        content,
        core_request.is_core_requested,
        agent_id,
        should_close,
    )
    if isinstance(content_result, dict):
        return content_result
    (
        redacted_content,
        clean_title,
        agent_val,
        content_hash,
        quality_score,
        quality_status,
        quality_flags_str,
    ) = content_result

    duplicate_result = _consolidation_duplicate_preflight(
        conn,
        resolved_parents,
        clean_title,
        redacted_content,
        content_hash,
        agent_val,
        should_close,
    )
    if duplicate_result is not None:
        return duplicate_result

    return (
        conn,
        should_close,
        resolved_parents,
        justification,
        override_applied,
        min_sim,
        offending_pair,
        unresolved,
        observed_state,
        redacted_content,
        clean_title,
        agent_val,
        content_hash,
        quality_score,
        quality_status,
        quality_flags_str,
    )


def consolidate_memories(
    parent_ids: list[str],
    title: str,
    content: str,
    tags: list[str] = None,
    scope: Literal["private", "shared"] = "shared",
    weight: int = 1,
    override_justification: str | None = None,
    db_connection=None,
    db_path: str = None,
    *,
    provenance: Provenance | None = None,
    core: CoreDeclaration | None = None,
    annotations: EntityAnnotations | None = None,
    _bulk: _BulkContext | None = None,
) -> dict:
    """Create one canonical memory and archive its parents without semantic edge mutation.

    `_bulk` is set only by bulk_commit_consolidation, whose caller already holds an open write
    transaction around the whole batch; its presence skips the nested write transaction wrapper.
    The three precomputed maps in `_bulk` are forwarded unchanged to the cohesion preflight.

    A pairwise-cohesion gate runs before the write: parent_ids' chunk-embedding centroids must
    clear COHESION_MIN_PAIRWISE_THRESHOLD (MIN, not MEAN, pairwise cosine similarity) or the
    call is rejected with REJECT_LOW_COHESION, unless override_justification (>=
    COHESION_OVERRIDE_MIN_LENGTH chars) is supplied to force the merge (audited atomically, see
    _do_commit below). The precomputed maps exist only so bulk_commit_consolidation can hoist
    expensive centroid computation before its write transaction opens, while every item still
    routes through this same single code path.

    `annotations.metadata`/`annotations.memory_type` are carried through into the consolidated
    entity instead of being silently dropped. `provenance` groups writer, context, and session
    attribution. `core` groups the core-memory governance declaration.

    Core-memory governance (see core_governance_service.py): `core.is_core` is NEVER inherited
    from parents. If any resolved parent is currently an active core (is_core=1, status !=
    'archived') and `core.is_core` is omitted, the commit is rejected -- pass explicit
    `core.is_core=True` (with `core.core_reason`/`core.core_exit_condition`/`core.core_review_after`,
    optionally `core.detail_memory_ids`) to keep the result core, or `core.is_core=False` to let
    it become an ordinary memory. The authoritative core-state resolution, including capacity
    admission, happens fresh inside `_do_commit`'s own TOCTOU revalidation -- a pre-transaction
    check would not catch a parent whose core/status changed concurrently.
    """
    provenance = provenance or Provenance()
    core = core or CoreDeclaration()
    annotations = annotations or EntityAnnotations()
    core_request = _ConsolidationCoreRequest(
        scope,
        core.is_core,
        core.core_reason,
        core.core_exit_condition,
        core.core_review_after,
        core.detail_memory_ids,
    )
    preflight_result = _consolidation_preflight(
        parent_ids,
        title,
        content,
        tags,
        provenance.agent_id,
        db_connection,
        db_path,
        override_justification,
        core_request,
        _bulk.precomputed_centroids if _bulk is not None else None,
        _bulk.precomputed_unresolved if _bulk is not None else None,
        _bulk.precomputed_observed_state if _bulk is not None else None,
    )
    if isinstance(preflight_result, dict):
        return preflight_result
    (
        conn,
        should_close,
        resolved_parents,
        justification,
        override_applied,
        min_sim,
        offending_pair,
        unresolved,
        observed_state,
        redacted_content,
        clean_title,
        agent_val,
        content_hash,
        quality_score,
        quality_status,
        quality_flags_str,
    ) = preflight_result

    consolidated_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    orphaned_edges: list[dict[str, str]] = []
    cohesion = _ConsolidationCohesionResult(
        override_applied=override_applied,
        min_sim=min_sim,
        offending_pair=offending_pair,
        unresolved=unresolved,
        justification=justification,
    )
    entity_values = _ConsolidationEntityValues(
        consolidated_id=consolidated_id,
        clean_title=clean_title,
        redacted_content=redacted_content,
        agent_val=agent_val,
        scope=scope,
        weight=weight,
        context_id=provenance.context_id,
        agent_session_id=provenance.agent_session_id,
        content_hash=content_hash,
        quality_score=quality_score,
        quality_status=quality_status,
        quality_flags_str=quality_flags_str,
        metadata=annotations.metadata,
        memory_type=annotations.memory_type,
        now=now,
    )

    try:

        def _write(c):
            orphaned_edges.clear()
            _consolidation_commit(
                c,
                resolved_parents,
                observed_state,
                core_request,
                cohesion,
                entity_values,
                tags,
                orphaned_edges,
            )

        if _bulk is not None:
            _write(conn)
        else:

            def _commit_single(c):
                with savepoint(c, "consolidation"):
                    _write(c)
                _clear_archived_parent_vectors(c, resolved_parents)

            write_transaction_retrying(conn, _commit_single)

        return envelope_ok(
            {
                "entity_id": consolidated_id,
                "message": f"Successfully committed consolidated memory with ID: {consolidated_id}",
                "orphaned_relations": orphaned_edges,
                "orphaned_edge_worklist": orphaned_edges,
                "orphaned_edges": orphaned_edges,
                "worklist_guidance": (
                    "Re-declaring these relations through manage_relation is optional; "
                    "skipping them leaves a correct historical graph."
                ),
            }
        )
    except core_governance_service.CoreDecisionRequired as exc:
        keep = exc.keep_fragment
        return rejected(
            [
                envelope_error(
                    "CORE_DECISION_REQUIRED",
                    str(exc),
                    corrected_calls=[keep, {"demote_core": True}],
                )
            ]
        )
    except Exception as e:
        logger.error("Error committing consolidation: %s", e)
        return _consolidation_rejected("CONSOLIDATION_FAILED", str(e))
    finally:
        if should_close:
            close_connection(conn)


def commit_consolidation(*args, **kwargs) -> str:
    """Temporary internal compatibility alias for pre-Phase-4 callers.

    The public lifecycle operation is ``consolidate_memories`` and returns the response
    envelope/worklist.  Older internal services still consume the historical text response;
    keep that adapter local while those callers are migrated.
    """
    result = consolidate_memories(*args, **kwargs)
    if isinstance(result, dict):
        if result.get("status") == "ok":
            return result["data"]["message"]
        errors = result.get("errors") or [{"message": "consolidation rejected"}]
        first = errors[0]
        code = first.get("code", "CONSOLIDATION_REJECTED")
        message = first.get("message", "")
        # Preserve the historical text for callers/tests that still consume the alias while
        # keeping the new machine-readable code available from consolidate_memories.
        if code == "REJECT_QUALITY":
            return f"Error: {message}"
        return f"Error: {code} - {message}"
    return str(result)


class _BulkItemRejected(RuntimeError):
    def __init__(self, result: dict):
        self.errors = list(result.get("errors") or [])
        super().__init__(f"Bulk consolidation aborted (all-or-nothing): {result}")


def _commit_bulk_consolidation_item(
    conn,
    item: dict,
    parent_ids: list[str],
    centroids: dict,
    unresolved: dict,
    observed_state: dict,
    agent_id: str | None,
    context_id: str | None,
    agent_session_id: str | None,
) -> dict:
    item_centroids = {pid: centroids[pid] for pid in parent_ids if pid in centroids}
    item_unresolved = {pid: unresolved[pid] for pid in parent_ids if pid in unresolved}
    item_observed_state = {pid: observed_state[pid] for pid in parent_ids if pid in observed_state}
    result = consolidate_memories(
        # Per-item ownership is an internal compatibility affordance.  Public MCP
        # wrappers remove this field before crossing the adapter boundary; retaining
        # the item-level override here supports trusted in-process callers.
        parent_ids=parent_ids,
        title=cast(str, item.get("title")),
        content=cast(str, item.get("content")),
        tags=item.get("tags", []),
        scope=item.get("scope", "shared"),
        weight=item.get("weight", 1),
        override_justification=item.get("override_justification"),
        db_connection=conn,
        provenance=Provenance(
            agent_id=item.get("agent_id", agent_id),
            context_id=item.get("context_id", context_id),
            agent_session_id=agent_session_id,
        ),
        core=CoreDeclaration(
            is_core=item.get("is_core"),
            core_reason=item.get("core_reason"),
            core_exit_condition=item.get("core_exit_condition"),
            core_review_after=item.get("core_review_after"),
            detail_memory_ids=item.get("detail_memory_ids"),
        ),
        _bulk=_BulkContext(
            precomputed_centroids=item_centroids,
            precomputed_unresolved=item_unresolved,
            precomputed_observed_state=item_observed_state,
        ),
    )
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise _BulkItemRejected(result)
    data = result["data"]
    return {
        "status": "success",
        "entity_id": data["entity_id"],
        "title": item.get("title"),
        "data": data,
        "orphaned_relations": data.get("orphaned_relations", []),
        "orphaned_edge_worklist": data.get("orphaned_edge_worklist", []),
        "result": result,
    }


def _write_bulk_consolidations(
    conn,
    consolidations: list,
    item_resolved_parents: list[list[str]],
    centroids: dict,
    unresolved: dict,
    observed_state: dict,
    results: list,
    agent_id: str | None,
    context_id: str | None,
    agent_session_id: str | None,
) -> None:
    results.clear()
    archived_parent_ids: list[str] = []
    with savepoint(conn, "bulk_consolidation"):
        for item, parent_ids in zip(consolidations, item_resolved_parents):
            results.append(
                _commit_bulk_consolidation_item(
                    conn,
                    item,
                    parent_ids,
                    centroids,
                    unresolved,
                    observed_state,
                    agent_id,
                    context_id,
                    agent_session_id,
                )
            )
            archived_parent_ids.extend(parent_ids)
    _clear_archived_parent_vectors(conn, archived_parent_ids)


def _prepare_bulk_consolidation_parents(
    conn, consolidations: list
) -> tuple[list[list[str]], list[str], dict[str, object] | None]:
    item_resolved_parents: list[list[str]] = []
    union_ids: list[str] = []
    seen_union: set[str] = set()
    for index, item in enumerate(consolidations):
        parent_ids, parent_error = _resolve_and_validate_parent_ids(
            conn, item.get("parent_ids", [])
        )
        if parent_error is not None:
            return [], [], {**parent_error, "_item_index": index}
        item_resolved_parents.append(parent_ids)
        for parent_id in parent_ids:
            if parent_id not in seen_union:
                seen_union.add(parent_id)
                union_ids.append(parent_id)
    return item_resolved_parents, union_ids, None


def _prefix_consolidation_errors(errors: list[dict], index: int) -> list[dict]:
    prefixed = []
    for entry in errors:
        field = entry.get("field")
        if not field:
            suffix = "parent_memory_ids" if entry.get("code") == "CORE_DECISION_REQUIRED" else ""
            field = f"items[{index}].{suffix}".rstrip(".")
        if field.startswith("items["):
            prefixed.append({**entry, "field": field})
        else:
            prefixed.append({**entry, "field": f"items[{index}].{field}"})
    return prefixed


def _consolidation_batch_result(conn, results: list, resolved_parents: list[list[str]]) -> dict:
    items = []
    for index, result in enumerate(results):
        entity_id = result["entity_id"]
        title = conn.execute("SELECT title FROM entities WHERE id = ?", (entity_id,)).fetchone()[0]
        items.append(
            {
                "memory_id": entity_id,
                "title": title,
                "parent_memory_ids": resolved_parents[index],
                "orphaned_relations": result.get("orphaned_relations", []),
            }
        )
    return envelope_ok(
        {
            "items": items,
            "consolidated_count": len(items),
            "worklist_guidance": (
                "Re-declaring these relations through manage_relation is optional; "
                "skipping them leaves a correct historical graph."
            ),
        }
    )


def _normalize_consolidation_items(items: list) -> tuple[list[dict], dict | None]:
    if not isinstance(items, list) or not items:
        return [], rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR, "items must be a non-empty array", "items"
                )
            ]
        )
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return [], rejected(
                [
                    envelope_error(
                        error_codes.VALIDATION_ERROR,
                        "consolidation item must be an object",
                        f"items[{index}]",
                    )
                ]
            )
        normalized.append(
            {key: value for key, value in item.items() if value is not None and key != "agent_id"}
        )
    return normalized, None


def _validate_cross_item_parents(resolved: list[list[str]]) -> dict | None:
    owner_by_parent: dict[str, int] = {}
    for index, parent_ids in enumerate(resolved):
        for parent_id in parent_ids:
            earlier = owner_by_parent.get(parent_id)
            if earlier is not None:
                return rejected(
                    [
                        envelope_error(
                            "DUPLICATE_PARENT_ID",
                            f"parent '{parent_id}' is already used by item {earlier}.",
                            f"items[{index}].parent_memory_ids",
                        )
                    ]
                )
            owner_by_parent[parent_id] = index
    return None


def apply_consolidations(
    items: list,
    agent_id: str | None = None,
    context_id: str | None = None,
    agent_session_id: str | None = None,
    db_connection=None,
    db_path: str | None = None,
) -> dict:
    """Apply consolidation items atomically, preserving per-item rejection fields."""
    normalized, validation_error = _normalize_consolidation_items(items)
    if validation_error is not None:
        return validation_error

    with managed_connection(db_connection=db_connection, db_path=db_path) as conn:
        results: list[dict] = []
        try:
            resolved, union_ids, parent_error = _prepare_bulk_consolidation_parents(
                conn, normalized
            )
            if parent_error is not None:
                index = cast(int, parent_error.pop("_item_index", 0))
                return rejected([{**parent_error, "field": f"items[{index}].parent_memory_ids"}])
            parent_conflict = _validate_cross_item_parents(resolved)
            if parent_conflict is not None:
                return parent_conflict
            centroids, unresolved, observed_state = get_fresh_entity_centroids(union_ids, conn)
            write_transaction_retrying(
                conn,
                lambda tx_conn: _write_bulk_consolidations(
                    tx_conn,
                    normalized,
                    resolved,
                    centroids,
                    unresolved,
                    observed_state,
                    results,
                    agent_id,
                    context_id,
                    agent_session_id,
                ),
            )
            return _consolidation_batch_result(conn, results, resolved)
        except _BulkItemRejected as exc:
            return rejected(_prefix_consolidation_errors(exc.errors, len(results)))
        except Exception as exc:
            logger.error("Error applying consolidation batch: %s", exc)
            if is_coordinator_connection(conn):
                raise
            return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(exc))])


def bulk_commit_consolidation(
    consolidations: list,
    agent_id: str = None,
    context_id: str = None,
    agent_session_id: str = None,
    db_connection=None,
    db_path: str = None,
) -> list[dict]:
    """Executes multiple consolidation commits atomically in a single transaction -- all-or-nothing.

    If any item raises (or would otherwise be reported as an error), the whole batch rolls
    back, so no partial set of consolidations is ever left committed. Because a single
    failure unwinds every prior "successful" item in the same batch, a mixed per-item
    success/error list would misrepresent the outcome -- so on failure this returns a
    single top-level error result instead of claiming any individual items succeeded.

    `agent_id`/`context_id` are batch-wide defaults, applied to any item that doesn't set its
    own -- an item's own `agent_id`/`context_id` always wins when present, same override
    relationship `override_justification` already has to the batch. This mirrors
    `bulk_store_relations`'s per-item `agent_id` support (a batch can legitimately mix
    ownership) while still letting the common single-owner batch set it once instead of
    repeating it on every item.

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
        item_resolved_parents, union_ids, parent_error = _prepare_bulk_consolidation_parents(
            conn, consolidations
        )
        if parent_error is not None:
            return [
                {
                    "status": "error",
                    "error": f"{parent_error['code']}: {parent_error['message']}",
                }
            ]
        centroids, unresolved, observed_state = get_fresh_entity_centroids(union_ids, conn)

        write_transaction_retrying(
            conn,
            lambda tx_conn: _write_bulk_consolidations(
                tx_conn,
                consolidations,
                item_resolved_parents,
                centroids,
                unresolved,
                observed_state,
                results,
                agent_id,
                context_id,
                agent_session_id,
            ),
        )
        return results
    except Exception as e:
        logger.error(
            "Bulk commit consolidation error (batch rolled back, no items consolidated): %s", e
        )
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


# Canonical Phase 4 spelling for the bulk worker; the single-item operation above is the
# response-envelope source of truth.  Keep the old name for in-process callers during migration.
bulk_consolidate_memories = bulk_commit_consolidation


def _store_relation_batch_item(
    conn,
    relation: dict,
    agent_id: str | None,
    invalidate: bool,
) -> dict:
    source_id = relation.get("source_id")
    target_id = relation.get("target_id")
    predicate = relation.get("predicate")
    item_invalidate = bool(relation.get("invalidate", invalidate))
    if item_invalidate:
        result = invalidate_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            invalid_at=relation.get("invalid_at"),
            db_connection=conn,
            _in_transaction=True,
        )
        action = "invalidate"
    else:
        result = store_relation(
            source_id=source_id,
            target_id=target_id,
            predicate=predicate,
            valid_at=relation.get("valid_at"),
            override_justification=relation.get("override_justification"),
            agent_id=relation.get("agent_id", agent_id),
            db_connection=conn,
            _in_transaction=True,
        )
        action = "store"
    if envelope.is_rejected(result):
        raise RuntimeError(
            f"Bulk relation store aborted (all-or-nothing): {result['errors'][0]['message']}"
        )
    is_dup = any(
        warning.get("code") == error_codes.ALREADY_DONE for warning in result.get("warnings", [])
    )
    return {
        "status": "duplicate" if is_dup else "success",
        "source": source_id,
        "target": target_id,
        "predicate": predicate,
        "action": action,
        "result": result["data"],
    }


def bulk_store_relations(
    relations: list,
    db_connection=None,
    db_path: str = None,
    agent_id: str | None = None,
    invalidate: bool = False,
) -> list | dict:
    """Executes multiple relation insertions or invalidations atomically -- all-or-nothing.

    ``agent_id`` is the default attribution for the batch.  Trusted in-process callers may
    provide an ``agent_id`` on an individual item to intentionally mix ownership; the MCP
    adapter strips those per-item fields before dispatch, so public callers cannot override the
    configured adapter identity.

    Each item may set its own ``"invalidate": true`` key to invalidate that specific edge instead
    of creating it, using the item's ``source_id``/``target_id``/``predicate`` and an optional
    per-item ``"invalid_at"`` invalidation timestamp.  If an item omits ``"invalidate"``, it
    falls back to the batch-wide ``invalidate`` parameter.  This supports invalidation in the
    bulk shape, which previously could only create relations.

    If any item raises (or would otherwise be reported as an error), the whole batch rolls
    back, so no partial set of relations is ever left committed. Because a single failure
    unwinds every prior "successful" item in the same batch, a mixed per-item success/error
    list would misrepresent the outcome -- so on failure this returns a single top-level
    error result instead of claiming any individual items succeeded.
    """
    if not relations or not isinstance(relations, list):
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "relations must be a non-empty array of objects",
                    "relations",
                )
            ]
        )
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    results: list[Any] = []
    try:

        def _write(tx_conn):
            results.clear()
            for relation in relations:
                results.append(_store_relation_batch_item(tx_conn, relation, agent_id, invalidate))

        write_transaction_retrying(conn, _write)
        return results
    except Exception as e:
        logger.error("Bulk store relations error (batch rolled back, no items stored): %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)
