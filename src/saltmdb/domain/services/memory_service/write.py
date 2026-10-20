"""Write path (create/update) for memory_service: entity resolution and persistence."""

import json
from dataclasses import dataclass
from functools import partial
import re
import sqlite3
import uuid
from difflib import SequenceMatcher
from datetime import datetime, UTC
from typing import Any, Literal, cast

from saltmdb.config import get_db_path, MAX_MEMORY_CONTENT_CHARS
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import compute_content_hash
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.envelope import error as envelope_error, rejected, ok as envelope_ok, warning
from saltmdb.domain.services import core_governance_service

from . import tags as tag_ops
from . import validation
from ._shared import logger, RETRIEVAL_TEXT_UNSET

_CORE_TAG = "#core"


def _normalized_tag_key(tag_name: str) -> str:
    return re.sub(r"[-_\s]+", "", (tag_name or "").lower().lstrip("#"))


def _effective_tag_report(
    conn, entity_id: str, submitted_tags: list | None
) -> tuple[list[str], list[dict]]:
    """Return canonical stored tags and advisory near-miss normalizations."""
    effective_tags = [
        row[0]
        for row in conn.execute(
            """
            SELECT t.name
            FROM entity_tags et JOIN tags t ON t.id = et.tag_id
            WHERE et.entity_id = ?
            ORDER BY t.name
            """,
            (entity_id,),
        ).fetchall()
        if row[0] != _CORE_TAG
        or any(_normalized_tag_key(tag) == "core" for tag in (submitted_tags or []))
    ]
    effective_by_key = {_normalized_tag_key(tag): tag for tag in effective_tags}
    near_misses: list[dict] = []

    def _tag_similarity(tag_name: str, submitted_key: str) -> float:
        return SequenceMatcher(None, submitted_key, _normalized_tag_key(tag_name)).ratio()

    for submitted in submitted_tags or []:
        if not isinstance(submitted, str) or not submitted.strip():
            continue
        submitted_key = _normalized_tag_key(submitted)
        if submitted_key in effective_by_key:
            continue
        candidate = max(
            effective_tags,
            key=partial(_tag_similarity, submitted_key=submitted_key),
            default=None,
        )
        if candidate is None:
            continue
        score = SequenceMatcher(None, submitted_key, _normalized_tag_key(candidate)).ratio()
        plural_match = submitted_key.rstrip("s") == _normalized_tag_key(candidate).rstrip("s")
        if plural_match or score >= 0.8:
            near_misses.append(
                {"submitted": submitted, "effective": candidate, "similarity": round(score, 3)}
            )
    return effective_tags, near_misses


def _legacy_tags_changed(conn, *, resolved_entity_id: str, tags: list | None) -> bool:
    if tags is None:
        return False
    requested_tags = {
        tag_ops.normalize_tag_name(tag).lower()
        for tag in tags
        if isinstance(tag, str) and tag.strip()
    }
    existing_tags = {
        row[0].lower()
        for row in conn.execute(
            "SELECT t.name FROM tags t JOIN entity_tags et ON et.tag_id = t.id WHERE et.entity_id = ?",
            (resolved_entity_id,),
        ).fetchall()
        if row[0].lower() != _CORE_TAG
    }
    return requested_tags != existing_tags


def _legacy_frozen_changes(
    conn,
    *,
    resolved_entity_id: str,
    current: dict,
    title: str,
    content: str,
    tags: list | None,
    agent_id: str,
    scope: str,
    memory_type: str | None,
    context_id: str | None,
) -> list[str]:
    changes: list[str] = []
    if title != current["title"]:
        changes.append("title")
    if content != current["full_content"]:
        changes.append("full_content")
    if agent_id != current["agent_id"]:
        changes.append("agent_id")
    # ``store_memory`` historically defaulted scope to ``shared``.  Treat that default as an
    # omitted value when the existing version is private; otherwise an administrative update that
    # never mentioned scope would be misclassified as a disclosure attempt.
    if scope != current["scope"] and not (scope == "shared" and current["scope"] == "private"):
        changes.append("scope")
    if memory_type is not None and memory_type != current["memory_type"]:
        changes.append("memory_type")
    if context_id is not None and context_id != current["context_id"]:
        changes.append("context_id")
    if _legacy_tags_changed(conn, resolved_entity_id=resolved_entity_id, tags=tags):
        changes.append("tags")
    return changes


def _legacy_update_guard(
    conn,
    *,
    resolved_entity_id: str | None,
    title: str,
    content: str,
    tags: list | None,
    agent_id: str,
    scope: str,
    memory_type: str | None,
    context_id: str | None,
) -> tuple[dict | None, dict | None]:
    """Reject frozen-field mutation before the legacy SCD writer can create an ``_h_`` row.

    This applies to explicit IDs and implicit same-title/owner/scope resolutions alike. The second
    return value is a set of canonical frozen values to feed the administrative-only update path.
    It prevents omitted/default fields (notably ``scope=shared``) from being
    written back over an existing version while still allowing lifecycle/core/retrieval fields to
    change in place. Metadata is intentionally excluded from the frozen-field check, same
    treatment as is_core/weight/core_reason.
    """
    if not resolved_entity_id:
        return None, None
    row = conn.execute(
        """
        SELECT title, full_content, agent_id, context_id, scope, memory_type,
               metadata, created_at, content_hash, parent_ids, valid_from
        FROM entities WHERE id = ?
        """,
        (resolved_entity_id,),
    ).fetchone()
    if row is None:
        # Explicit caller-chosen IDs that do not exist are still fresh inserts.
        return None, None
    current = dict(
        zip(
            (
                "title",
                "full_content",
                "agent_id",
                "context_id",
                "scope",
                "memory_type",
                "metadata",
                "created_at",
                "content_hash",
                "parent_ids",
                "valid_from",
            ),
            row,
        )
    )
    changes = _legacy_frozen_changes(
        conn,
        resolved_entity_id=resolved_entity_id,
        current=current,
        title=title,
        content=content,
        tags=tags,
        agent_id=agent_id,
        scope=scope,
        memory_type=memory_type,
        context_id=context_id,
    )
    if changes:
        field = changes[0]
        return (
            rejected(
                [
                    envelope_error(
                        "IMMUTABLE_MEMORY",
                        "Frozen field(s) cannot be changed by store_memory; use revise_memory or supersede_memory to create a new version: "
                        + ", ".join(changes),
                        field,
                    )
                ]
            ),
            None,
        )
    return None, current


def _resolve_existing_entity_id(
    conn, entity_id: str | None, title: str, agent_id: str, scope: str, content_hash: str
) -> tuple[str | None, dict | None]:
    """Resolves what entity id a `store_memory` call will target, before persistence.

    Returns (resolved_entity_id, error_envelope). error_envelope is only ever set for an exact
    content-hash collision -- callers must return it immediately before any write. resolved_entity_id
    is None for a fresh insert (no explicit entity_id, no hash
    collision, no same-title match); non-None means either the caller's own explicit entity_id, or
    a same-title/owner/scope temporal-upsert match.

    Exact content-hash collisions are deterministic hard failures.  Same-title matches remain
    administrative updates, while near-duplicate similarity is advisory and is evaluated only
    after a fresh entity has been persisted.
    """
    explicit_fresh_id = False
    if entity_id:
        existing_row = conn.execute("SELECT id FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if existing_row:
            # Existing explicit targets are administrative updates; identical content is allowed
            # so metadata/core lifecycle fields can be repaired without creating a new version.
            return entity_id, None
        # A caller-supplied ID that does not exist is still a create.  It must not bypass the
        # deterministic exact-hash guard merely because the ID was supplied.
        explicit_fresh_id = True
    try:
        row = conn.execute(
            """
            SELECT id FROM entities
            WHERE content_hash = ? AND (agent_id = ? OR scope = 'shared') AND status != 'archived'
        """,
            (content_hash, agent_id),
        ).fetchone()
        if row:
            existing_id = row[0]
            duplicate_error = envelope_error(
                "REJECT_EXACT_DUPLICATE",
                "An active memory with the exact content hash already exists; use its existing ID "
                f"{existing_id} or revise/supersede it instead of storing a duplicate.",
                "content",
            )
            duplicate_error["detail"] = {"existing_entity_id": existing_id}
            return None, rejected([duplicate_error])
    except sqlite3.Error as exc:
        logger.debug("Exact content-hash lookup unavailable; continuing with title lookup: %s", exc)
    if explicit_fresh_id:
        return entity_id, None
    try:
        row = conn.execute(
            """
            SELECT id FROM entities
            WHERE title = ? AND agent_id = ? AND scope = ? AND status != 'archived'
        """,
            (title, agent_id, scope),
        ).fetchone()
        if row:
            return row[0], None
    except sqlite3.Error as exc:
        logger.debug("Title lookup unavailable; treating memory as a fresh insert: %s", exc)
    return None, None


def _resolve_raw_core_state(
    conn, proposed: dict, entity_id: str, was_existing_entity: bool, redacted_content: str
) -> dict:
    """Resolve authoritative in-transaction core state for a raw write."""
    is_core = proposed.get("is_core")
    title = proposed["title"]
    scope = proposed["scope"]
    memory_type = proposed.get("memory_type")
    # Core-memory governance (resolved gap #2): AUTHORITATIVE, in-transaction re-resolution --
    # runs against `conn`, the caller's own open write transaction, never the pre-transaction
    # snapshot store_memory computed for its own advisory early-return. Raises
    # core_governance_service.CoreGovernanceRejected on a concurrent-state TOCTOU failure
    # (capacity or lifecycle), aborting this transaction with zero side effects.
    try:
        is_core_requested = core_governance_service.parse_is_core(is_core)
        core_state = core_governance_service.resolve_store_core_state(
            conn,
            entity_id=entity_id if was_existing_entity else None,
            is_core_requested=is_core_requested,
            content=redacted_content,
            scope=scope,
            core_reason=proposed.get("core_reason"),
            core_exit_condition=proposed.get("core_exit_condition"),
            core_review_after=proposed.get("core_review_after"),
            detail_memory_ids=proposed.get("detail_memory_ids"),
        )
        # store_memory never itself changes `status` (preserved verbatim on an update -- only
        # archive_memory/commit_consolidation/review_core_memory do); a fresh insert always
        # lands as 'raw'. An existing ARCHIVED target therefore stays archived after this write
        # and never becomes bootstrap-visible -- it must not count against capacity or the
        # overdue boundary (plan rule 47: archived entities never count), even though its
        # is_core column is still being set/validated above for consistency.
        target_will_be_active = True
        if was_existing_entity:
            status_row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            # A None row means the caller supplied an explicit entity_id that doesn't exist yet
            # -- _resolve_existing_entity_id always echoes an explicit entity_id back verbatim
            # regardless of whether it exists (see its own docstring), so was_existing_entity
            # alone can't distinguish "real update" from "insert under a caller-chosen id". Both
            # a status_row of None and any non-archived status land this write as active.
            target_will_be_active = status_row is None or status_row[0] != "archived"

        effective_memory_type = core_governance_service.resolve_effective_memory_type(
            conn,
            entity_id=entity_id if was_existing_entity else None,
            requested_memory_type=memory_type,
        )
        prospective_entry = {
            "id": entity_id,
            "title": title,
            "memory_type": effective_memory_type,
            "core_reason": core_state["core_reason"],
            "core_exit_condition": core_state["core_exit_condition"],
            "core_review_after": core_state["core_review_after"],
            "full_content": redacted_content,
            "agent_id": proposed["agent_id"],
        }

        if target_will_be_active:
            core_governance_service.enforce_overdue_boundary(
                conn,
                entity_id=entity_id if was_existing_entity else None,
                effective_is_core=core_state["is_core"],
                is_new_core=core_state["is_new_core"],
                review_after_changed=core_state["review_after_changed"],
                prospective_entry=prospective_entry,
            )
    except ValueError as e:
        raise core_governance_service.CoreGovernanceRejected(str(e)) from e

    if core_state["is_core"] and target_will_be_active:
        rejection = core_governance_service.check_capacity_admission(
            conn,
            exclude_ids=[entity_id] if was_existing_entity else [],
            new_entry=prospective_entry,
        )
        if rejection is not None:
            raise core_governance_service.CoreGovernanceRejected(rejection)
    return core_state


def _write_raw_scd_history(conn, entity_id: str, existing: tuple[Any, ...], now: str) -> None:
    created_at = existing[0]
    valid_from = existing[2]
    hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"
    conn.execute(
        """
                 INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, agent_session_id, last_touched_session_id, embedding_status, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_review_after, core_last_reviewed_at, core_last_reviewed_by, core_review_rationale, core_detail_memory_ids)
                 SELECT ?, created_at, updated_at, last_accessed_at, agent_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, context_id, agent_session_id, last_touched_session_id, 'archived', content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_review_after, core_last_reviewed_at, core_last_reviewed_by, core_review_rationale, core_detail_memory_ids
                 FROM entities WHERE id = ?
             """,
        (hist_id, valid_from if valid_from else created_at, now, entity_id),
    )
    conn.execute(
        """
                 INSERT INTO entity_tags (entity_id, tag_id)
                 SELECT ?, tag_id FROM entity_tags WHERE entity_id = ?
             """,
        (hist_id, entity_id),
    )


def _raw_existing_values(
    conn, entity_id: str, title: str, redacted_content: str, content_hash: str
) -> tuple[tuple[Any, ...] | None, bool, str | None, str | None, str | None]:
    existing = conn.execute(
        "SELECT created_at, agent_id, valid_from, title, full_content, content_hash, metadata "
        "FROM entities WHERE id = ?",
        (entity_id,),
    ).fetchone()
    if existing:
        base_source_changed = (
            existing[3] != title or existing[4] != redacted_content or existing[5] != content_hash
        )
        prior_retrieval = conn.execute(
            "SELECT retrieval_text,retrieval_text_hash FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        existing_retrieval_text = prior_retrieval[0] if prior_retrieval else None
        existing_retrieval_hash = prior_retrieval[1] if prior_retrieval else None
    else:
        base_source_changed = True
        existing_retrieval_text = None
        existing_retrieval_hash = None
    return (
        existing,
        base_source_changed,
        existing_retrieval_text,
        existing_retrieval_hash,
        (existing[6] if existing else None),
    )


def _raw_metadata(
    existing: tuple[Any, ...] | None, prior_metadata: str | None, metadata: dict | None
) -> str | None:
    if not existing:
        return json.dumps(metadata) if metadata is not None else None
    current_metadata_dict = {}
    if prior_metadata:
        try:
            parsed_meta = json.loads(prior_metadata)
            if isinstance(parsed_meta, dict):
                current_metadata_dict = parsed_meta
        except (json.JSONDecodeError, TypeError):
            current_metadata_dict = {}
    if metadata is None:
        return prior_metadata
    submitted_metadata_dict = metadata if isinstance(metadata, dict) else {}
    return json.dumps({**current_metadata_dict, **submitted_metadata_dict})


def _raw_retrieval_values(
    existing: tuple[Any, ...] | None,
    retrieval_text_provided: bool,
    requested_retrieval_text: str | None,
    existing_retrieval_text: str | None,
    existing_retrieval_hash: str | None,
) -> tuple[str | None, str | None]:
    if existing and not retrieval_text_provided:
        return existing_retrieval_text, existing_retrieval_hash
    final_retrieval_text = requested_retrieval_text if retrieval_text_provided else None
    final_retrieval_hash = (
        validation._retrieval_text_hash(final_retrieval_text)
        if final_retrieval_text is not None
        else None
    )
    return final_retrieval_text, final_retrieval_hash


@dataclass(frozen=True, slots=True)
class _RawEntityRow:
    # Contains mutable state; must not be hashed or cached.
    entity_id: str
    now: str
    agent_id: str
    scope: str
    is_core_val: int
    weight: int
    title: str
    redacted_content: str
    metadata_str: str | None
    context_id: str | None
    agent_session_id: str | None
    content_hash: str
    quality_score: int
    quality_status: str
    quality_flags_str: str
    memory_type: str | None
    final_retrieval_text: str | None
    final_retrieval_hash: str | None
    core_state: dict
    core_detail_ids_str: str | None


def _insert_raw_entity(conn, row: _RawEntityRow) -> None:
    conn.execute(
        """
        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, agent_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, agent_session_id, last_touched_session_id, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_review_after, core_detail_memory_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'), ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at = excluded.updated_at,
            last_touched_session_id = excluded.last_touched_session_id,
            last_accessed_at = excluded.last_accessed_at,
            agent_id = COALESCE(excluded.agent_id, entities.agent_id),
            scope = excluded.scope,
            is_core = excluded.is_core,
            weight = excluded.weight,
            status = entities.status,
            title = excluded.title,
            full_content = excluded.full_content,
            valid_from = entities.valid_from,
            valid_to = CASE WHEN entities.status IN ('consolidated', 'archived')
                             THEN entities.valid_to ELSE NULL END,
            metadata = excluded.metadata,
            context_id = COALESCE(excluded.context_id, entities.context_id),
            content_hash = excluded.content_hash,
            quality_score = excluded.quality_score,
            quality_status = excluded.quality_status,
            quality_flags = excluded.quality_flags,
            memory_type = COALESCE(?, entities.memory_type),
            retrieval_text = excluded.retrieval_text,
            retrieval_text_hash = excluded.retrieval_text_hash,
            core_reason = excluded.core_reason,
            core_exit_condition = excluded.core_exit_condition,
            core_review_after = excluded.core_review_after,
            core_detail_memory_ids = excluded.core_detail_memory_ids
    """,
        (
            row.entity_id,
            row.now,
            row.now,
            row.now,
            row.agent_id,
            row.scope,
            row.is_core_val,
            row.weight,
            json.dumps([]),
            row.title,
            row.redacted_content,
            row.now,
            row.metadata_str,
            row.context_id,
            row.agent_session_id,
            row.agent_session_id,
            row.content_hash,
            row.quality_score,
            row.quality_status,
            row.quality_flags_str,
            row.memory_type,
            row.final_retrieval_text,
            row.final_retrieval_hash,
            row.core_state["core_reason"],
            row.core_state["core_exit_condition"],
            row.core_state["core_review_after"],
            row.core_detail_ids_str,
            row.memory_type,
        ),
    )


def _write_raw_tags(conn, entity_id: str, tags: list | None, agent_id: str) -> None:
    if tags is None:
        return

    tag_lookup: dict[str, str] = {}  # norm -> resolved tag_id, cached per-call to avoid
    # re-resolving the same tag string twice within one store_memory
    for tag_name in tags:
        tag_name = tag_name.strip()
        if not tag_name:
            continue

        norm_input = tag_name.lower().lstrip("#")
        norm_input = re.sub(r"[-_\s]+", "", norm_input)

        # Use cached result if we already resolved an equivalent tag string
        tag_id: str | None
        if norm_input in tag_lookup:
            tag_id = tag_lookup[norm_input]
        else:
            tag_id = tag_ops.resolve_or_create_tag(conn, tag_name, agent_id=agent_id)
            if tag_id:
                tag_lookup[norm_input] = tag_id

        if not tag_id:
            continue

        conn.execute(
            "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
            (entity_id, tag_id),
        )


def _delete_raw_tags(conn, entity_id: str, tags: list | None) -> None:
    if tags is not None:
        conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))


def _sync_raw_core_tag(conn, entity_id: str, agent_id: str) -> None:
    # Stage 4.5: is_core -> #core tag sync. is_core is the single writable source of
    # truth; #core is a derived label the server maintains so the two can never drift
    # apart again. Runs on every write (even calls that touch neither is_core nor tags),
    # which also self-heals any pre-existing drift the next time an entity is touched.
    resolved_row = conn.execute(
        "SELECT is_core FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    resolved_is_core = bool(resolved_row[0]) if resolved_row else False
    core_tag_id = tag_ops.resolve_or_create_tag(conn, _CORE_TAG, agent_id=agent_id)
    if core_tag_id:
        if resolved_is_core:
            conn.execute(
                "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                (entity_id, core_tag_id),
            )
        else:
            conn.execute(
                "DELETE FROM entity_tags WHERE entity_id = ? AND tag_id = ?",
                (entity_id, core_tag_id),
            )


def _enqueue_raw_embedding_jobs(
    conn,
    *,
    entity_id: str,
    title: str,
    redacted_content: str,
    content_hash: str,
    base_source_changed: bool,
    retrieval_text_provided: bool,
    existing: tuple[Any, ...] | None,
    final_retrieval_text: str | None,
    final_retrieval_hash: str | None,
) -> None:
    # This is intentionally part of the same transaction as the entity
    # version/tag update: a committed active source always has durable work,
    # even if the daemon dies before the scheduler can dispatch inference.
    from saltmdb.domain.services.embedding_service import (
        enqueue_embedding_jobs_for_entity,
        enqueue_retrieval_embedding_job_for_entity,
    )

    if base_source_changed:
        enqueue_embedding_jobs_for_entity(conn, entity_id, title, redacted_content, content_hash)
    if retrieval_text_provided or not existing:
        enqueue_retrieval_embedding_job_for_entity(
            conn,
            entity_id,
            final_retrieval_text,
            final_retrieval_hash,
            force=True,
        )


def _store_raw_entity(conn, proposed: dict) -> tuple[str, bool]:
    """Persists `proposed` as a plain raw entity (a temporal upsert if `resolved_entity_id` names
    an already-existing row, otherwise a fresh insert) -- the same insert/tag/`#core`-sync logic
    Must run inside the caller's own write transaction. Returns (entity_id, was_existing) --
    `was_existing` gates the "[Tip: ...]" suffix.
    """
    was_existing_entity = bool(proposed.get("resolved_entity_id"))
    entity_id = proposed.get("resolved_entity_id") or str(uuid.uuid4())
    title = proposed["title"]
    redacted_content = proposed["content"]
    agent_id = proposed["agent_id"]
    scope = proposed["scope"]
    weight = proposed.get("weight") or 1
    memory_type = proposed.get("memory_type")
    metadata = proposed.get("metadata")
    context_id = proposed.get("context_id")
    content_hash = proposed["content_hash"]
    quality_score = proposed["quality_score"]
    quality_status = proposed["quality_status"]
    quality_flags_str = proposed["quality_flags_str"]
    tags = proposed.get("tags")
    retrieval_text_provided = bool(proposed.get("retrieval_text_provided", False))
    requested_retrieval_text = proposed.get("retrieval_text")
    agent_session_id = proposed["agent_session_id"]
    now = datetime.now(UTC).isoformat()

    core_state = _resolve_raw_core_state(
        conn, proposed, entity_id, was_existing_entity, redacted_content
    )
    is_core_val = 1 if core_state["is_core"] else 0
    (
        existing,
        base_source_changed,
        existing_retrieval_text,
        existing_retrieval_hash,
        prior_metadata,
    ) = _raw_existing_values(conn, entity_id, title, redacted_content, content_hash)
    if existing and not proposed.get("_skip_scd_history", False):
        _write_raw_scd_history(conn, entity_id, existing, now)
    _delete_raw_tags(conn, entity_id, tags)
    metadata_str = _raw_metadata(existing, prior_metadata, metadata)
    final_retrieval_text, final_retrieval_hash = _raw_retrieval_values(
        existing,
        retrieval_text_provided,
        requested_retrieval_text,
        existing_retrieval_text,
        existing_retrieval_hash,
    )
    core_detail_ids_str = (
        json.dumps(core_state["core_detail_memory_ids"])
        if core_state["core_detail_memory_ids"]
        else None
    )
    _insert_raw_entity(
        conn,
        _RawEntityRow(
            entity_id=entity_id,
            now=now,
            agent_id=agent_id,
            scope=scope,
            is_core_val=is_core_val,
            weight=weight,
            title=title,
            redacted_content=redacted_content,
            metadata_str=metadata_str,
            context_id=context_id,
            agent_session_id=agent_session_id,
            content_hash=content_hash,
            quality_score=quality_score,
            quality_status=quality_status,
            quality_flags_str=quality_flags_str,
            memory_type=memory_type,
            final_retrieval_text=final_retrieval_text,
            final_retrieval_hash=final_retrieval_hash,
            core_state=core_state,
            core_detail_ids_str=core_detail_ids_str,
        ),
    )
    core_governance_service.reconcile_detail_relations(
        conn,
        core_id=entity_id,
        agent_id=agent_id,
        new_detail_ids=core_state["core_detail_memory_ids"],
        previous_detail_ids=core_state["previous_detail_memory_ids"],
    )
    _write_raw_tags(conn, entity_id, tags, agent_id)
    _sync_raw_core_tag(conn, entity_id, agent_id)
    _enqueue_raw_embedding_jobs(
        conn,
        entity_id=entity_id,
        title=title,
        redacted_content=redacted_content,
        content_hash=content_hash,
        base_source_changed=base_source_changed,
        retrieval_text_provided=retrieval_text_provided,
        existing=existing,
        final_retrieval_text=final_retrieval_text,
        final_retrieval_hash=final_retrieval_hash,
    )
    return entity_id, bool(existing)


def _near_duplicate_warning(duplicate_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return warning(
        "NEAR_DUPLICATE",
        "Memory stored; review duplicate_candidates and use supersede_memory for one "
        "replacement, consolidate_memories to merge several, or manage_relation to "
        "link them if related but not redundant.",
        {
            "duplicate_candidates": duplicate_candidates,
            "guidance": {
                "single": "supersede_memory",
                "several": "consolidate_memories",
                "related_not_redundant": "manage_relation",
            },
        },
    )


def _validate_store_fields(
    agent_id: str | None, content: str | None, scope: str, memory_type: str | None
) -> str | None:
    if not agent_id:
        return "Error: agent_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."
    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."
    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        return f"Error: content exceeds {MAX_MEMORY_CONTENT_CHARS} characters (got {len(content)})."
    if scope not in ("private", "shared"):
        return "Error: scope must be either 'private' or 'shared'"
    if memory_type is not None and memory_type not in (
        "fact",
        "event",
        "procedure",
        "decision",
        "preference",
    ):
        return "Error: memory_type must be one of 'fact', 'event', 'procedure', 'decision', 'preference'"
    return None


def _weight_from_dimensions(
    weight: int,
    relevance: int | None,
    impact: int | None,
    novelty: int | None,
    actionability: int | None,
) -> int:
    if relevance is None and impact is None and novelty is None and actionability is None:
        return weight
    r = relevance if relevance is not None else 3
    im = impact if impact is not None else 3
    n = novelty if novelty is not None else 3
    a = actionability if actionability is not None else 3
    return max(1, min(5, (r + im + n + a) // 4))


def _validate_store_basics(
    agent_id: str | None,
    content: str | None,
    scope: str,
    memory_type: str | None,
    weight: int,
    relevance: int | None,
    impact: int | None,
    novelty: int | None,
    actionability: int | None,
) -> tuple[str | None, int]:
    error = _validate_store_fields(agent_id, content, scope, memory_type)
    if error:
        return error, weight
    return None, _weight_from_dimensions(weight, relevance, impact, novelty, actionability)


def _normalize_retrieval_input(
    retrieval_text: str | None | object,
) -> tuple[str | None, str | None, bool]:
    provided = retrieval_text is not RETRIEVAL_TEXT_UNSET
    if not provided:
        return None, None, False
    if retrieval_text is None:
        return (
            "Error: retrieval_text JSON null is ambiguous; omit the field to preserve or use an empty string to clear.",
            None,
            True,
        )
    if not isinstance(retrieval_text, str):
        return "Error: retrieval_text must be a string", None, True
    try:
        normalized = validation.normalize_retrieval_text(retrieval_text)
    except ValueError as exc:
        return f"Error: {exc}", None, True
    return None, normalized, True


def _normalize_store_inputs(
    content: str,
    title: str | None,
    retrieval_text: str | None | object,
    metadata: dict | None,
    context_id: str | None,
    tags: list | None,
) -> tuple[str | None, dict]:
    redacted_content = redact_secrets(content)
    retrieval_error, normalized_retrieval_text, retrieval_text_provided = (
        _normalize_retrieval_input(retrieval_text)
    )
    if retrieval_error:
        return retrieval_error, {}
    if title:
        title = redact_secrets(title)
    if not title or not title.strip():
        return "Error: title is mandatory and cannot be empty.", {}
    try:
        validation.validate_memory_input(title, redacted_content, tags)
    except ValueError as e:
        return str(e), {}
    # Stage 1: Auto-Formatting (Idempotent cleanup: f(f(x)) = f(x))
    from saltmdb.utils.nlp import auto_format_markdown

    redacted_content = auto_format_markdown(redacted_content)
    if not context_id and metadata and isinstance(metadata, dict):
        context_id = metadata.get("project") or metadata.get("project_id")
    return None, {
        "content": redacted_content,
        "title": title,
        "retrieval_text": normalized_retrieval_text,
        "retrieval_text_provided": retrieval_text_provided,
        "context_id": context_id,
    }


def _evaluate_store_quality(redacted_content: str) -> tuple[dict | None, dict, str, str, str]:
    # Stage 2 & 3: Extract Prose & Pre-Embedding Quality Gate Evaluation
    quality_res = evaluate_memory_quality(redacted_content)
    if quality_res["status"] == "REJECT":
        return (
            rejected(
                [
                    envelope_error(
                        code,
                        f"Memory quality check rejected by {code}.",
                        "content",
                    )
                    for code in (quality_res.get("hard_errors") or ["MEMORY_QUALITY_REJECTED"])
                ],
                warnings=[
                    warning(
                        "QUALITY_WARNING",
                        f"Advisory quality finding: {code}.",
                        {"rule": code},
                    )
                    for code in quality_res.get("warnings", [])
                ],
            ),
            quality_res,
            "",
            "",
            "",
        )
    return (
        None,
        quality_res,
        compute_content_hash(redacted_content),
        quality_res["status"],
        json.dumps(quality_res["quality_flags"]),
    )


def _preview_store_core(
    conn,
    *,
    resolved_entity_id: str | None,
    is_core: bool | None,
    redacted_content: str,
    scope: str,
    core_reason: str | None,
    core_exit_condition: str | None,
    core_review_after: str | None,
    detail_memory_ids: list | None,
    title: str,
    agent_id: str,
    memory_type: str | None,
) -> tuple[str | dict | None, str | None]:
    try:
        core_state_preview = core_governance_service.resolve_store_core_state(
            conn,
            entity_id=resolved_entity_id,
            is_core_requested=core_governance_service.parse_is_core(is_core),
            content=redacted_content,
            scope=scope,
            core_reason=core_reason,
            core_exit_condition=core_exit_condition,
            core_review_after=core_review_after,
            detail_memory_ids=detail_memory_ids,
        )
        preview_target_will_be_active = True
        if resolved_entity_id:
            status_row = conn.execute(
                "SELECT status FROM entities WHERE id = ?", (resolved_entity_id,)
            ).fetchone()
            preview_target_will_be_active = status_row is None or status_row[0] != "archived"
        effective_memory_type_preview = core_governance_service.resolve_effective_memory_type(
            conn, entity_id=resolved_entity_id, requested_memory_type=memory_type
        )
        prospective_entry_preview = {
            "id": resolved_entity_id or str(uuid.uuid4()),
            "title": title,
            "memory_type": effective_memory_type_preview,
            "core_reason": core_state_preview["core_reason"],
            "core_exit_condition": core_state_preview["core_exit_condition"],
            "core_review_after": core_state_preview["core_review_after"],
            "full_content": redacted_content,
            "agent_id": agent_id,
        }
        if preview_target_will_be_active:
            core_governance_service.enforce_overdue_boundary(
                conn,
                entity_id=resolved_entity_id,
                effective_is_core=core_state_preview["is_core"],
                is_new_core=core_state_preview["is_new_core"],
                review_after_changed=core_state_preview["review_after_changed"],
                prospective_entry=prospective_entry_preview,
            )
    except ValueError as e:
        return f"Error: {e}", None
    if core_state_preview["is_core"] and preview_target_will_be_active:
        rejection = core_governance_service.check_capacity_admission(
            conn,
            exclude_ids=[resolved_entity_id] if resolved_entity_id else [],
            new_entry=prospective_entry_preview,
        )
        if rejection is not None:
            return rejection, None
    return None, effective_memory_type_preview


def _persist_store_entity(conn, proposed: dict) -> tuple[str | dict | None, str | None, bool]:
    def _write(c):
        return _store_raw_entity(c, proposed)

    try:
        entity_id_out, was_existing = write_transaction_retrying(conn, _write)
    except core_governance_service.CoreGovernanceRejected as e:
        return e.payload if isinstance(e.payload, dict) else f"Error: {e.payload}", None, False
    return None, entity_id_out, was_existing


def _probe_store_duplicates(
    *,
    conn,
    was_existing: bool,
    probe_sink: list[dict[str, Any]] | None,
    title: str,
    redacted_content: str,
    agent_id: str,
    tags: list | None,
    context_id: str | None,
    entity_id_out: str,
    db_path: str | None,
) -> list[dict[str, Any]]:
    if was_existing:
        return []
    if probe_sink is not None:
        probe_sink.append(
            {
                "title": title,
                "content": redacted_content,
                "agent_id": agent_id,
                "tags": tags,
                "context_id": context_id,
                "exclude_ids": [entity_id_out],
                "db_path": db_path or get_db_path(),
            }
        )
        return []
    from .duplicates import check_duplicate_memories

    duplicate_result = check_duplicate_memories(
        title=title,
        content=redacted_content,
        agent_id=agent_id,
        context_id=cast(str, context_id),
        exclude_ids=[entity_id_out],
        db_connection=conn,
        db_path=cast(str, db_path),
    )
    return duplicate_result.get("potential_duplicates") or []


def _store_response_warnings(
    quality_res: dict,
    tag_near_misses: list[dict],
    duplicate_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    response_warnings = [
        warning(
            "QUALITY_WARNING",
            f"Memory stored with advisory quality finding: {quality_code}.",
            {"rule": quality_code},
        )
        for quality_code in quality_res.get("warnings", [])
    ]
    response_warnings.extend(
        warning(
            "TAG_NEAR_MISS",
            f"Submitted tag {item['submitted']} resolved near {item['effective']}.",
            item,
        )
        for item in tag_near_misses
    )
    if duplicate_candidates:
        response_warnings.append(_near_duplicate_warning(duplicate_candidates))
    return response_warnings


def _prepare_store_content(request: dict) -> tuple[str | dict | None, dict]:
    inputs_error, normalized = _normalize_store_inputs(
        request["content"],
        request["title"],
        request["retrieval_text"],
        request["metadata"],
        request["context_id"],
        request["tags"],
    )
    if inputs_error:
        return inputs_error, {}
    redacted_content = normalized["content"]
    title = normalized["title"]
    quality_error, quality_res, content_hash, quality_status, quality_flags_str = (
        _evaluate_store_quality(redacted_content)
    )
    if quality_error is not None:
        return quality_error, {}
    return None, {
        "redacted_content": redacted_content,
        "title": title,
        "normalized_retrieval_text": normalized["retrieval_text"],
        "retrieval_text_provided": normalized["retrieval_text_provided"],
        "context_id": normalized["context_id"],
        "quality_res": quality_res,
        "content_hash": content_hash,
        "quality_status": quality_status,
        "quality_flags_str": quality_flags_str,
    }


def _apply_frozen_store_values(
    metadata: dict | None, frozen_current: dict
) -> tuple[str, str, str | None, str, list | None, dict | None]:
    # The current row is the immutable source of truth for an administrative-only
    # update.  Preserve omitted/default frozen inputs before entering _store_raw_entity.
    title = frozen_current["title"]
    redacted_content = frozen_current["full_content"]
    context_id = frozen_current["context_id"]
    content_hash = frozen_current["content_hash"]
    write_tags = None
    if metadata is None and frozen_current["metadata"]:
        try:
            metadata = json.loads(frozen_current["metadata"])
        except json.JSONDecodeError:
            metadata = None
    return title, redacted_content, context_id, content_hash, write_tags, metadata


def _prepare_store_guarded(
    conn, request: dict, content_state: dict
) -> tuple[str | dict | None, dict]:
    redacted_content = content_state["redacted_content"]
    title = content_state["title"]
    normalized_retrieval_text = content_state["normalized_retrieval_text"]
    retrieval_text_provided = content_state["retrieval_text_provided"]
    context_id = content_state["context_id"]
    content_hash = content_state["content_hash"]
    quality_res = content_state["quality_res"]
    quality_status = content_state["quality_status"]
    quality_flags_str = content_state["quality_flags_str"]
    resolved_entity_id, hash_collision_error = _resolve_existing_entity_id(
        conn,
        request["entity_id"],
        title,
        request["agent_id"],
        request["scope"],
        content_hash,
    )
    if hash_collision_error:
        return hash_collision_error, {}
    write_tags = request["tags"]
    legacy_error, frozen_current = _legacy_update_guard(
        conn,
        resolved_entity_id=resolved_entity_id,
        title=title,
        content=redacted_content,
        tags=write_tags,
        agent_id=request["agent_id"],
        scope=request["scope"],
        memory_type=request["memory_type"],
        context_id=context_id,
    )
    if legacy_error is not None:
        return legacy_error, {}
    request_metadata = request["metadata"]
    if frozen_current is not None:
        (
            title,
            redacted_content,
            context_id,
            content_hash,
            write_tags,
            request_metadata,
        ) = _apply_frozen_store_values(request_metadata, frozen_current)
    proposed = {
        "content": redacted_content,
        "title": title,
        "tags": write_tags,
        "agent_id": frozen_current["agent_id"] if frozen_current else request["agent_id"],
        "scope": frozen_current["scope"] if frozen_current else request["scope"],
        "memory_type": frozen_current["memory_type"] if frozen_current else request["memory_type"],
        "context_id": context_id,
        "is_core": request["is_core"],
        "weight": request["weight"],
        "metadata": request_metadata,
        "resolved_entity_id": resolved_entity_id,
        "content_hash": content_hash,
        "quality_score": quality_res["quality_score"],
        "quality_status": quality_status,
        "quality_flags_str": quality_flags_str,
        "retrieval_text": normalized_retrieval_text,
        "retrieval_text_provided": retrieval_text_provided,
        "core_reason": request["core_reason"],
        "core_exit_condition": request["core_exit_condition"],
        "core_review_after": request["core_review_after"],
        "detail_memory_ids": request["detail_memory_ids"],
        "agent_session_id": request["agent_session_id"],
        "_skip_scd_history": frozen_current is not None,
    }
    # Capacity/lifecycle validation remains side-effect free and authoritative persistence
    # still happens inside the write transaction. Similarity is deliberately advisory only.
    preview_error, effective_memory_type_preview = _preview_store_core(
        conn,
        resolved_entity_id=resolved_entity_id,
        is_core=request["is_core"],
        redacted_content=redacted_content,
        scope=cast(str, proposed["scope"]),
        core_reason=request["core_reason"],
        core_exit_condition=request["core_exit_condition"],
        core_review_after=request["core_review_after"],
        detail_memory_ids=request["detail_memory_ids"],
        title=title,
        agent_id=cast(str, proposed["agent_id"]),
        memory_type=cast(str | None, proposed["memory_type"]),
    )
    if preview_error is not None:
        return preview_error, {}
    return None, {
        "proposed": proposed,
        "title": title,
        "redacted_content": redacted_content,
        "context_id": context_id,
        "write_tags": write_tags,
        "quality_res": quality_res,
        "effective_memory_type_preview": effective_memory_type_preview,
    }


def _prepare_store_operation(conn, request: dict) -> tuple[str | dict | None, dict]:
    content_error, content_state = _prepare_store_content(request)
    if content_error is not None:
        return content_error, {}
    return _prepare_store_guarded(conn, request, content_state)


def store_memory(
    content: str = None,
    tags: list = None,
    agent_id: str = None,
    scope: Literal["private", "shared"] = "shared",
    weight: int = 1,
    is_core: bool = None,
    memory_type: Literal["fact", "event", "procedure", "decision", "preference"] = None,
    title: str = None,
    entity_id: str = None,
    relevance: int = None,
    impact: int = None,
    novelty: int = None,
    actionability: int = None,
    metadata: dict = None,
    context_id: str = None,
    agent_session_id: str = None,
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    *,
    retrieval_text: str | None | object = RETRIEVAL_TEXT_UNSET,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
    probe_sink: list[dict[str, Any]] | None = None,
) -> str | dict:
    """Stores a consolidated Markdown fact chunk as a long-term memory.

    Exact content-hash duplicates are rejected before any write and identify the existing entity.
    Fresh near duplicates are persisted normally; the success response includes inline candidate
    IDs/titles/similarity scores and directs callers to `supersede_memory` or
    `consolidate_memories` when they determine the knowledge should be replaced or merged.

    Core-memory governance (see core_governance_service.py): `is_core=True` requires `scope=
    "shared"`, `core_reason`/`core_exit_condition` (each 20-500 characters), and admits a hard
    global cap on active-core count/per-memory length/rendered bootstrap size -- a capacity
    failure returns a `status: "rejected"` envelope (`errors[0].code ==
    "CORE_CAPACITY_EXCEEDED"`) with zero side effects (no memory, relation, or
    other state created), never a partial write. Omitting `core_reason`/`core_exit_condition`/
    `core_review_after`/`detail_memory_ids` on an UPDATE to an already-core memory preserves the
    existing values; supplying any of them while the effective memory is NOT core is rejected,
    never silently ignored. `detail_memory_ids=None` preserves the current declaration, `[]`
    clears it, a replacement list atomically reconciles the declared `elaborates_on` relations.
    """
    basics_error, weight = _validate_store_basics(
        agent_id,
        content,
        scope,
        memory_type,
        weight,
        relevance,
        impact,
        novelty,
        actionability,
    )
    if basics_error:
        return basics_error

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        request = {
            "content": content,
            "title": title,
            "retrieval_text": retrieval_text,
            "metadata": metadata,
            "context_id": context_id,
            "tags": tags,
            "agent_id": agent_id,
            "scope": scope,
            "memory_type": memory_type,
            "entity_id": entity_id,
            "is_core": is_core,
            "weight": weight,
            "core_reason": core_reason,
            "core_exit_condition": core_exit_condition,
            "core_review_after": core_review_after,
            "detail_memory_ids": detail_memory_ids,
            "agent_session_id": agent_session_id,
        }
        prepare_error, prepared = _prepare_store_operation(conn, request)
        if prepare_error is not None:
            return prepare_error
        proposed = prepared["proposed"]
        title = prepared["title"]
        redacted_content = prepared["redacted_content"]
        context_id = prepared["context_id"]
        write_tags = prepared["write_tags"]
        quality_res = prepared["quality_res"]
        effective_memory_type_preview = prepared["effective_memory_type_preview"]
        agent_id = proposed["agent_id"]
        scope = proposed["scope"]
        persist_error, entity_id_out, was_existing = _persist_store_entity(conn, proposed)
        if persist_error is not None:
            return persist_error
        stored_entity_id = cast(str, entity_id_out)
        res_msg = f"Knowledge stored successfully with ID: {stored_entity_id}"
        if not was_existing and write_tags:
            res_msg += " [Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]"

        # Near duplicates are warnings, never a write gate.  Keep this probe after persistence so
        # callers receive the candidate IDs alongside the newly-created memory even when they do
        # not follow the guidance.
        duplicate_candidates = _probe_store_duplicates(
            conn=conn,
            was_existing=was_existing,
            probe_sink=probe_sink,
            title=title,
            redacted_content=redacted_content,
            agent_id=agent_id,
            tags=write_tags,
            context_id=context_id,
            entity_id_out=stored_entity_id,
            db_path=db_path,
        )
        effective_tags, tag_near_misses = _effective_tag_report(conn, stored_entity_id, write_tags)
        response_warnings = _store_response_warnings(
            quality_res, tag_near_misses, duplicate_candidates
        )

        from saltmdb.domain.services.librarian_service import trigger_librarian

        trigger_librarian(db_path=db_path, coordinator=coordinator)

        return envelope_ok(
            {
                "id": stored_entity_id,
                "message": res_msg,
                "submitted_tags": list(write_tags or []),
                "effective_tags": effective_tags,
                "duplicate_candidates": duplicate_candidates,
            },
            warnings=response_warnings,
            effective={
                "agent_id": agent_id,
                "context_id": context_id,
                "scope": scope,
                "memory_type": effective_memory_type_preview,
                "agent_session_id": agent_session_id,
            },
        )
    except Exception as e:
        from saltmdb.utils import error_codes

        logger.error("Error storing knowledge: %s", e)
        return rejected([envelope_error(error_codes.INTERNAL_ERROR, str(e))])
    finally:
        if should_close:
            close_connection(conn)


def apply_duplicate_probe(envelope: dict[str, Any], request: dict[str, Any]) -> None:
    from .duplicates import check_duplicate_memories

    duplicate_result = check_duplicate_memories(
        title=request["title"],
        content=request["content"],
        agent_id=request["agent_id"],
        context_id=request["context_id"],
        exclude_ids=request["exclude_ids"],
        db_connection=None,
        db_path=request["db_path"],
    )
    duplicate_candidates = duplicate_result.get("potential_duplicates") or []
    if "error" in duplicate_result or not duplicate_candidates:
        return
    envelope["data"]["duplicate_candidates"] = duplicate_candidates
    envelope["warnings"].append(_near_duplicate_warning(duplicate_candidates))
