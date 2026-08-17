"""Write path (create/update) for memory_service: entity resolution and persistence.

Pure code-motion extraction (see refactor plan). The two deferred (in-function)
imports of disposition_service and librarian_service below are preserved verbatim,
including their comments -- they exist to avoid a real init-time circular import
(memory_service -> disposition_service -> relation_service -> memory_service, and
memory_service -> librarian_service -> memory_service).
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, UTC
from typing import Any, Literal

from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import extract_title_and_snippet, compute_content_hash
from saltmdb.utils.nlp import evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets

from . import tags as tag_ops
from . import validation
from ._shared import logger, RETRIEVAL_TEXT_UNSET


def _resolve_existing_entity_id(
    conn, entity_id: str | None, title: str, owner_id: str, scope: str, content_hash: str
) -> tuple[str | None, str | None]:
    """Resolves what entity id a `store_memory` call will target, before persistence.

    Returns (resolved_entity_id, error_message). error_message is only ever set for an exact
    content-hash collision (REJECT_EXACT_DUPLICATE) -- callers must return it immediately, same as
    always. resolved_entity_id is None for a fresh insert (no explicit entity_id, no hash
    collision, no same-title match); non-None means either the caller's own explicit entity_id, or
    a same-title/owner/scope temporal-upsert match.

    Track A (memory-core rework, see scratch/plans/track_a_disposition_detailed.md §0/§3):
    extracted out of `store_memory`'s body so `disposition_service.evaluate_store_preflight`/
    `commit_disposed_write` can determine the identical resolved target without duplicating this
    SQL, and so the exact same resolution can be re-run at both preflight and commit time for the
    review-token binding check.
    """
    if entity_id:
        return entity_id, None
    try:
        row = conn.execute(
            """
            SELECT id FROM entities
            WHERE content_hash = ? AND (owner_id = ? OR scope = 'shared') AND status != 'archived'
        """,
            (content_hash, owner_id),
        ).fetchone()
        if row:
            return (
                None,
                f"Error: REJECT_EXACT_DUPLICATE - Memory with exact content hash already exists with ID: {row[0]}",
            )
    except sqlite3.Error as exc:
        logger.debug("Exact content-hash lookup unavailable; continuing with title lookup: %s", exc)
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
    `store_memory` has always run, factored out so `disposition_service.commit_disposed_write`'s
    no-`consolidate`-disposition path reuses it rather than duplicating it (Track A, see
    scratch/plans/track_a_disposition_detailed.md §0/§3). Must run inside the caller's own write
    transaction. Returns (entity_id, was_existing) -- `was_existing` gates the "[Tip: ...]" suffix
    the same way the pre-Track-A code's local `existing` variable did.
    """
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

    cursor = conn.execute(
        "SELECT created_at, owner_id, valid_from, title, full_content, content_hash "
        "FROM entities WHERE id = ?",
        (entity_id,),
    )
    existing = cursor.fetchone()
    existing_retrieval_text = None
    existing_retrieval_hash = None
    if existing:
        created_at, owner, valid_from, prior_title, prior_content, prior_content_hash = existing
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
        hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"

        conn.execute(
            """
             INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, embedding_status, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash)
             SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, context_id, 'archived', content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash
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

    metadata_str = json.dumps(metadata) if metadata else None
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
    if is_core is None:
        is_core_val = None
    else:
        is_core_val = 1 if is_core in (True, 1, "true", "1", "True") else 0

    conn.execute(
        """
        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, content_hash, quality_score, quality_status, quality_flags, memory_type, retrieval_text, retrieval_text_hash)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'), ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            updated_at = excluded.updated_at,
            last_accessed_at = excluded.last_accessed_at,
            owner_id = COALESCE(excluded.owner_id, entities.owner_id),
            scope = excluded.scope,
            is_core = COALESCE(?, entities.is_core),
            weight = excluded.weight,
            status = entities.status,
            title = excluded.title,
            full_content = excluded.full_content,
            valid_from = excluded.valid_from,
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
            retrieval_text_hash = excluded.retrieval_text_hash
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
            is_core_val,
            memory_type,
        ),
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
    skip_duplicate_check: bool = False,
    context_id: str = None,
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    *,
    review_token: str | None = None,
    dispositions: list | None = None,
    retrieval_text: str | None | object = RETRIEVAL_TEXT_UNSET,
) -> str | dict:
    """Stores a consolidated Markdown fact chunk as a long-term memory.

    Track A (memory-core rework, see scratch/plans/track_a_disposition_detailed.md): every call
    runs a side-effect-free preflight before persistence. If evidence-gathering finds no flagged
    candidates, this behaves exactly as before -- a single call, same string return. If it finds
    one or more (a possible duplicate, supersession, or stale-consolidated-node signal), nothing
    is persisted; instead this returns a `REVIEW_REQUIRED` dict carrying an opaque `review_token`
    and the flagged candidates, each with an advisory (never authoritative) `suggested_label` and
    the disposition options available for it. Resend the identical call with `review_token` and
    `dispositions` (`[{"candidate_id": ..., "disposition": "distinct"|"supersede"|"consolidate"|
    "elaborate"}, ...]`, one entry per flagged candidate) to commit. A stale/expired token or a
    proposed write that no longer matches what was previewed returns `REVIEW_STALE` instead of
    persisting anything -- call again without `review_token` to get a fresh preflight.

    `skip_duplicate_check=True` bypasses the preflight entirely (same as before Track A), same as
    an explicit `entity_id` or a same-title/owner/scope match already resolving this call to an
    existing entity -- in both cases this is a direct write, not a create-or-flag decision.
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

        if not title:
            title, _ = extract_title_and_snippet(redacted_content)
        else:
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
            return f"Error: Memory quality check rejected (Score: {quality_res['quality_score']:.2f}). Reason: {quality_res['reason']}"

        content_hash = compute_content_hash(redacted_content)
        quality_score = quality_res["quality_score"]
        quality_status = quality_res["status"]
        quality_flags_str = json.dumps(quality_res["quality_flags"])

        resolved_entity_id, hash_collision_error = _resolve_existing_entity_id(
            conn, entity_id, title, owner_id, scope, content_hash
        )
        if hash_collision_error:
            return hash_collision_error

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
        }

        # Deferred import: disposition_service imports relation_service, which imports this very
        # module (memory_service) at ITS OWN top level -- a top-level import here would create a
        # real init-time cycle. Matches this function's other deferred imports below.
        from saltmdb.domain.services import disposition_service

        effective_db_path = db_path or get_db_path()

        if review_token:
            result = disposition_service.commit_disposed_write(
                conn, proposed, review_token, dispositions or [], effective_db_path
            )
            if isinstance(result, dict):
                return result  # REVIEW_STALE
            if isinstance(result, str) and result.startswith("Error"):
                return result
            entity_id_out = result.split("ID: ")[-1].strip()
            res_msg = result
        else:
            # Gated identically to the pre-Track-A dup-check: skipped whenever this call already
            # resolves to an existing entity (explicit entity_id OR a same-title/owner/scope
            # upsert match -- resolved_entity_id covers both) or the caller opted out, exactly
            # matching store_memory's original `if not entity_id and not skip_duplicate_check`
            # gate, which was itself checked AFTER entity_id could have been mutated by the
            # same-title match.
            if resolved_entity_id or skip_duplicate_check:
                preflight: dict[str, Any] = {"candidates": []}
            else:
                preflight = disposition_service.evaluate_store_preflight(
                    conn, proposed, effective_db_path
                )

            if preflight["candidates"]:
                return disposition_service.build_review_required_response(proposed, preflight)

            def _write(c):
                return _store_raw_entity(c, proposed)

            entity_id_out, was_existing = write_transaction_retrying(conn, _write)
            res_msg = f"Knowledge stored successfully with ID: {entity_id_out}"
            if not was_existing and tags:
                res_msg += " [Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]"

        from saltmdb.domain.services.librarian_service import trigger_librarian

        trigger_librarian(db_path=db_path, coordinator=coordinator)

        return res_msg
    except Exception as e:
        logger.error("Error storing knowledge: %s", e)
        return f"Error storing knowledge: {e}"
    finally:
        if should_close:
            close_connection(conn)
