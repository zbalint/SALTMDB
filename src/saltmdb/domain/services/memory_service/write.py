"""Write path (create/update) for memory_service: entity resolution and persistence."""

import json
import re
import sqlite3
import uuid
from difflib import SequenceMatcher
from datetime import datetime, UTC
from typing import Literal

from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import compute_content_hash
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets
from saltmdb.utils.envelope import error as envelope_error, rejected, ok as envelope_ok, warning
from saltmdb.domain.services import core_governance_service

from . import tags as tag_ops
from . import validation
from ._shared import logger, RETRIEVAL_TEXT_UNSET


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
        if row[0] != "#core"
        or any(_normalized_tag_key(tag) == "core" for tag in (submitted_tags or []))
    ]
    effective_by_key = {_normalized_tag_key(tag): tag for tag in effective_tags}
    near_misses: list[dict] = []
    for submitted in submitted_tags or []:
        if not isinstance(submitted, str) or not submitted.strip():
            continue
        submitted_key = _normalized_tag_key(submitted)
        if submitted_key in effective_by_key:
            continue
        candidate = max(
            effective_tags,
            key=lambda tag: SequenceMatcher(None, submitted_key, _normalized_tag_key(tag)).ratio(),
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


def _legacy_update_guard(  # noqa: C901, PLR0912
    conn,
    *,
    resolved_entity_id: str | None,
    title: str,
    content: str,
    tags: list | None,
    owner_id: str,
    scope: str,
    memory_type: str | None,
    context_id: str | None,
    metadata: dict | None,
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
        SELECT title, full_content, owner_id, context_id, scope, memory_type,
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
                "owner_id",
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
    changes: list[str] = []
    if title != current["title"]:
        changes.append("title")
    if content != current["full_content"]:
        changes.append("full_content")
    if owner_id != current["owner_id"]:
        changes.append("owner_id")
    # ``store_memory`` historically defaulted scope to ``shared``.  Treat that default as an
    # omitted value when the existing version is private; otherwise an administrative update that
    # never mentioned scope would be misclassified as a disclosure attempt.
    if scope != current["scope"] and not (scope == "shared" and current["scope"] == "private"):
        changes.append("scope")
    if memory_type is not None and memory_type != current["memory_type"]:
        changes.append("memory_type")
    if context_id is not None and context_id != current["context_id"]:
        changes.append("context_id")
    if tags is not None:
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
            if row[0].lower() != "#core"
        }
        if requested_tags != existing_tags:
            changes.append("tags")
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
    conn, entity_id: str | None, title: str, owner_id: str, scope: str, content_hash: str
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
            WHERE content_hash = ? AND (owner_id = ? OR scope = 'shared') AND status != 'archived'
        """,
            (content_hash, owner_id),
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
            WHERE title = ? AND owner_id = ? AND scope = ? AND status != 'archived'
        """,
            (title, owner_id, scope),
        ).fetchone()
        if row:
            return row[0], None
    except sqlite3.Error as exc:
        logger.debug("Title lookup unavailable; treating memory as a fresh insert: %s", exc)
    return None, None


def _store_raw_entity(conn, proposed: dict) -> tuple[str, bool]:  # noqa: C901, PLR0912, PLR0915
    """Persists `proposed` as a plain raw entity (a temporal upsert if `resolved_entity_id` names
    an already-existing row, otherwise a fresh insert) -- the same insert/tag/`#core`-sync logic
    Must run inside the caller's own write transaction. Returns (entity_id, was_existing) --
    `was_existing` gates the "[Tip: ...]" suffix.
    """
    was_existing_entity = bool(proposed.get("resolved_entity_id"))
    entity_id = proposed.get("resolved_entity_id") or str(uuid.uuid4())
    title = proposed["title"]
    redacted_content = proposed["content"]
    owner_id = proposed["owner_id"]
    scope = proposed["scope"]
    weight = proposed.get("weight") or 1
    is_core = proposed.get("is_core")
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
    now = datetime.now(UTC).isoformat()

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
            "owner_id": owner_id,
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

    is_core_val = 1 if core_state["is_core"] else 0

    cursor = conn.execute(
        "SELECT created_at, owner_id, valid_from, title, full_content, content_hash, metadata "
        "FROM entities WHERE id = ?",
        (entity_id,),
    )
    existing = cursor.fetchone()
    existing_retrieval_text = None
    existing_retrieval_hash = None
    if existing:
        (
            created_at,
            owner,
            valid_from,
            prior_title,
            prior_content,
            prior_content_hash,
            prior_metadata,
        ) = existing
        base_source_changed = (
            prior_title != title
            or prior_content != redacted_content
            or prior_content_hash != content_hash
        )
        prior_retrieval = conn.execute(
            "SELECT retrieval_text,retrieval_text_hash FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if prior_retrieval:
            existing_retrieval_text, existing_retrieval_hash = prior_retrieval
        if not proposed.get("_skip_scd_history", False):
            hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"
            conn.execute(
                """
                 INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, embedding_status, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_last_reviewed_at, core_last_reviewed_by, core_review_rationale, core_detail_memory_ids)
                 SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, context_id, 'archived', content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_review_after, core_last_reviewed_at, core_last_reviewed_by, core_review_rationale, core_detail_memory_ids
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
    else:
        base_source_changed = True

    if tags is not None:
        conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))

    if existing:
        current_metadata_dict = {}
        if prior_metadata:
            try:
                parsed_meta = json.loads(prior_metadata)
                if isinstance(parsed_meta, dict):
                    current_metadata_dict = parsed_meta
            except (json.JSONDecodeError, TypeError):
                current_metadata_dict = {}
        if metadata is not None:
            submitted_metadata_dict = metadata if isinstance(metadata, dict) else {}
            merged_meta = {**current_metadata_dict, **submitted_metadata_dict}
            metadata_str = json.dumps(merged_meta)
        else:
            metadata_str = prior_metadata
    else:
        metadata_str = json.dumps(metadata) if metadata is not None else None
    if existing:
        if retrieval_text_provided:
            final_retrieval_text = requested_retrieval_text
            final_retrieval_hash = (
                validation._retrieval_text_hash(final_retrieval_text)
                if final_retrieval_text is not None
                else None
            )
        else:
            final_retrieval_text = existing_retrieval_text
            final_retrieval_hash = existing_retrieval_hash
    else:
        final_retrieval_text = requested_retrieval_text if retrieval_text_provided else None
        final_retrieval_hash = (
            validation._retrieval_text_hash(final_retrieval_text)
            if final_retrieval_text is not None
            else None
        )
    core_detail_ids_str = (
        json.dumps(core_state["core_detail_memory_ids"])
        if core_state["core_detail_memory_ids"]
        else None
    )

    conn.execute(
        """
        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash, core_reason, core_exit_condition, core_review_after, core_detail_memory_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'), ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at = excluded.updated_at,
            last_accessed_at = excluded.last_accessed_at,
            owner_id = COALESCE(excluded.owner_id, entities.owner_id),
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
            entity_id,
            now,
            now,
            now,
            owner_id,
            scope,
            is_core_val,
            weight,
            json.dumps([]),
            title,
            redacted_content,
            now,
            metadata_str,
            context_id,
            content_hash,
            quality_score,
            quality_status,
            quality_flags_str,
            memory_type,
            final_retrieval_text,
            final_retrieval_hash,
            core_state["core_reason"],
            core_state["core_exit_condition"],
            core_state["core_review_after"],
            core_detail_ids_str,
            memory_type,
        ),
    )

    core_governance_service.reconcile_detail_relations(
        conn,
        core_id=entity_id,
        owner_id=owner_id,
        new_detail_ids=core_state["core_detail_memory_ids"],
        previous_detail_ids=core_state["previous_detail_memory_ids"],
    )

    if tags is not None:
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
                tag_id = tag_ops.resolve_or_create_tag(conn, tag_name, agent_id=owner_id)
                if tag_id:
                    tag_lookup[norm_input] = tag_id

            if not tag_id:
                continue

            conn.execute(
                "INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)",
                (entity_id, tag_id),
            )

    # Stage 4.5: is_core -> #core tag sync. is_core is the single writable source of
    # truth; #core is a derived label the server maintains so the two can never drift
    # apart again. Runs on every write (even calls that touch neither is_core nor tags),
    # which also self-heals any pre-existing drift the next time an entity is touched.
    resolved_row = conn.execute(
        "SELECT is_core FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    resolved_is_core = bool(resolved_row[0]) if resolved_row else False
    core_tag_id = tag_ops.resolve_or_create_tag(conn, "#core", agent_id=owner_id)
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

    return entity_id, bool(existing)


def store_memory(  # noqa: C901, PLR0911, PLR0912, PLR0915
    content: str = None,
    tags: list = None,
    owner_id: str = None,
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
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    *,
    retrieval_text: str | None | object = RETRIEVAL_TEXT_UNSET,
    core_reason: str | None = None,
    core_exit_condition: str | None = None,
    core_review_after: str | None = None,
    detail_memory_ids: list | None = None,
) -> str | dict:
    """Stores a consolidated Markdown fact chunk as a long-term memory.

    Exact content-hash duplicates are rejected before any write and identify the existing entity.
    Fresh near duplicates are persisted normally; the success response includes inline candidate
    IDs/titles/similarity scores and directs callers to `supersede_memory` or
    `consolidate_memories` when they determine the knowledge should be replaced or merged.

    Core-memory governance (see core_governance_service.py): `is_core=True` requires `scope=
    "shared"`, `core_reason`/`core_exit_condition` (each 20-500 characters), and admits a hard
    global cap on active-core count/per-memory length/rendered bootstrap size -- a capacity
    failure returns a `status: "REJECTED"` dict with zero side effects (no memory, relation, or
    other state created), never a partial write. Omitting `core_reason`/`core_exit_condition`/
    `core_review_after`/`detail_memory_ids` on an UPDATE to an already-core memory preserves the
    existing values; supplying any of them while the effective memory is NOT core is rejected,
    never silently ignored. `detail_memory_ids=None` preserves the current declaration, `[]`
    clears it, a replacement list atomically reconciles the declared `elaborates_on` relations.
    """
    if not owner_id:
        return "Error: owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."

    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."

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

    if (
        relevance is not None
        or impact is not None
        or novelty is not None
        or actionability is not None
    ):
        r = relevance if relevance is not None else 3
        im = impact if impact is not None else 3
        n = novelty if novelty is not None else 3
        a = actionability if actionability is not None else 3
        weight = max(1, min(5, (r + im + n + a) // 4))

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        redacted_content = redact_secrets(content)

        retrieval_text_provided = retrieval_text is not RETRIEVAL_TEXT_UNSET
        if retrieval_text_provided:
            if retrieval_text is None:
                return "Error: retrieval_text JSON null is ambiguous; omit the field to preserve or use an empty string to clear."
            if not isinstance(retrieval_text, str):
                return "Error: retrieval_text must be a string"
            try:
                normalized_retrieval_text = validation.normalize_retrieval_text(retrieval_text)
            except ValueError as exc:
                return f"Error: {exc}"
        else:
            normalized_retrieval_text = None

        if title:
            title = redact_secrets(title)

        if not title or not title.strip():
            return "Error: title is mandatory and cannot be empty."

        try:
            validation.validate_memory_input(title, redacted_content, metadata)
        except ValueError as e:
            return str(e)

        # Stage 1: Auto-Formatting (Idempotent cleanup: f(f(x)) = f(x))
        from saltmdb.utils.nlp import auto_format_markdown

        redacted_content = auto_format_markdown(redacted_content)

        if not context_id and metadata and isinstance(metadata, dict):
            context_id = metadata.get("project") or metadata.get("project_id")

        # Stage 2 & 3: Extract Prose & Pre-Embedding Quality Gate Evaluation
        quality_res = evaluate_memory_quality(redacted_content, title)
        if quality_res["status"] == "REJECT":
            return rejected(
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
            )

        content_hash = compute_content_hash(redacted_content)
        quality_score = quality_res["quality_score"]
        quality_status = quality_res["status"]
        quality_flags_str = json.dumps(quality_res["quality_flags"])

        resolved_entity_id, hash_collision_error = _resolve_existing_entity_id(
            conn, entity_id, title, owner_id, scope, content_hash
        )
        if hash_collision_error:
            return hash_collision_error

        legacy_error, frozen_current = _legacy_update_guard(
            conn,
            resolved_entity_id=resolved_entity_id,
            title=title,
            content=redacted_content,
            tags=tags,
            owner_id=owner_id,
            scope=scope,
            memory_type=memory_type,
            context_id=context_id,
            metadata=metadata,
        )
        if legacy_error is not None:
            return legacy_error
        if frozen_current is not None:
            # The current row is the immutable source of truth for an administrative-only
            # update.  Preserve omitted/default frozen inputs before entering _store_raw_entity.
            title = frozen_current["title"]
            redacted_content = frozen_current["full_content"]
            owner_id = frozen_current["owner_id"]
            scope = frozen_current["scope"]
            context_id = frozen_current["context_id"]
            memory_type = frozen_current["memory_type"]
            content_hash = frozen_current["content_hash"]
            tags = None
            if metadata is None and frozen_current["metadata"]:
                try:
                    metadata = json.loads(frozen_current["metadata"])
                except json.JSONDecodeError:
                    metadata = None

        proposed = {
            "content": redacted_content,
            "title": title,
            "tags": tags,
            "owner_id": owner_id,
            "scope": scope,
            "memory_type": memory_type,
            "context_id": context_id,
            "is_core": is_core,
            "weight": weight,
            "metadata": metadata,
            "resolved_entity_id": resolved_entity_id,
            "content_hash": content_hash,
            "quality_score": quality_score,
            "quality_status": quality_status,
            "quality_flags_str": quality_flags_str,
            "retrieval_text": normalized_retrieval_text,
            "retrieval_text_provided": retrieval_text_provided,
            "core_reason": core_reason,
            "core_exit_condition": core_exit_condition,
            "core_review_after": core_review_after,
            "detail_memory_ids": detail_memory_ids,
            "_skip_scd_history": frozen_current is not None,
        }

        # Capacity/lifecycle validation remains side-effect free and authoritative persistence
        # still happens inside the write transaction. Similarity is deliberately advisory only.
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
                "owner_id": owner_id,
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
            return f"Error: {e}"

        if core_state_preview["is_core"] and preview_target_will_be_active:
            rejection = core_governance_service.check_capacity_admission(
                conn,
                exclude_ids=[resolved_entity_id] if resolved_entity_id else [],
                new_entry=prospective_entry_preview,
            )
            if rejection is not None:
                return rejection

        def _write(c):
            return _store_raw_entity(c, proposed)

        try:
            entity_id_out, was_existing = write_transaction_retrying(conn, _write)
        except core_governance_service.CoreGovernanceRejected as e:
            return e.payload if isinstance(e.payload, dict) else f"Error: {e.payload}"
        res_msg = f"Knowledge stored successfully with ID: {entity_id_out}"
        if not was_existing and tags:
            res_msg += " [Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]"

        # Near duplicates are warnings, never a write gate.  Keep this probe after persistence so
        # callers receive the candidate IDs alongside the newly-created memory even when they do
        # not follow the guidance.
        duplicate_candidates: list[dict] = []
        if not was_existing:
            from .duplicates import check_duplicate_memories

            duplicate_result = check_duplicate_memories(
                title=title,
                content=redacted_content,
                owner_id=owner_id,
                tags=tags,
                context_id=context_id,
                exclude_ids=[entity_id_out],
                db_connection=conn,
                db_path=db_path,
            )
            duplicate_candidates = duplicate_result.get("potential_duplicates") or []

        effective_tags, tag_near_misses = _effective_tag_report(conn, entity_id_out, tags)
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
            response_warnings.append(
                warning(
                    "NEAR_DUPLICATE",
                    "Memory stored; review duplicate_candidates and use supersede_memory for one replacement or consolidate_memories to merge several.",
                    {
                        "duplicate_candidates": duplicate_candidates,
                        "guidance": {
                            "single": "supersede_memory",
                            "several": "consolidate_memories",
                        },
                    },
                )
            )

        from saltmdb.domain.services.librarian_service import trigger_librarian

        trigger_librarian(db_path=db_path, coordinator=coordinator)

        return envelope_ok(
            {
                "id": entity_id_out,
                "message": res_msg,
                "submitted_tags": list(tags or []),
                "effective_tags": effective_tags,
                "duplicate_candidates": duplicate_candidates,
            },
            warnings=response_warnings,
            effective={
                "owner_id": owner_id,
                "context_id": context_id,
                "scope": scope,
                "memory_type": effective_memory_type_preview,
            },
        )
    except Exception as e:
        logger.error("Error storing knowledge: %s", e)
        return f"Error storing knowledge: {e}"
    finally:
        if should_close:
            close_connection(conn)
