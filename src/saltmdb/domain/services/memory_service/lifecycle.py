"""Entity lifecycle operations for memory_service: fetch, touch, archive, and versioning.

Pure code-motion extraction (see refactor plan). No cross-module dependencies
within this package.
"""

import json
import sqlite3
import uuid
from datetime import datetime, UTC
from typing import Any, cast, Literal

from saltmdb.config import get_db_path
from saltmdb.db.connection import (
    get_connection,
    is_coordinator_connection,
    write_transaction_retrying,
    close_connection,
)
from saltmdb.utils.text import resolve_entity_id, resolve_id_prefix
from saltmdb.utils.text import resolve_entity_ref
from saltmdb.utils.text import large_content_descriptor
from saltmdb.utils.envelope import (
    error as envelope_error,
    ok as envelope_ok,
    rejected,
    warning as envelope_warning,
)
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.text import compute_content_hash

from ._shared import logger


_MEMORY_TYPES = ("fact", "event", "procedure", "decision", "preference")


class _LifecycleRejected(Exception):
    """Abort a replacement transaction while returning its structured rejection envelope."""

    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload)
        self.payload = payload


def _lineage_nodes(result, direction: str) -> list[dict]:
    """Extract the node list from either graph-service lineage shape.

    Phase 3 changes the graph API from ``analyze_lineage()['ancestors']`` to the
    direction-specific ``get_lineage`` result.  Keeping this small adapter here lets
    explicit-memory reads remain compatible while the daemon/viewer callers migrate.
    """
    if not isinstance(result, dict) or result.get("error") or result.get("status") == "rejected":
        return []
    data = result.get("data", {})
    candidates = data.get("nodes")
    if candidates is None:
        candidates = data.get(direction)
    if candidates is None:
        candidates = data.get("ancestors" if direction == "ancestors" else "descendants")
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


def _replacement_error(code: str, message: str, field: str | None = None) -> dict:
    """Keep replacement validation errors uniform and easy for MCP adapters to expose."""
    return rejected([envelope_error(code, message, field)])


def _validate_replacement_inputs(  # noqa: PLR0911
    entity_id: str | None,
    title: str | None,
    tags: list | None,
    content: str | None,
    reason: str | None,
    scope: str | None,
    memory_type: str | None,
) -> dict | None:
    """Validate all caller-controlled replacement fields before opening a write transaction."""
    if not entity_id or not isinstance(entity_id, str) or not entity_id.strip():
        return _replacement_error("MISSING_ENTITY_ID", "entity_id is mandatory.", "entity_id")
    if not isinstance(title, str) or not title.strip():
        return _replacement_error(
            "MISSING_TITLE", "title is mandatory and cannot be empty.", "title"
        )
    if not isinstance(content, str) or not content.strip():
        return _replacement_error(
            "MISSING_CONTENT", "content is mandatory and cannot be empty.", "content"
        )
    if not isinstance(reason, str) or not reason.strip():
        return _replacement_error(
            "MISSING_REASON", "reason is mandatory and cannot be empty.", "reason"
        )
    if tags is not None and (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
    ):
        return _replacement_error(
            "INVALID_TAGS", "tags must be a non-empty list of non-empty strings.", "tags"
        )
    if scope is not None and scope not in ("private", "shared"):
        return _replacement_error("INVALID_SCOPE", "scope must be 'private' or 'shared'.", "scope")
    if memory_type is not None and memory_type not in _MEMORY_TYPES:
        return _replacement_error(
            "INVALID_MEMORY_TYPE",
            "memory_type must be one of 'fact', 'event', 'procedure', 'decision', or 'preference'.",
            "memory_type",
        )
    return None


def _successor_details(entity_id: str, conn) -> tuple[list[dict], dict]:
    """Return active descendants and the complete lineage used in an inactive-target error."""
    lineage = _memory_lineage(entity_id, conn)
    descendants = [
        node
        for node in lineage.get("descendants", [])
        if node.get("status") not in (None, "archived")
    ]
    # Consolidated is an absorbing historical state, not a writable active successor.
    active = [node for node in descendants if node.get("status") == "raw"]
    return active, lineage


def _semantic_edge_worklist(conn, entity_id: str) -> list[dict[str, Any]]:
    """Capture active semantic edges before replacement archives their predecessor."""
    rows = conn.execute(
        """
        SELECT r.id, r.source_id, r.target_id, r.predicate,
               r.valid_from, r.valid_to, r.valid_at, r.invalid_at,
               src.title, src.status, tgt.title, tgt.status
        FROM relations r
        LEFT JOIN entities src ON src.id = r.source_id
        LEFT JOIN entities tgt ON tgt.id = r.target_id
        WHERE (r.source_id = ? OR r.target_id = ?)
          AND r.valid_to IS NULL
          AND r.predicate NOT IN ('revises', 'supersedes', 'consolidated_from')
        ORDER BY r.id
        """,
        (entity_id, entity_id),
    ).fetchall()
    worklist = []
    for row in rows:
        (
            relation_id,
            source_id,
            target_id,
            predicate,
            valid_from,
            valid_to,
            valid_at,
            invalid_at,
            source_title,
            source_status,
            target_title,
            target_status,
        ) = row
        other_id = target_id if source_id == entity_id else source_id
        worklist.append(
            {
                "relation_id": relation_id,
                "predicate": predicate,
                "originating_parent": entity_id,
                "source_id": source_id,
                "target_id": target_id,
                "other_endpoint": other_id,
                "source_title": source_title,
                "source_status": source_status,
                "target_title": target_title,
                "target_status": target_status,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "valid_at": valid_at,
                "invalid_at": invalid_at,
            }
        )
    return worklist


def _replacement_snapshot(conn, predecessor_id: str) -> tuple[list[str], dict[str, Any]]:
    """Read a complete entity row by column name, preserving unknown future columns on copy."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(entities)").fetchall()]
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (predecessor_id,)).fetchone()
    if row is None:
        return columns, {}
    return columns, dict(zip(columns, row))


def _insert_replacement_tags(conn, entity_id: str, tags: list, owner_id: str | None) -> None:
    """Resolve explicit new tags in the same transaction as the new entity."""
    from . import tags as tag_ops

    seen: set[str] = set()
    for tag in tags:
        normalized = tag.strip()
        key = normalized.lower().lstrip("#").replace("-", "").replace("_", "").replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        tag_id = tag_ops.resolve_or_create_tag(conn, normalized, agent_id=owner_id)
        if tag_id:
            conn.execute(
                "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                (entity_id, tag_id),
            )


def _replacement_operation(  # noqa: C901, PLR0911, PLR0912, PLR0915
    operation: Literal["revises", "supersedes"],
    *,
    entity_id: str | None = None,
    title: str | None = None,
    tags: list | None = None,
    content: str | None = None,
    reason: str | None = None,
    owner_id: str | None = None,
    context_id: str | None = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    agent_session_id: str | None = None,
    metadata: dict | None = None,
    repoint_relations: bool = False,
    db_connection=None,
    db_path: str | None = None,
    _in_transaction: bool = False,
) -> dict:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Atomically create an immutable replacement and archive its predecessor.

    All validation and inactive-target checks happen before the write transaction.  The callback
    repeats the status check under the write lock, then inserts the lifecycle edge *after* archiving
    the predecessor so that the new lineage edge remains active and semantic history is untouched.

    ``repoint_relations=True`` (opt-in, default False) additionally repoints every active
    semantic edge touching the predecessor onto the new entity, in the same transaction: each edge
    `_semantic_edge_worklist` would otherwise only report as ``orphaned_semantic_edges`` is
    invalidated and an equivalent edge recreated with the predecessor id swapped for the new id in
    whichever endpoint (source and/or target) it occupied, using fresh timestamps rather than the
    old edge's -- never backdating the new edge's validity window. No predicate or direction
    filtering -- every non-lifecycle edge found is repointed unconditionally, since the caller's
    own decision to pass this flag is already the safety gate. Skips the RELATION_GATE_*
    similarity/contradiction governance in `relation_service.store_relation`: this is identity
    continuity for an already-approved edge, not a new semantic claim being asserted.

    This default-False/opt-in shape is deliberately compatible with the standing safe-degradation
    law (saltmdb project's own SALTMDB memory `7af33335`: a lazy caller must get a correct-if-
    sparse graph, never a richer-but-fabricated one) -- it is the "optional cooperative path" that
    law's own corollary describes, not a reopening of the original no-auto-repoint decision. See
    memory `60fca8c8` for the full rationale, including the related known gap `213f80c5` (the
    previously-documented manual `orphaned_semantic_edges` workaround leaves the old edge
    un-invalidated) that this flag closes for any caller who opts into it.
    """
    validation_error = _validate_replacement_inputs(
        entity_id, title, tags, content, reason, scope, memory_type
    )
    if validation_error is not None:
        return validation_error
    # The validator above establishes these invariants. Casts carry that fact to static checking
    # without runtime assertions that disappear under optimized Python.
    entity_id = cast(str, entity_id)
    title = cast(str, title)
    content = cast(str, content)
    reason = cast(str, reason)

    should_close = False
    conn = db_connection
    if conn is None:
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
            return _replacement_error(
                "UNKNOWN_ENTITY_ID", f"No memory matches entity_id '{entity_id}'.", "entity_id"
            )

        target = conn.execute("SELECT status FROM entities WHERE id = ?", (resolved_id,)).fetchone()
        if target is None:
            return _replacement_error(
                "UNKNOWN_ENTITY_ID", f"No memory matches entity_id '{entity_id}'.", "entity_id"
            )
        if target[0] != "raw":
            successors, lineage = _successor_details(resolved_id, conn)
            return rejected(
                [
                    envelope_error(
                        "INACTIVE_TARGET",
                        f"Memory '{resolved_id}' is inactive ({target[0]}). Fetch an active successor and inspect it before retrying; no replacement was created. To correct stale/wrong information on an inactive memory without reviving it, store a new store_memory fact with the correction, then manage_relation(predicate='corrects', source_id=<new memory>, target_id='{resolved_id}') -- no revise_memory/supersede_memory call is possible on an inactive target.",
                        "entity_id",
                    )
                ],
                effective={
                    "target_id": resolved_id,
                    "status": target[0],
                    "active_successors": successors,
                    "lineage": lineage,
                },
            )

        columns, before = _replacement_snapshot(conn, resolved_id)
        from . import tags as tag_ops

        inherited_tags = tag_ops.list_entity_tags(conn, resolved_id) if tags is None else None
        if not before:
            return _replacement_error(
                "UNKNOWN_ENTITY_ID", f"No memory matches entity_id '{entity_id}'.", "entity_id"
            )
        orphaned_edges = _semantic_edge_worklist(conn, resolved_id)
        new_title = redact_secrets(title.strip())
        new_content = redact_secrets(content)
        # validate_memory_input raises only for title bounds/metadata shape; its error is safe to
        # return before BEGIN and therefore guarantees zero side effects on malformed requests.
        try:
            from .validation import validate_memory_input

            validate_memory_input(
                new_title, new_content, metadata if metadata is not None else before.get("metadata"), tags
            )
        except ValueError as exc:
            return _replacement_error("INVALID_MEMORY", str(exc))

        now = datetime.now(UTC).isoformat()
        content_hash = compute_content_hash(new_content)
        quality = evaluate_memory_quality(new_content, new_title)
        if quality.get("status") == "REJECT":
            return _replacement_error(
                "MEMORY_QUALITY_REJECTED",
                f"Memory quality check rejected (Score: {quality.get('quality_score', 0):.2f}). "
                f"Reason: {quality.get('reason', 'quality requirements were not met')}",
            )
        inherited_owner = before.get("owner_id") if owner_id is None else owner_id
        inherited_context = before.get("context_id") if context_id is None else context_id
        inherited_scope = before.get("scope") if scope is None else scope
        inherited_type = before.get("memory_type") or "fact" if memory_type is None else memory_type
        inherited_metadata = before.get("metadata") if metadata is None else json.dumps(metadata)

        effective_tags = inherited_tags if tags is None else tags
        if effective_tags is None:
            effective_tags = []
        inherited: dict[str, Any] = {
            field: value
            for field, value, supplied in (
                ("tags", inherited_tags, tags is not None),
                ("owner_id", inherited_owner, owner_id is not None),
                ("context_id", inherited_context, context_id is not None),
                ("scope", inherited_scope, scope is not None),
                ("memory_type", inherited_type, memory_type is not None),
            )
            if not supplied
        }
        changed: dict[str, Any] = {
            "title": new_title,
            "reason": reason.strip(),
        }
        changed.update(
            {
                field: value
                for field, value, supplied in (
                    ("tags", list(effective_tags), tags is not None),
                    ("owner_id", inherited_owner, owner_id is not None),
                    ("context_id", inherited_context, context_id is not None),
                    ("scope", inherited_scope, scope is not None),
                    ("memory_type", inherited_type, memory_type is not None),
                    ("metadata", inherited_metadata, metadata is not None),
                )
                if supplied
            }
        )

        def _write(c):
            current = c.execute(
                "SELECT status FROM entities WHERE id = ?", (resolved_id,)
            ).fetchone()
            if current is None:
                raise _LifecycleRejected(
                    _replacement_error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{resolved_id}'.",
                        "entity_id",
                    )
                )
            if current[0] != "raw":
                successors, lineage = _successor_details(resolved_id, c)
                raise _LifecycleRejected(
                    rejected(
                        [
                            envelope_error(
                                "INACTIVE_TARGET",
                                f"Memory '{resolved_id}' became inactive ({current[0]}) before replacement; retry against an active successor. To correct stale/wrong information on an inactive memory without reviving it, store a new store_memory fact with the correction, then manage_relation(predicate='corrects', source_id=<new memory>, target_id='{resolved_id}') -- no revise_memory/supersede_memory call is possible on an inactive target.",
                                "entity_id",
                            )
                        ],
                        effective={
                            "target_id": resolved_id,
                            "status": current[0],
                            "active_successors": successors,
                            "lineage": lineage,
                        },
                    )
                )

            # Re-read the frozen source under the lock.  If a concurrent writer changed any
            # source column, abort rather than silently copying a stale predecessor snapshot.
            _, locked_before = _replacement_snapshot(c, resolved_id)
            for frozen in (
                "title",
                "full_content",
                "owner_id",
                "context_id",
                "scope",
                "memory_type",
                "created_at",
                "content_hash",
                "metadata",
                "parent_ids",
                "valid_from",
            ):
                if locked_before.get(frozen) != before.get(frozen):
                    raise _LifecycleRejected(
                        _replacement_error(
                            "TARGET_CHANGED",
                            "Target changed while replacement was being prepared; retry after fetching it again.",
                            "entity_id",
                        )
                    )

            new_id = str(uuid.uuid4())
            replacement = dict(locked_before)
            replacement.update(
                {
                    "id": new_id,
                    "created_at": now,
                    "updated_at": now,
                    "last_accessed_at": now,
                    "owner_id": inherited_owner,
                    "context_id": inherited_context,
                    "scope": inherited_scope,
                    "memory_type": inherited_type,
                    "metadata": inherited_metadata,
                    "status": "raw",
                    "embedding_status": "pending",
                    "title": new_title,
                    "full_content": new_content,
                    "valid_from": now,
                    "valid_to": None,
                    "content_hash": content_hash,
                    "agent_session_id": agent_session_id,
                    "last_touched_session_id": agent_session_id,
                    "quality_score": quality.get("quality_score"),
                    "quality_status": quality.get("status"),
                    "quality_flags": json.dumps(quality.get("quality_flags", [])),
                }
            )
            insert_columns = [column for column in columns if column in replacement]
            placeholders = ", ".join("?" for _ in insert_columns)
            c.execute(
                f"INSERT INTO entities ({', '.join(insert_columns)}) VALUES ({placeholders})",
                [replacement[column] for column in insert_columns],
            )
            _insert_replacement_tags(c, new_id, effective_tags, inherited_owner)

            archived_at = now
            c.execute(
                "UPDATE entities SET status = 'archived', embedding_status = 'archived', updated_at = ?, valid_to = ? WHERE id = ? AND status = 'raw'",
                (archived_at, archived_at, resolved_id),
            )
            from saltmdb.domain.services.embedding_service import (
                cancel_embedding_jobs_for_entity,
                cancel_retrieval_embedding_jobs_for_entity,
                clear_embedding_vectors_for_entity,
                enqueue_embedding_jobs_for_entity,
                enqueue_retrieval_embedding_job_for_entity,
            )

            cancel_embedding_jobs_for_entity(c, resolved_id)
            clear_embedding_vectors_for_entity(c, resolved_id)
            cancel_retrieval_embedding_jobs_for_entity(c, resolved_id, clear_vector=True)
            enqueue_embedding_jobs_for_entity(c, new_id, new_title, new_content, content_hash)
            enqueue_retrieval_embedding_job_for_entity(
                c,
                new_id,
                replacement.get("retrieval_text"),
                replacement.get("retrieval_text_hash"),
                force=True,
            )
            relation_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (relation_id, new_id, resolved_id, operation, now, now, now),
            )

            repointed: list[dict[str, Any]] = []
            still_orphaned_ids: set[str] = set()
            if repoint_relations:
                # Re-read under the write lock rather than trusting the pre-transaction
                # `orphaned_edges` snapshot -- mirrors the frozen-column re-check above, so a
                # relation created/invalidated concurrently between validation and this lock is
                # not silently missed or double-handled.
                for edge in _semantic_edge_worklist(c, resolved_id):
                    new_source = new_id if edge["source_id"] == resolved_id else edge["source_id"]
                    new_target = new_id if edge["target_id"] == resolved_id else edge["target_id"]
                    if new_source == new_target:
                        # A pre-existing self-loop on the predecessor would become a
                        # self-referential edge on the new entity, which store_relation's own
                        # guard forbids elsewhere. Leave this one edge completely untouched
                        # (still an active edge on the archived predecessor, still reported as
                        # orphaned) for the caller to resolve by hand, rather than fabricating a
                        # disallowed edge or invalidating it with no replacement.
                        still_orphaned_ids.add(edge["relation_id"])
                        continue
                    c.execute(
                        "UPDATE relations SET invalid_at = ?, valid_to = ? WHERE id = ?",
                        (now, now, edge["relation_id"]),
                    )
                    new_relation_id = str(uuid.uuid4())
                    insert_cursor = c.execute(
                        """
                        INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, target_id, predicate) WHERE valid_to IS NULL DO NOTHING
                        """,
                        (new_relation_id, new_source, new_target, edge["predicate"], now, now, now),
                    )
                    if insert_cursor.rowcount == 0:
                        # An active edge with this exact (source, target, predicate) triple
                        # already exists -- the old edge above is still correctly invalidated
                        # (it's now redundant with the pre-existing one), but don't report a
                        # new_relation_id that was never actually written.
                        continue
                    repointed.append(
                        {
                            "old_relation_id": edge["relation_id"],
                            "new_relation_id": new_relation_id,
                            "source_id": new_source,
                            "target_id": new_target,
                            "predicate": edge["predicate"],
                        }
                    )
            return new_id, relation_id, repointed, still_orphaned_ids

        try:
            if _in_transaction:
                new_id, relation_id, repointed_relations, still_orphaned_ids = _write(conn)
            else:
                new_id, relation_id, repointed_relations, still_orphaned_ids = write_transaction_retrying(
                    conn, _write
                )
        except _LifecycleRejected as exc:
            return exc.payload
        # A large new body echoed back verbatim risks tripping the calling MCP client's own
        # response-size limit (confirmed live 2026-09-14 on a ~94KB supersede_memory response) --
        # dump it to a file past the threshold, same as get_memory's full-content read path. Done
        # only now, after the write has actually committed, so a rejected write (TARGET_CHANGED/
        # INACTIVE_TARGET) never leaves an orphaned dump file with nothing referencing it.
        changed.update(large_content_descriptor(new_content))
        response_orphaned_edges = (
            [edge for edge in orphaned_edges if edge["relation_id"] in still_orphaned_ids]
            if repoint_relations
            else orphaned_edges
        )
        return envelope_ok(
            {
                "old_id": resolved_id,
                "new_id": new_id,
                "relation_id": relation_id,
                "predicate": operation,
                "reason": reason.strip(),
                "inherited": inherited,
                "changed": changed,
                "inherited_fields": list(inherited),
                "changed_fields": list(changed),
                "orphaned_semantic_edges": response_orphaned_edges,
                "semantic_relations_repointed": bool(repoint_relations),
                "repointed_relations": repointed_relations,
            }
        )
    except Exception as exc:
        logger.error("Error %sing memory: %s", operation, exc)
        return rejected([envelope_error("LIFECYCLE_WRITE_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def revise_memory(
    entity_id: str = None,
    title: str = None,
    tags: list = None,
    content: str = None,
    reason: str = None,
    owner_id: str = None,
    context_id: str = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    agent_session_id: str | None = None,
    metadata: dict = None,
    repoint_relations: bool = False,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> dict:
    """Create a corrected representation with ``new --revises--> old`` lineage.

    See ``_replacement_operation`` for ``repoint_relations``.
    """
    return _replacement_operation(
        "revises",
        entity_id=entity_id,
        title=title,
        tags=tags,
        content=content,
        reason=reason,
        owner_id=owner_id,
        context_id=context_id,
        scope=scope,
        memory_type=memory_type,
        agent_session_id=agent_session_id,
        metadata=metadata,
        repoint_relations=repoint_relations,
        db_connection=db_connection,
        db_path=db_path,
        _in_transaction=_in_transaction,
    )


def supersede_memory(
    entity_id: str = None,
    title: str = None,
    tags: list = None,
    content: str = None,
    reason: str = None,
    owner_id: str = None,
    context_id: str = None,
    scope: Literal["private", "shared"] | None = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] | None = None,
    agent_session_id: str | None = None,
    metadata: dict = None,
    repoint_relations: bool = False,
    db_connection=None,
    db_path: str = None,
) -> dict:
    """Create newer knowledge with ``new --supersedes--> old`` lineage.

    See ``_replacement_operation`` for ``repoint_relations``.
    """
    return _replacement_operation(
        "supersedes",
        entity_id=entity_id,
        title=title,
        tags=tags,
        content=content,
        reason=reason,
        owner_id=owner_id,
        context_id=context_id,
        scope=scope,
        memory_type=memory_type,
        agent_session_id=agent_session_id,
        metadata=metadata,
        repoint_relations=repoint_relations,
        db_connection=db_connection,
        db_path=db_path,
    )


def _assemble_memory_record(
    conn,
    resolved_id: str,
    entity_id: str,
    *,
    include_content: bool,
    include_lineage: bool,
    touch: bool = True,
    max_depth: int = 10,
) -> dict | None:
    """Shared field-assembly for get_memory / inspect_memory / get_related_memories's
    include_inspect=True embedding. Returns None if resolved_id no longer resolves to a row
    (caller already resolved it moments earlier via resolve_entity_ref -- this guards a rare
    concurrent-delete race, mirroring get_memory's own pre-existing "not row" check).

    include_content=True includes the full `content` field (get_memory's contract);
    include_content=False includes a `snippet` field instead, via the same
    extract_title_and_snippet() heuristic search_memory already falls back to when it has no
    query to center a snippet on (inspect_memory's contract -- no query at all, always this
    path).

    include_lineage=True runs the (potentially expensive, two bounded graph queries)
    _memory_lineage() computation -- an already-accepted cost for a standalone get_memory or
    inspect_memory call. include_lineage=False skips it entirely -- used only when this helper
    is embedded once per related entity inside a get_related_memories traversal, where running
    lineage per-entity would multiply that cost by however many related entities were found.

    touch=True bumps last_accessed_at (get_memory's and inspect_memory's own standalone-call
    contract: "explicit retrieval is still a memory access"). touch=False skips it -- used only
    by the get_related_memories embedding, where bumping last_accessed_at on every related
    entity as a side effect of one traversal call would be a surprising, unrequested side
    effect entirely separate from actually looking at that entity.
    """
    row = conn.execute(
        """
        SELECT id, title, full_content, status, created_at, updated_at,
               last_accessed_at, owner_id, scope, is_core, parent_ids,
               valid_from, valid_to, metadata, context_id, memory_type,
               quality_score, quality_status, quality_flags,
               agent_session_id, last_touched_session_id
        FROM entities WHERE id = ?
        """,
        (resolved_id,),
    ).fetchone()
    if not row:
        return None

    accessed_at = row[6]
    if touch:
        accessed_at = datetime.now(UTC).isoformat()
        try:
            conn.execute(
                "UPDATE entities SET last_accessed_at = ? WHERE id = ?",
                (accessed_at, resolved_id),
            )
            if not is_coordinator_connection(conn):
                conn.commit()
        except sqlite3.OperationalError as touch_exc:
            if "readonly database" not in str(touch_exc).lower():
                raise
            accessed_at = row[6]  # touch skipped; report the untouched stored value
            logger.debug(
                "_assemble_memory_record: skipped last_accessed_at touch on read-only "
                "connection for %s",
                resolved_id,
            )

    from . import tags as tag_ops

    data = {
        "id": row[0],
        "title": row[1],
        "status": row[3],
        "created_at": row[4],
        "updated_at": row[5],
        "last_accessed_at": accessed_at,
        "owner_id": row[7],
        "scope": row[8],
        "is_core": bool(row[9]),
        "parent_ids": json.loads(row[10]) if row[10] else [],
        "valid_from": row[11],
        "valid_to": row[12],
        "metadata": json.loads(row[13]) if row[13] else {},
        "context_id": row[14],
        "memory_type": row[15],
        "quality_score": row[16],
        "quality_status": row[17],
        "quality_flags": json.loads(row[18]) if row[18] else [],
        "agent_session_id": row[19],
        "last_touched_session_id": row[20],
        "tags": tag_ops.list_entity_tags(conn, resolved_id),
    }
    if include_content:
        data.update(large_content_descriptor(row[2]))
    else:
        from saltmdb.utils.text import extract_title_and_snippet

        _, snippet = extract_title_and_snippet(row[2])
        data["snippet"] = snippet
    if include_lineage:
        data["lineage"] = _memory_lineage(resolved_id, conn, max_depth=max_depth)
    return data


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

        data = _assemble_memory_record(
            conn,
            resolved_id,
            entity_id,
            include_content=True,
            include_lineage=True,
            touch=True,
            max_depth=max_depth,
        )
        if data is None:
            return rejected(
                [
                    envelope_error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{entity_id}'.",
                        "entity_id",
                    )
                ]
            )
        return envelope_ok(data)
    except Exception as exc:
        logger.error("Error getting memory: %s", exc)
        return rejected([envelope_error("MEMORY_READ_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def inspect_memory(entity_id: str, db_connection=None, db_path: str = None, *, max_depth: int = 10) -> dict:
    """Lighter-weight sibling to get_memory: identical field set minus `content` (replaced by a
    `snippet`). Lineage IS included here (matching get_memory's own already-accepted cost for a
    standalone call) -- only get_related_memories's include_inspect=True embedding drops it.
    There is no full-content escape hatch on this tool by design: a caller who decides they need
    the body after seeing the snippet calls get_memory next.
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

        data = _assemble_memory_record(
            conn,
            resolved_id,
            entity_id,
            include_content=False,
            include_lineage=True,
            touch=True,
            max_depth=max_depth,
        )
        if data is None:
            return rejected(
                [
                    envelope_error(
                        "UNKNOWN_ENTITY_ID",
                        f"No memory matches entity_id '{entity_id}'.",
                        "entity_id",
                    )
                ]
            )
        return envelope_ok(data)
    except Exception as exc:
        logger.error("Error inspecting memory: %s", exc)
        return rejected([envelope_error("MEMORY_READ_FAILED", str(exc))])
    finally:
        if should_close:
            close_connection(conn)


def update_memory_metadata(  # noqa: C901
    entity_id: str,
    metadata: dict,
    agent_session_id: str | None = None,
    db_connection=None,
    db_path: str = None,
) -> dict[str, Any]:
    """Shallow-merges `metadata` into an existing memory's metadata dict without touching
    title/content/tags/content_hash/parent_ids -- coexists with (does not deprecate)
    store_memory(entity_id=..., metadata=...), which requires those fields restated
    byte-identical for the same intent. Works uniformly on core and non-core memories;
    core-lifecycle governance fields (outcome/core_review_after/review_rationale) remain owned
    exclusively by review_core_memory, not this tool. No key-deletion sentinel: submitting a
    key with value null overwrites it to null (caller convention for "cleared") -- it is never
    stripped from the stored dict.
    """
    from saltmdb.utils import error_codes

    if not entity_id or not isinstance(entity_id, str) or not entity_id.strip():
        return rejected(
            [envelope_error(error_codes.VALIDATION_ERROR, "entity_id is mandatory.", "entity_id")]
        )
    if not isinstance(metadata, dict):
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "metadata must be an object (dict).",
                    "metadata",
                )
            ]
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
                        error_codes.NOT_FOUND,
                        f"No memory matches entity_id '{entity_id}'.",
                        "entity_id",
                    )
                ]
            )

        result_holder: dict[str, Any] = {}

        def _write(c):
            row = c.execute(
                "SELECT metadata FROM entities WHERE id = ?", (resolved_id,)
            ).fetchone()
            if not row:
                result_holder["result"] = rejected(
                    [
                        envelope_error(
                            error_codes.NOT_FOUND,
                            f"Memory '{resolved_id}' not found.",
                            "entity_id",
                        )
                    ]
                )
                return
            try:
                current = json.loads(row[0]) if row[0] else {}
                if not isinstance(current, dict):
                    current = {}
            except (json.JSONDecodeError, TypeError):
                current = {}
            merged = {**current, **metadata}
            now = datetime.now(UTC).isoformat()
            c.execute(
                "UPDATE entities SET metadata = ?, updated_at = ?, last_touched_session_id = ? "
                "WHERE id = ?",
                (json.dumps(merged), now, agent_session_id, resolved_id),
            )
            changed_keys = sorted(metadata.keys())
            message = (
                f"Memory '{resolved_id}' metadata updated "
                f"({len(changed_keys)} key(s): {', '.join(changed_keys)})."
                if changed_keys
                else f"Memory '{resolved_id}' metadata unchanged (empty patch)."
            )
            result_holder["result"] = envelope_ok({"id": resolved_id, "message": message})

        write_transaction_retrying(conn, _write)
        return result_holder.get(
            "result",
            rejected(
                [
                    envelope_error(
                        error_codes.INTERNAL_ERROR,
                        f"metadata update for '{resolved_id}' did not complete.",
                    )
                ]
            ),
        )
    except Exception as e:
        logger.error("Error updating memory metadata: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
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
        clear_embedding_vectors_for_entity,
    )

    cancel_embedding_jobs_for_entity(conn, resolved_id)
    clear_embedding_vectors_for_entity(conn, resolved_id)
    cancel_retrieval_embedding_jobs_for_entity(conn, resolved_id, clear_vector=True)


def archive_memory(  # noqa: PLR0911
    entity_id: str = None,
    owner_id: str = None,
    db_connection=None,
    db_path: str = None,
    _in_transaction: bool = False,
) -> dict[str, Any]:
    """Explicitly archives (retires) a long-term memory.

    _in_transaction=True skips the internal write_transaction_retrying wrapper -- used by
    bulk_archive_memory, whose caller already holds an open write transaction around the
    whole batch (so the single-item write here must not open/commit its own nested transaction).
    """
    from saltmdb.utils import error_codes

    if not entity_id:
        return rejected(
            [
                envelope_error(
                    error_codes.VALIDATION_ERROR,
                    "entity_id parameter is mandatory.",
                    "entity_id",
                )
            ]
        )
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return rejected(
                [
                    envelope_error(
                        error_codes.NOT_FOUND,
                        f"Could not resolve entity '{entity_id}'",
                        "entity_id",
                    )
                ]
            )

        cursor = conn.execute(
            "SELECT owner_id, scope, status FROM entities WHERE id = ?", (resolved_id,)
        )
        row = cursor.fetchone()
        if not row:
            return rejected(
                [
                    envelope_error(
                        error_codes.NOT_FOUND,
                        f"Memory '{resolved_id}' not found.",
                        "entity_id",
                    )
                ]
            )

        existing_owner, scope, status = row
        if status == "archived":
            return envelope_ok(
                {"id": resolved_id, "message": f"Memory '{resolved_id}' is already archived."},
                warnings=[
                    envelope_warning(error_codes.ALREADY_DONE, "Memory was already archived.")
                ],
            )
        if owner_id and existing_owner and existing_owner != owner_id:
            return rejected(
                [
                    envelope_error(
                        error_codes.CONFLICT,
                        f"Memory '{resolved_id}' owner mismatch.",
                        "owner_id",
                    )
                ]
            )

        if _in_transaction:
            _archive_entity_unchecked(conn, resolved_id)
        else:

            def _write(c):
                _archive_entity_unchecked(conn, resolved_id)

            write_transaction_retrying(conn, _write)

        return envelope_ok(
            {"id": resolved_id, "message": f"Memory '{resolved_id}' was successfully archived."}
        )
    except Exception as e:
        logger.error("Error archiving memory: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
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
