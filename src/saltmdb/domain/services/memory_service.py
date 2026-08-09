import uuid
import json
import re
import logging
from datetime import datetime, UTC
from typing import Any, Literal
from saltmdb.config import (
    get_db_path,
    DEDUP_SUPERSESSION_THRESHOLD,
    DEDUP_DUPLICATE_THRESHOLD,
    DEDUP_LEXICAL_THRESHOLD,
    BM25_TITLE_WEIGHT,
    BM25_CONTENT_WEIGHT,
    BM25_ALIAS_WEIGHT,
    RELATION_COUNT_PENALTY,
    SNIPPET_MAX_TOKENS,
    SNIPPET_MATCH_START,
    SNIPPET_MATCH_END,
    SNIPPET_ELLIPSIS,
    SUPERSESSION_CHAIN_MAX_DEPTH,
    STRICT_OVERFETCH_CANDIDATE_CAP,
)
from saltmdb.db.connection import get_connection, is_coordinator_connection, write_transaction_retrying, close_connection
from saltmdb.utils.text import (
    resolve_entity_id,
    extract_title_and_snippet,
    sanitize_fts_query,
    compute_content_hash,
)
from saltmdb.utils.nlp import word_sim, evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
_embed_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saltmdb-embed")
_search_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="saltmdb-search")


TITLE_MIN_LENGTH = 5
TITLE_MAX_LENGTH = 120


def validate_memory_input(title: str, content: str, metadata: dict | None) -> None:
    """Validates memory input to enforce title length bounds."""
    if title:
        stripped_title = title.strip()
        if len(stripped_title) > TITLE_MAX_LENGTH:
            raise ValueError(
                f"Error: Title exceeds the maximum length of {TITLE_MAX_LENGTH} characters (got {len(stripped_title)}). "
                "Titles must be a short canonical label in '[Domain] Topic' form, not the memory body. "
                "Move the full text into the 'content' parameter."
            )
        if len(stripped_title) < TITLE_MIN_LENGTH:
            raise ValueError(
                f"Error: Title is too short (minimum {TITLE_MIN_LENGTH} characters). Provide a descriptive, canonical title."
            )


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
    except Exception:
        pass
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
    except Exception:
        pass
    return None, None


def _store_raw_entity(conn, proposed: dict) -> tuple[str, bool]:
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
    now = datetime.now(UTC).isoformat()

    cursor = conn.execute(
        "SELECT created_at, owner_id, valid_from FROM entities WHERE id = ?", (entity_id,)
    )
    existing = cursor.fetchone()
    if existing:
        created_at, owner, valid_from = existing
        hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"

        conn.execute(
            """
             INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, embedding_status, content_hash, quality_score, quality_status, quality_flags, memory_type)
             SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, context_id, 'archived', content_hash, quality_score, quality_status, quality_flags, memory_type
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

    if tags is not None:
        conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))

    metadata_str = json.dumps(metadata) if metadata else None
    if is_core is None:
        is_core_val = None
    else:
        is_core_val = 1 if is_core in (True, 1, "true", "1", "True") else 0

    conn.execute(
        """
        INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, context_id, content_hash, quality_score, quality_status, quality_flags, memory_type)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, COALESCE(?, 'fact'))
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
            memory_type = COALESCE(?, entities.memory_type)
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
                tag_id = resolve_or_create_tag(conn, tag_name, agent_id=owner_id)
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
    core_tag_id = resolve_or_create_tag(conn, "#core", agent_id=owner_id)
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
    from saltmdb.domain.services.embedding_service import enqueue_embedding_jobs_for_entity

    enqueue_embedding_jobs_for_entity(conn, entity_id, title, redacted_content, content_hash)

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

        if not title:
            title, _ = extract_title_and_snippet(redacted_content)
        else:
            title = redact_secrets(title)

        if not title or not title.strip():
            return "Error: title is mandatory and cannot be empty."

        try:
            validate_memory_input(title, redacted_content, metadata)
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
                preflight = {"candidates": []}
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


STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "he",
    "in",
    "is",
    "it",
    "its",
    "of",
    "on",
    "that",
    "the",
    "to",
    "was",
    "were",
    "will",
    "with",
    "what",
    "my",
    "when",
    "i",
    "type",
    "did",
    "how",
    "does",
    "or",
    "which",
    "who",
    "whom",
    "this",
    "these",
    "those",
}


def _run_fts_search(
    conn,
    sanitized_query: str,
    where_clauses: list,
    params: list,
    limit: int,
    offset: int,
    *,
    return_fallback_flag: bool = False,
) -> list | tuple[list, bool]:
    """Execute the FTS5/BM25 query with AND->OR fallback. Returns sqlite3 Row list.

    Each row's last column, fts_snippet, is a query-centered excerpt of full_content
    (FTS5 snippet(), column index 2) -- populated because this row genuinely matched via
    FTS5 MATCH in this query, distinct from rows that only surface via semantic_search().

    `return_fallback_flag` (opt-in, default False for backward compatibility with the two
    benchmark scripts that call this function directly): when True, returns
    `(rows, used_or_fallback)` instead of a bare row list, where `used_or_fallback` is True iff
    the AND-joined MATCH found nothing and the OR-joined retry is what actually produced `rows`.
    This is a single pool-level bool, not a per-row property -- the OR-fallback is an
    all-or-nothing property of how the query as a whole was executed. Every return path
    (including the early-return empty-terms case) honors the flag: `([], False)`, never a bare
    `[]`, when `return_fallback_flag=True`.
    """
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in STOP_WORDS]
    if not terms:
        terms = raw_terms

    if not terms:
        return ([], False) if return_fallback_flag else []

    fts_query_str = " ".join(f'"{t}"*' for t in terms)
    where_sql = f" AND {' AND '.join(where_clauses)}" if where_clauses else ""
    bm25_weights = f"{BM25_TITLE_WEIGHT}, {BM25_CONTENT_WEIGHT}, {BM25_ALIAS_WEIGHT}"
    # full_content is column index 2 in entities_fts (id=0 UNINDEXED, title=1, full_content=2,
    # search_aliases=3 -- see db/schema.py). Markers/ellipsis/budget are static config
    # constants inlined as literals (not `?` params) to avoid disturbing exec_params ordering.
    snippet_sql = (
        f"snippet(entities_fts, 2, '{SNIPPET_MATCH_START}', '{SNIPPET_MATCH_END}', "
        f"'{SNIPPET_ELLIPSIS}', {SNIPPET_MAX_TOKENS})"
    )
    sql = f"""
        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
               bm25(entities_fts, {bm25_weights}) as rank_score,
               e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id, e.memory_type,
               (SELECT COUNT(*) FROM relations r WHERE r.target_id = e.id
                AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))) as rel_count,
               {snippet_sql} as fts_snippet
        FROM entities_fts fts
        JOIN entities e ON fts.id = e.id
        WHERE fts.entities_fts MATCH ?{where_sql}
        ORDER BY (bm25(entities_fts, {bm25_weights}) * e.weight + (rel_count * {RELATION_COUNT_PENALTY})) ASC,
                 e.updated_at DESC
        LIMIT ? OFFSET ?
    """
    exec_params = [fts_query_str] + params + [limit, offset]
    rows = conn.execute(sql, exec_params).fetchall()
    used_or_fallback = False
    if not rows and len(terms) > 1:
        fts_fallback_query = " OR ".join(f'"{t}"*' for t in terms)
        exec_params_fb = [fts_fallback_query] + params + [limit, offset]
        rows = conn.execute(sql, exec_params_fb).fetchall()
        # True iff the OR retry actually produced rows (Codex review finding) -- not merely
        # "the OR branch ran." An AND-empty query whose OR retry ALSO finds nothing leaves
        # `rows` empty either way, but the flag's own contract ("the OR-joined retry is what
        # produced rows", see this function's docstring) should reflect reality precisely, not
        # just "the fallback was attempted."
        used_or_fallback = bool(rows)
    return (rows, used_or_fallback) if return_fallback_flag else rows


def semantic_search(
    query: str,
    where_clauses: list[str],
    params: list,
    limit: int,
    db_path: str,
    offset: int = 0,
) -> list[tuple[str, float]]:
    """Return [(entity_id, cosine_distance), ...] ascending by distance.

    Opens its own dedicated connection so it can safely load the sqlite_vec
    extension without conflicting with a concurrent FTS search on a shared conn.

    Raises on failure (post-Codex-review fix, P0): model loading, embedding,
    sqlite-vec extension loading, or the vector SQL query all propagate their
    exception to the caller instead of being swallowed into an empty result. A
    caller-side `except -> []` here would be indistinguishable from a genuinely
    successful query that just found zero candidates -- search_memory's RRF merge
    would then quietly present ordinary-looking FTS-only results, which is
    exactly the silent-fallback failure mode Part 0 already retired for the
    semantic-disabled case. search_memory() is expected to let this exception
    propagate up to its own top-level `except Exception -> [{"error": ...}]`
    handler rather than catching it locally.
    """
    conn = None
    try:
        import sqlite_vec
        from saltmdb.domain.services import embedding_service
        from saltmdb.db.connection import get_connection

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        query_vector = embedding_service.embed_text(query)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        # vec0's `MATCH ... AND k = N` is its KNN execution path.  Calling
        # vec_distance_cosine() in ORDER BY forces a full scan of every embedding and made
        # realistic frozen-corpus evaluation (and normal search at scale) effectively hang.
        # Request enough neighbours to retain the existing pagination contract, then apply the
        # ordinary SQL limit/offset below.
        knn_k = limit + offset
        sql = f"""
            SELECT e.id, ee.distance as distance
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            WHERE ee.embedding MATCH ? AND k = ?
              AND e.embedding_status = 'ready' AND {where_sql}
            LIMIT ? OFFSET ?
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector), knn_k] + params + [limit, offset]
        rows = conn.execute(sql, exec_params).fetchall()
        return [(row[0], row[1]) for row in rows]
    finally:
        if conn:
            close_connection(conn)


def _batch_semantic_similarities(
    candidate_ids: list[str],
    query_vector: list[float],
    db_path: str,
) -> dict[str, float]:
    """Return {entity_id: cosine_similarity} for candidates with a ready precomputed vector.

    Looks up precomputed vectors for the given candidate IDs in a single SQL query instead
    of re-embedding each candidate's text. Opens its own dedicated connection and loads
    sqlite_vec on it, same reasoning as semantic_search(): must not conflict with a
    shared/FTS connection the caller may have passed in. Candidates whose embedding_status
    != 'ready' are simply absent from the result; callers should fall back to a lexical
    comparison for those.
    """
    if not candidate_ids:
        return {}
    conn = None
    try:
        import sqlite_vec

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        placeholders = ",".join("?" for _ in candidate_ids)
        sql = f"""
            SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            WHERE e.embedding_status = 'ready' AND e.id IN ({placeholders})
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector)] + list(candidate_ids)
        rows = conn.execute(sql, exec_params).fetchall()
        return {row[0]: 1.0 - row[1] for row in rows}
    except Exception as e:
        logger.warning("Batched semantic similarity lookup failed, falling back to lexical: %s", e)
        return {}
    finally:
        if conn:
            close_connection(conn)


def rerank_candidates_by_topic(
    query_text: str,
    candidate_ids: list[str],
    db_path: str,
) -> dict[str, dict]:
    """Return {entity_id: {"topic_score": float, "semantic_verdict": str}} for candidates
    scorable from PRECOMPUTED entity_chunk_embeddings rows (Phase 2 Part B -- see plans/ and
    SALTMDB memory `5c09effa`).

    Never re-chunks/re-embeds candidate content -- that's the deliberate, load-bearing deviation
    from the original Gemini-generated spec (which re-chunked/re-embedded candidates live on
    every search), reusing Foundation's precomputed chunk vectors instead. IDs with zero chunk
    rows are simply absent from the returned dict -- callers apply their own fallback (see
    search_memory's rerank_by_topic handling).

    Algorithm: chunk the query text the same way entity content is chunked (usually one chunk
    for a realistic search query, implemented generally), batch-embed all query chunks in one
    call, then for each query chunk vector run one SQL query that computes, per candidate,
    MIN(vec_distance_cosine(...)) grouped by entity_id -- the "max similarity over this
    candidate's chunks" half of Mean(Max(cosine_similarity)), since distance is 1 - similarity so
    MIN(distance) is exactly MAX(similarity). Accumulating (1.0 - min_distance) across every query
    chunk and dividing by the chunk count gives the "mean over query chunks" half. Same
    own-dedicated-connection / try-except-log-and-return-{} shape as _batch_semantic_similarities
    and semantic_search.

    Staleness guard (post-Codex-review fix): the SQL joins entity_chunk_embeddings to entities
    and requires `c.content_hash IS e.content_hash` plus `e.status != 'archived'`. Without this, a
    chunk row left behind by a failed/in-flight async refresh (Part A's write path) would still
    be readable and could influence topic_score until the next startup repair sweep -- the join
    makes staleness exclusion synchronous with every call instead. A candidate whose only chunk
    rows are stale is excluded from the returned dict exactly like a candidate with zero chunk
    rows, so it falls through to the caller's fallback tier (_batch_semantic_similarities) rather
    than silently vanishing.
    """
    if not candidate_ids:
        return {}
    conn = None
    try:
        import sqlite_vec

        from saltmdb.config import (
            CHUNK_SIZE_CHARS,
            CHUNK_OVERLAP_CHARS,
            RERANK_SAME_TOPIC_THRESHOLD,
            RERANK_BROAD_THEME_THRESHOLD,
        )
        from saltmdb.utils.chunking import chunk_text
        from saltmdb.domain.services import embedding_service

        query_chunks = chunk_text(query_text or "", CHUNK_SIZE_CHARS, CHUNK_OVERLAP_CHARS)
        if not query_chunks:
            query_chunks = [{"text": query_text or ""}]
        query_vectors = embedding_service.embed_texts([c["text"] for c in query_chunks])
        if not query_vectors:
            return {}

        conn = get_connection(db_path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        placeholders = ",".join("?" for _ in candidate_ids)
        sql = f"""
            SELECT c.entity_id, MIN(vec_distance_cosine(c.embedding, ?)) AS min_distance
            FROM entity_chunk_embeddings c
            JOIN entities e ON e.id = c.entity_id
            WHERE c.entity_id IN ({placeholders})
              AND e.status != 'archived'
              AND c.content_hash IS e.content_hash
            GROUP BY c.entity_id
        """
        similarity_sums: dict[str, float] = {}
        for qv in query_vectors:
            exec_params = [sqlite_vec.serialize_float32(qv)] + list(candidate_ids)
            rows = conn.execute(sql, exec_params).fetchall()
            for entity_id, min_distance in rows:
                similarity_sums[entity_id] = similarity_sums.get(entity_id, 0.0) + (
                    1.0 - min_distance
                )

        num_query_chunks = len(query_vectors)
        results: dict[str, dict] = {}
        for entity_id, total in similarity_sums.items():
            topic_score = total / num_query_chunks
            if topic_score >= RERANK_SAME_TOPIC_THRESHOLD:
                verdict = "SAME_SPECIFIC_TOPIC"
            elif topic_score >= RERANK_BROAD_THEME_THRESHOLD:
                verdict = "BROADLY_RELATED_THEMES"
            else:
                verdict = "DIFFERENT_TOPICS"
            results[entity_id] = {"topic_score": topic_score, "semantic_verdict": verdict}
        return results
    except Exception as e:
        logger.warning("Cross-chunk topic reranking failed, falling back: %s", e)
        return {}
    finally:
        if conn:
            close_connection(conn)


def _score_topics_with_fallback(query_text: str, ids: list[str], db_path: str) -> dict[str, dict]:
    """Shared by rerank_by_topic's full-pool Stage-2 rerank and mode="strict"'s on-demand,
    FTS-less-candidates-only grounding lookup (Part B) -- factored out so both call sites use
    exactly one code path for "score these ids via chunk-level topic_score, falling back to
    entity-level cosine similarity (B4) for any id rerank_candidates_by_topic couldn't score (not
    yet chunk-embedded)". No candidate in `ids` is ever dropped: a fallback-tier id gets a
    BROADLY_RELATED_THEMES/DIFFERENT_TOPICS verdict from RERANK_BROAD_THEME_THRESHOLD instead of
    the primary tier's SAME_SPECIFIC_TOPIC/BROADLY_RELATED_THEMES/DIFFERENT_TOPICS three-way split.
    """
    if not ids:
        return {}
    topic_scores = rerank_candidates_by_topic(query_text, ids, db_path)
    missing_ids = [eid for eid in ids if eid not in topic_scores]
    if missing_ids:
        from saltmdb.config import RERANK_BROAD_THEME_THRESHOLD
        from saltmdb.domain.services import embedding_service

        fallback_vec = embedding_service.embed_text(query_text)
        fallback = _batch_semantic_similarities(missing_ids, fallback_vec, db_path)
        for eid in missing_ids:
            fscore = fallback.get(eid, 0.0)
            fverdict = (
                "BROADLY_RELATED_THEMES"
                if fscore >= RERANK_BROAD_THEME_THRESHOLD
                else "DIFFERENT_TOPICS"
            )
            topic_scores[eid] = {"topic_score": fscore, "semantic_verdict": fverdict}
    return topic_scores


def reciprocal_rank_fusion(
    fts_results: list,
    semantic_results: list[tuple[str, float]],
    limit: int,
    k: int = 60,
) -> dict[str, float]:
    """Merge two ranked lists by rank position (not raw score). Returns {entity_id: rrf_score}."""
    scores: dict[str, float] = {}
    for rank, row in enumerate(fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (entity_id, _distance) in enumerate(semantic_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return dict(ranked[:limit])


def _rrf_gap_confident(rrf_score_map: dict[str, float], fts_ids: set, semantic_ids: set) -> bool:
    """True when RRF's top1 candidate is (a) matched by BOTH the FTS and dense-vector channels
    and (b) separated from top2 by RERANK_GAP_SKIP_RATIO or more -- hybrid search already has a
    decisive, corroborated winner and Stage-2 rerank_by_topic has no signal worth adding (see
    SALTMDB memory 870a1d4e, Q8: rerank overrode a dual-channel, ~2x-margin decisive winner with a
    noise-level embedding-cosine call). A tie (Q1-style, ~1.0x, or a top1 matched by only one
    channel) still falls through to rerank -- exactly the ambiguous case rerank helps with.
    Requiring dual-channel support, not ratio alone, avoids trusting a numeric gap that isn't
    actually backed by real retrieval agreement (see RERANK_GAP_SKIP_RATIO's config.py comment for
    the calibration data behind this).
    """
    ids = list(rrf_score_map.keys())
    scores = list(rrf_score_map.values())
    if len(scores) < 2 or scores[1] <= 0:
        return False
    top1_id = ids[0]
    if top1_id not in fts_ids or top1_id not in semantic_ids:
        return False
    from saltmdb.config import RERANK_GAP_SKIP_RATIO

    return (scores[0] / scores[1]) >= RERANK_GAP_SKIP_RATIO


def _apply_type_bias(ordered_ids: list, conn) -> list:
    """Part 2 (SALTMDB memory 870a1d4e, prefer_durable_types): stable-partitions `event`-typed
    candidates to the back of ordered_ids, preserving relative order within each group. `event`
    memories are working/session notes prone to staleness by design (see SALTMDB memory 870a1d4e's
    Q12 case) -- the other four memory_type values (fact/decision/procedure/preference) are
    treated as durable and kept in front. No-op on an empty pool.
    """
    if not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})", ordered_ids
    ).fetchall()
    event_ids = {row[0] for row in rows if row[1] == "event"}
    return [eid for eid in ordered_ids if eid not in event_ids] + [
        eid for eid in ordered_ids if eid in event_ids
    ]


def _compute_superseded_ids(ordered_ids: list, conn) -> set:
    """Shared query: ids within ordered_ids that are the TARGET of a currently-valid outgoing
    `supersedes` edge (`A supersedes B` -> B, the target, is the old/superseded one -- matches this
    codebase's `consolidated_from` precedent of source=new/target=old). Factored out of
    `_apply_supersession_demotion` so mode="history" (Part C) can reuse the exact same
    single-hop "is this superseded right now" check to TAG candidates without demoting or hiding
    them, instead of duplicating the SQL. No-op (empty set) on an empty pool.
    """
    if not ordered_ids:
        return set()
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT target_id FROM relations
        WHERE target_id IN ({placeholders}) AND predicate = 'supersedes'
          AND (valid_to IS NULL OR datetime(valid_to) > datetime('now'))
        """,
        ordered_ids,
    ).fetchall()
    return {row[0] for row in rows}


def _compute_bitemporal_target_ids(ordered_ids: list, conn, predicate: str, now: str) -> set:
    """Shared core: ids within ordered_ids that are the TARGET of a currently-valid outgoing
    `predicate` edge, "currently valid" meaning the full four-column bitemporal predicate
    (`valid_from`/`valid_to`/`valid_at`/`invalid_at`) holds at the single caller-supplied `now`
    instant -- not each column checked against its own independently-sampled clock read. Callers
    that need internal consistency across multiple predicate checks (e.g.
    `_apply_strict_ranking_defaults` checking both `supersedes` and `corrects`) MUST capture `now`
    once and pass the same value into every call, or a candidate could be classified differently by
    two checks that should agree (SALTMDB roadmap ba2cf66f P1#6 plan, Codex round-1 finding). No-op
    (empty set) on an empty pool.
    """
    if not ordered_ids:
        return set()
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT target_id FROM relations
        WHERE target_id IN ({placeholders}) AND predicate = ?
          AND (valid_from IS NULL OR datetime(valid_from) <= datetime(?))
          AND (valid_to IS NULL OR datetime(valid_to) > datetime(?))
          AND (valid_at IS NULL OR datetime(valid_at) <= datetime(?))
          AND (invalid_at IS NULL OR datetime(invalid_at) > datetime(?))
        """,
        ordered_ids + [predicate, now, now, now, now],
    ).fetchall()
    return {row[0] for row in rows}


def _compute_superseded_ids_bitemporal(ordered_ids: list, conn) -> set:
    """mode="history"'s own single-hop "is this superseded right now" check (Part C) -- NOT the
    same query as `_compute_superseded_ids` above (Codex review P1 finding, correctly caught): that
    function only checks `valid_to`, a pre-existing precedent from `_apply_supersession_demotion`
    which this plan explicitly leaves unchanged ("stays single-hop, sink-to-bottom, and unchanged"
    -- see that function's own docstring). But `history` mode's own docs promise `is_superseded`
    reflects a "currently-valid" edge in the same full bitemporal sense Part A's resolver uses, so
    it needs the same four-column predicate (`valid_from`/`valid_to`/`valid_at`/`invalid_at`), not
    demote_superseded's narrower single-column one -- reusing `_apply_supersession_demotion`'s
    check here would silently tag an edge whose `valid_from` is still in the future, or whose
    `invalid_at` has already passed, as "currently superseding" when it isn't. No-op on an empty
    pool.

    Thin wrapper over `_compute_bitemporal_target_ids` (SALTMDB roadmap ba2cf66f P1#6 plan) --
    own `now` sample per call, matching this function's pre-existing single-call-site behavior
    under mode="history" (which never needs cross-predicate consistency, unlike
    `_apply_strict_ranking_defaults` below).
    """
    return _compute_bitemporal_target_ids(
        ordered_ids, conn, "supersedes", datetime.now(UTC).isoformat()
    )


def _apply_supersession_demotion(ordered_ids: list, conn) -> list:
    """Part 2 (SALTMDB memory 870a1d4e, demote_superseded): stable-partitions candidates that are
    the TARGET of a currently-valid outgoing `supersedes` edge to the back of ordered_ids,
    preserving relative order within each group. `A supersedes B` means A (source) is the
    new/authoritative memory and B (target) is the old one it replaces -- matching this codebase's
    `consolidated_from` precedent (source = new summary, target = old raw parent) -- so it is the
    target side that gets demoted here, not the source (corrects a direction bug in 870a1d4e's own
    original wording, confirmed during implementation review). Uses this file's own existing
    "currently valid" literal idiom (see the related_map query above) rather than
    relation_service's separate point-in-time parameter style. No-op on an empty pool.

    This is single-hop, sink-to-bottom, independently-togglable demotion -- structurally separate from the
    multi-hop chain-resolution *substitution* `_resolve_supersession_chains` performs for
    mode="strict" below (Part A of plans/scalable-strolling-stallman.md); this flag/function is
    unchanged by that work.
    """
    if not ordered_ids:
        return []
    superseded_ids = _compute_superseded_ids(ordered_ids, conn)
    return [eid for eid in ordered_ids if eid not in superseded_ids] + [
        eid for eid in ordered_ids if eid in superseded_ids
    ]


def _apply_strict_ranking_defaults(ordered_ids: list, conn) -> list:
    """mode="strict"-only forced ranking defaults (SALTMDB roadmap ba2cf66f P1#6, design 1fddc04a):
    durable-type preference + a residual-supersession/correction safety-net demotion, applied
    unconditionally regardless of the caller's own prefer_durable_types/demote_superseded flags
    (which keep their existing, independently-togglable meaning for broad/history -- untouched by this
    function). Order matches _apply_type_bias-then-demotion's existing precedent (Part 2, SALTMDB
    memory 870a1d4e): type bias first, so an explicitly stale/wrong item always sinks below a
    merely-event-typed one, not the reverse.

    Covers two cases `demote_superseded`/`_resolve_supersession_chains` don't, by design:
    - A candidate Part A's chain resolver abstained on (cycle, depth-cap breach, or archived
      intermediate node) stays in the pool under its original id, still bitemporally superseded --
      substitution deliberately declined to touch it, so without this safety net it would rank as
      if authoritative.
    - A candidate that is the target of a currently-valid `corrects` edge -- a predicate Part A's
      resolver has no concept of (it only walks `supersedes` chains), and `demote_superseded`
      (kept intentionally unchanged) never checked either.

    One `now` captured here and passed into both bitemporal lookups so a validity-boundary-
    straddling edge can't be classified differently by the two predicate checks (Codex plan-review
    round-1 finding, `plans/amber-sifting-falcon.md`). Demoted ids are unioned (not two sequential
    partitions) so an id caught by both checks sinks once, not double-processed -- both signal "this
    specific memory is known wrong/outdated," treated as one demotion tier. Demotion changes
    position only, never presence -- a demoted candidate already independently cleared
    accept_or_abstain's gate on its own evidence merits before this function ever sees it. No-op on
    an empty pool.
    """
    if not ordered_ids:
        return []
    ordered_ids = _apply_type_bias(ordered_ids, conn)
    now = datetime.now(UTC).isoformat()
    demoted = _compute_bitemporal_target_ids(
        ordered_ids, conn, "supersedes", now
    ) | _compute_bitemporal_target_ids(ordered_ids, conn, "corrects", now)
    if not demoted:
        return ordered_ids
    return [eid for eid in ordered_ids if eid not in demoted] + [
        eid for eid in ordered_ids if eid in demoted
    ]


def _resolve_supersession_chains(  # noqa: C901
    conn,
    candidate_ids: list[str],
    where_clauses: list[str],
    params: list,
    max_depth: int = SUPERSESSION_CHAIN_MAX_DEPTH,
) -> dict[str, str]:
    """Part A (multi-hop supersession-chain resolution, plans/scalable-strolling-stallman.md, for
    search_memory's mode="strict"): for each id in candidate_ids, walk the currently-valid
    `supersedes` chain forward (`A supersedes B` => A is the newer/authoritative node, B is the old
    one being replaced) to its live, fully-revalidated terminal head, and return
    {candidate_id: resolved_head_id} for candidates that successfully resolved to a DIFFERENT id.
    A candidate absent from the returned dict either has no live supersessor at all (nothing to
    substitute) or hit an abstain condition below -- both mean "use the original id, unsubstituted"
    to the caller, by design: a depth-capped or cycle-cut path is not safely treated as a terminal
    head, so it must never be silently substituted.

    Batched over the whole candidate pool in a single recursive-CTE round trip (same
    IN (...)-batched idiom as `_apply_type_bias`/`_compute_superseded_ids` above -- not a
    per-candidate loop), bounded to edges actually reachable from this pool within max_depth+1
    hops. The CTE enumerates every reachable (root, hop) edge -- it deliberately does NOT attempt
    to prune at a fork mid-recursion (a SQLite recursive CTE can't safely express "greedily keep
    only the tie-break winner, discard the rest" without a fragile correlated-subquery rewrite);
    instead all candidate edges are returned, and the correctness-critical tie-break/cycle/
    depth-cap/liveness decisions are made by one deterministic Python walk per candidate below,
    over the small in-memory edge set the query returns. This keeps the "one DB round trip,
    batched over the whole pool" property the plan calls for while keeping the actual fork/abstain
    logic auditable and unit-testable in plain Python instead of opaque SQL.

    The SQL recursion bound is `depth <= max_depth` alone -- NOT a path-based cycle guard like
    analyze_lineage/analyze_dependencies' own `NOT LIKE '%'||id||'%'` precedent (a real bug caught
    during test-writing: a path-based SQL guard silently DROPS the row that would reveal a cycle,
    which then looks indistinguishable from "genuinely terminal" to the code below it -- an actual
    two-node A<->B cycle was mis-resolved to a live successor instead of abstaining, before this
    was caught). The depth cap alone still guarantees SQL termination even on a real cycle
    (bounded, repeated re-visits up to max_depth+1 rows), and cycle detection is instead done
    exactly once, correctly, in the Python walk's own `visited` set below -- one source of truth
    for "is this a cycle," not two that could disagree.

    Design decisions pinned down explicitly (per the plan's own callout not to leave these
    implicit):
    - "Currently valid" for the `supersedes` EDGE is evaluated across all four bitemporal columns
      (valid_from/valid_to/valid_at/invalid_at) at one captured `now` for the whole call -- not a
      partial check like analyze_lineage's existing CTE (relation_service.py), which omits
      valid_at.
    - "Live" for a traversed NODE means `entities.status != 'archived'` only -- entities.valid_from/
      valid_to are NOT independently re-checked, because in this codebase they only ever move in
      lockstep with status (store_memory's temporal-upsert and archive_memory both only ever set
      valid_to alongside status='archived'; a live entity's own valid_to is always NULL), so a
      separate check would be redundant, not additive. This matches search_memory's own existing
      liveness precedent (`e.status != 'archived'` in its where_clauses).
    - Tie-break at a fork (two-plus currently-valid edges targeting the same node -- the relations
      table's partial unique index only guarantees uniqueness per (source, target) pair, not per
      target) is the successor's updated_at, then created_at, then id, all descending -- applied
      greedily at EVERY hop of the walk, not just the final target, so a fork mid-chain resolves
      the same deterministic way as a fork at the seed.
    - Cycle, depth-cap breach (a chain that needs more than max_depth hops to terminate), or an
      inaccessible/archived intermediate node anywhere in the chain: abstain on that candidate
      entirely (see module docstring above for what "abstain" means to the caller).
    - The resolved head is re-checked against the ORIGINAL query's own where_clauses/params
      (owner_id/scope, context_id, is_core, memory_type_filter, tags_filter) before being
      returned -- analyze_lineage/analyze_dependencies are unfiltered admin tools, search_memory is
      not, and a resolved head is not necessarily visible to this particular caller.
    """
    if not candidate_ids:
        return {}

    now = datetime.now(UTC).isoformat()
    validity_sql = (
        "(r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?)) AND "
        "(r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?)) AND "
        "(r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?)) AND "
        "(r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))"
    )
    placeholders = ",".join("?" for _ in candidate_ids)
    query = f"""
        WITH RECURSIVE chain(root_id, current_id, next_id, depth) AS (
            SELECT r.target_id, r.target_id, r.source_id, 1
            FROM relations r
            WHERE r.target_id IN ({placeholders}) AND r.predicate = 'supersedes'
              AND {validity_sql}

            UNION ALL

            SELECT c.root_id, c.next_id, r.source_id, c.depth + 1
            FROM relations r
            JOIN chain c ON r.target_id = c.next_id
            WHERE r.predicate = 'supersedes' AND c.depth <= ?
              AND {validity_sql}
        )
        SELECT DISTINCT root_id, current_id, next_id FROM chain
    """
    exec_params = list(candidate_ids) + [now, now, now, now, max_depth, now, now, now, now]
    edge_rows = conn.execute(query, exec_params).fetchall()
    if not edge_rows:
        return {}

    adjacency: dict[str, set] = {}
    touched_ids: set = set(candidate_ids)
    roots_with_edges: set = set()
    for root_id, current_id, next_id in edge_rows:
        adjacency.setdefault(current_id, set()).add(next_id)
        touched_ids.add(current_id)
        touched_ids.add(next_id)
        roots_with_edges.add(root_id)

    entity_placeholders = ",".join("?" for _ in touched_ids)
    entity_rows = conn.execute(
        f"SELECT id, status, updated_at, created_at FROM entities WHERE id IN ({entity_placeholders})",
        list(touched_ids),
    ).fetchall()
    entity_info = {
        row[0]: {"status": row[1], "updated_at": row[2], "created_at": row[3]}
        for row in entity_rows
    }

    def _tie_break(next_ids: set) -> str:
        def key(nid: str):
            info = entity_info.get(nid, {})
            return (info.get("updated_at") or "", info.get("created_at") or "", nid)

        return max(next_ids, key=key)

    _ABSTAIN = object()

    def _walk(root_id: str):
        node = root_id
        visited = {root_id}
        depth = 0
        while True:
            next_ids = adjacency.get(node)
            if not next_ids:
                break  # terminal: node has no further live supersessor
            if depth + 1 > max_depth:
                return _ABSTAIN  # chain continues beyond the allowed cap
            chosen = _tie_break(next_ids)
            info = entity_info.get(chosen)
            if not info or info["status"] == "archived":
                return _ABSTAIN  # inaccessible/archived intermediate (or terminal) node
            if chosen in visited:
                return _ABSTAIN  # cycle
            visited.add(chosen)
            node = chosen
            depth += 1
        return node if node != root_id else None

    resolved: dict[str, str] = {}
    for root_id in roots_with_edges:
        outcome = _walk(root_id)
        if outcome is not None and outcome is not _ABSTAIN:
            resolved[root_id] = outcome

    if not resolved:
        return {}

    # Filter-reapplication: the resolved head must independently satisfy the ORIGINAL query's own
    # where_clauses/params -- a resolved head is not necessarily visible to this particular caller
    # (owner/scope/context/is_core/memory_type/tags filters all still apply).
    heads = set(resolved.values())
    head_placeholders = ",".join("?" for _ in heads)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    passing_rows = conn.execute(
        f"SELECT e.id FROM entities e WHERE e.id IN ({head_placeholders}) AND {where_sql}",
        list(heads) + list(params),
    ).fetchall()
    passing_heads = {row[0] for row in passing_rows}

    return {cid: head for cid, head in resolved.items() if head in passing_heads}


def _substitute_resolved_heads(
    rrf_score_map: dict[str, float], resolved_map: dict[str, str]
) -> dict[str, float]:
    """Part A dedup-merge rule: substitutes every candidate in rrf_score_map that appears in
    resolved_map (from `_resolve_supersession_chains`) with its resolved head, merging colliding
    heads to the MAX score (never sum) -- avoids RRF-score inflation when multiple, otherwise
    unrelated pool entries collapse onto the same live head. Returns a new dict re-sorted by score
    descending (merging can change relative order versus the input). No-op (returns a re-sorted
    copy) when resolved_map is empty.
    """
    substituted: dict[str, float] = {}
    for cid, score in rrf_score_map.items():
        head = resolved_map.get(cid, cid)
        if head in substituted:
            substituted[head] = max(substituted[head], score)
        else:
            substituted[head] = score
    return dict(sorted(substituted.items(), key=lambda kv: -kv[1]))


def _build_candidate_evidence(
    pool_ids: list[str],
    rrf_score_map: dict[str, float],
    fts_rows: list,
    semantic_rows: list[tuple[str, float]],
    topic_scores_map: dict[str, dict],
    resolved_from: dict[str, list[str]],
    predecessor_grounded_map: dict[str, bool] | None = None,
    cross_encoder_scores_map: dict[str, float] | None = None,
    *,
    used_or_fallback: bool = False,
) -> dict[str, dict]:
    """Part B: builds a per-candidate evidence record for every id in pool_ids, consumed by
    `accept_or_abstain` below. Two independent evidence axes are tracked:

    - DIRECT vs INDIRECT provenance (Codex correction to the first plan draft, which conflated
      them):
      - DIRECT: the candidate itself appeared in the FTS and/or semantic retrieval pool -- it has
        its own native rank/score/lexical-match signal (`in_fts`/`in_semantic`/`fts_rank`/
        `semantic_distance`/etc below).
      - INDIRECT: the candidate is a resolved supersession head (present in `resolved_from`) that
        was NOT itself in the original, pre-resolution retrieval pool. It has no native FTS/semantic
        rank of its own -- copying the superseded predecessor's evidence onto it would be unsound
        (the predecessor matched the query; the successor's own relation to the query is
        unverified). This function deliberately leaves an indirect candidate's own direct-evidence
        fields as their natural empty/None/False values; `predecessor_grounded_map` (built by the
        caller from the PRE-resolution pool's own evidence, see search_memory) is threaded through
        instead, so `accept_or_abstain` can require the predecessor's own match to have been strong,
        per the plan's explicit "requiring the predecessor's match to have been strong" option.
        A resolved head CAN also independently appear directly in the pool -- both classes can
        coexist; `provenance` is "direct" whenever there's any native signal at all, "indirect" only
        when there is none.
    - AND-match vs OR-fallback-only, for DIRECT FTS matches (H1 fix): `used_or_fallback` is the
      single pool-level bool `_run_fts_search` returns when `return_fallback_flag=True` -- True iff
      the AND-joined MATCH found nothing and the OR-joined retry is what produced `fts_rows`. When
      True, every id in `fts_rows` is an OR-fallback-only match (`in_fts_or_only`); when False,
      every id in `fts_rows` is a genuine AND match (`in_fts_and`) -- this is a property of how the
      whole query was executed, not a per-row distinction. `in_fts` stays `in_fts_and or
      in_fts_or_only` for any caller that doesn't care about the split (broad/history mode ranking
      keeps using it for recall, unchanged). `dual_channel` is keyed specifically on `in_fts_and`
      (NOT the broader `in_fts`) -- an OR-fallback-only candidate that also happens to land in the
      semantic pool must still go through `accept_or_abstain`'s `in_fts_or_only` topic-grounding
      check, not bypass it via the dual-channel shortcut (this was a real bypass caught during
      review; do not "simplify" `dual_channel` back to `in_fts and in_semantic`).

    `topic_score`/`semantic_verdict` stay optional (None when absent) -- populated only when
    rerank_by_topic actually ran for this call (cost note, Part B): this function must never force
    that expensive Stage-2 pass as a side effect of being called.

    `cross_encoder_score` (roadmap `ba2cf66f` P1#7) is the same optional-field shape: `None`
    unless `use_cross_encoder` actually scored this candidate. Evidence only, this release --
    `accept_or_abstain` does NOT read this field yet (see search_memory's own docstring for why).
    """
    fts_rank = {row[0]: i for i, row in enumerate(fts_rows)}
    fts_bm25 = {row[0]: row[5] for row in fts_rows}
    semantic_rank = {eid: i for i, (eid, _dist) in enumerate(semantic_rows)}
    semantic_distance = {eid: dist for eid, dist in semantic_rows}
    predecessor_grounded_map = predecessor_grounded_map or {}
    cross_encoder_scores_map = cross_encoder_scores_map or {}

    evidence: dict[str, dict] = {}
    for eid in pool_ids:
        in_fts = eid in fts_rank
        in_fts_and = in_fts and not used_or_fallback
        in_fts_or_only = in_fts and used_or_fallback
        in_semantic = eid in semantic_rank
        has_direct_signal = in_fts or in_semantic
        is_resolved_head = eid in resolved_from
        provenance = "direct" if has_direct_signal or not is_resolved_head else "indirect"
        topic = topic_scores_map.get(eid)
        evidence[eid] = {
            "entity_id": eid,
            "provenance": provenance,
            "rrf_score": rrf_score_map.get(eid),
            "in_fts": in_fts,
            "in_fts_and": in_fts_and,
            "in_fts_or_only": in_fts_or_only,
            "fts_rank": fts_rank.get(eid),
            "fts_bm25": fts_bm25.get(eid),
            "in_semantic": in_semantic,
            "semantic_rank": semantic_rank.get(eid),
            "semantic_distance": semantic_distance.get(eid),
            "dual_channel": in_fts_and and in_semantic,
            "topic_score": topic["topic_score"] if topic else None,
            "semantic_verdict": topic["semantic_verdict"] if topic else None,
            "is_resolved_head": is_resolved_head,
            "predecessor_grounded": predecessor_grounded_map.get(eid, False),
            "cross_encoder_score": cross_encoder_scores_map.get(eid),
        }
    return evidence


def accept_or_abstain(evidence: dict, policy: dict | None = None) -> tuple[bool, str]:  # noqa: PLR0911
    """Part B: pure function deciding whether ONE candidate's evidence record clears the
    relevance-abstention gate. Called per-candidate (not just against top-1) by search_memory's
    mode="strict" path; an empty resulting pool after filtering every candidate is the `[]` case
    (SALTMDB memory `c27792a1`). `policy` is accepted for future extension/testability but unused
    today -- this function consumes only the precomputed categorical `semantic_verdict` already
    attached to `evidence` (see `_build_candidate_evidence`); it reads no config.py threshold of
    its own (unlike `_rrf_gap_confident`'s own direct RERANK_GAP_SKIP_RATIO import) -- the
    SAME_SPECIFIC_TOPIC/BROADLY_RELATED_THEMES/DIFFERENT_TOPICS classification and its underlying
    RERANK_SAME_TOPIC_THRESHOLD/RERANK_BROAD_THEME_THRESHOLD live in rerank_candidates_by_topic.

    Acceptance requires a positive grounding signal -- a real, non-phantom match this codebase can
    already produce:

    - DIRECT, dual-channel (in_fts_and AND in_semantic): always accepted -- the strongest, already-
      established signal shape (`_rrf_gap_confident`'s own precondition for even considering a
      confident top-1). Deliberately keyed on `in_fts_and`, not the broader `in_fts` -- an
      OR-fallback-only match that also happens to land in the semantic pool must NOT take this
      shortcut; it goes through the `in_fts_or_only` rule below instead (H1 fix; this is the exact
      bypass an earlier revision of this gate had).
    - DIRECT, true FTS AND-match (in_fts_and, not in_semantic): accepted -- a genuine AND-joined
      `entities_fts MATCH` is a real term match, not a nearest-neighbor phantom, and its
      correctness doesn't degrade as the corpus grows.
    - DIRECT, FTS OR-fallback-only (in_fts_or_only -- present only because the AND-joined query
      found nothing and `_run_fts_search` silently retried with an OR-joined query), REGARDLESS of
      whether it also happens to be `in_semantic`: accepted ONLY if `semantic_verdict` is exactly
      "SAME_SPECIFIC_TOPIC", the same bar as the semantic-only rule below. An OR-fallback match is
      an incidental single-term hit, not a corroborated match on its own -- unconditionally
      accepting it (as an earlier revision of this gate did, via the old broad `in_fts` check) is
      exactly the false-accept mechanism SALTMDB memory `6ee96334` traced 10/10 replayed
      negative-control queries to.
    - DIRECT, semantic-only (in_semantic, neither in_fts_and nor in_fts_or_only): accepted ONLY if
      `semantic_verdict` is exactly "SAME_SPECIFIC_TOPIC" (reusing RERANK_SAME_TOPIC_THRESHOLD
      as-is, not a new constant -- see the "why not a raw distance cutoff" note below).
      "BROADLY_RELATED_THEMES" or no topic_score at all is NOT sufficient on its own.
    - INDIRECT (resolved supersession head absent from the original pool): has no native signal of
      its own to trust. Accepted only if `predecessor_grounded` is True -- the original,
      pre-resolution candidate that resolved to this head independently cleared the DUAL_CHANNEL,
      true-AND FTS_MATCH, or (since the H1 fix) the OR-fallback-plus-SAME_SPECIFIC_TOPIC rule above
      (NOT the semantic-only rule; see search_memory's predecessor-evidence construction). An
      OR-fallback-only predecessor's `predecessor_grounded` value can therefore itself depend on an
      on-demand topic-verdict lookup performed over the pre-resolution pool before this map is
      built -- not only on `dual_channel`/`in_fts_and` signals, as an earlier revision of this
      docstring implied. This is deliberately the ONLY condition (an earlier version of this function also required
      the head's own semantic_verdict not be "DIFFERENT_TOPICS" -- reverted: the on-demand
      topic-grounding lookup that powers the DIRECT semantic-only rule above assigns EVERY
      FTS-less candidate some verdict, including a default "DIFFERENT_TOPICS" for a resolved head
      with no embedding data at all to score -- indistinguishable in the data from "genuinely
      off-topic," so it was vetoing legitimately-grounded resolved heads that simply had no
      chunk/entity embedding yet. Plan section B explicitly frames "predecessor was strong" as a
      sufficient condition on its own, not one that must also be combined with the head's own
      weak/absent signal).
    - No evidence at all (neither direct nor a grounded indirect path): abstain.

    Why not a raw semantic_distance cutoff (what an earlier version of this function did): empirically
    disproven during implementation verification. Measured live against the 21k-entity diverse
    test corpus (scratch/diverse_corpus_full.db), a genuinely unrelated/nonsense query's nearest
    entity-embedding neighbor routinely lands at cosine distance 0.22-0.34 -- fully overlapping the
    0.2152-0.3677 range measured for HAND-VERIFIED GENUINE semantic paraphrase matches in a small
    control corpus. A single whole-document embedding vector's absolute distance to an unrelated
    query shrinks as the candidate pool grows (more documents means a better chance some unrelated
    one is coincidentally "close"), so a fixed distance floor that looks well-calibrated on a small
    corpus silently stops discriminating at real scale -- it is not a fixable-by-retuning problem,
    it is the wrong signal shape. A rank/margin-based check over the same raw vectors was tried
    next and also failed: genuine and nonsense queries showed statistically indistinguishable
    rank-1-vs-rank-2 distance gaps against the full corpus (both types of query land in a densely
    clustered neighborhood of similar-distance candidates). Chunk-level topic_score
    (rerank_candidates_by_topic) was tried third and is what this function actually uses --
    but even that alone is NOT precise enough to cleanly separate the two classes at the
    "BROADLY_RELATED_THEMES" tier (both genuine and nonsense queries commonly land there); only
    the stricter, already-calibrated "SAME_SPECIFIC_TOPIC" tier reliably excludes the nonsense
    class in live testing, at the accepted cost of also abstaining on some genuine but only
    loosely/broadly-paraphrased semantic-only matches -- matching this codebase's explicit,
    stated risk asymmetry (0% false-accept is the hard target; a nonzero false-reject rate on
    weak, uncorroborated matches is accepted, not hidden) and directly implementing design memory
    `b9b75764`'s "treat weak vector-only proximity as insufficient."
    """
    if evidence.get("provenance") == "indirect":
        if not evidence.get("predecessor_grounded"):
            return False, "indirect_ungrounded_predecessor"
        return True, "indirect_grounded_predecessor"

    if evidence.get("dual_channel"):
        return True, "dual_channel"
    if evidence.get("in_fts_and"):
        return True, "fts_match"
    if evidence.get("in_fts_or_only"):
        if evidence.get("semantic_verdict") == "SAME_SPECIFIC_TOPIC":
            return True, "fts_or_fallback_same_specific_topic"
        return False, "fts_or_fallback_insufficient_topic_grounding"
    if evidence.get("in_semantic"):
        if evidence.get("semantic_verdict") == "SAME_SPECIFIC_TOPIC":
            return True, "semantic_only_same_specific_topic"
        return False, "semantic_only_insufficient_topic_grounding"
    return False, "no_evidence"


def search_memory(  # noqa: C901, PLR0912, PLR0915
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    context_id: str = None,
    is_core: bool = None,
    memory_type_filter: Literal["fact", "event", "procedure", "decision", "preference"] = None,
    tag_operator: Literal["AND", "OR"] = "AND",
    cursor: str = None,
    include_related: bool = True,
    rerank_by_topic: bool = False,
    prefer_durable_types: bool = True,
    demote_superseded: bool = True,
    use_cross_encoder: bool = False,
    mode: Literal["strict", "broad", "history"] = "broad",
    disable_semantic: bool = False,
    db_connection=None,
    db_path: str = None,
) -> list | dict:
    """Performs full-text keyword search and filtering in long-term memory.

    disable_semantic (default False; Track B, scratch/plans/track_b_daemon_detailed.md §14): a
    per-call override forcing the FTS-only path for this one request, regardless of the
    SALTMDB_ENABLE_SEMANTIC env var -- added because a persistent daemon reads its environment
    once at its own startup and holds it fixed, so a caller-side env mutation (as
    `cmd_bootstrap_digest`'s `--no-semantic` flag used to do) has no effect on an already-running
    daemon. Evaluated fresh on every call, never a global/env mutation, since the daemon is
    multi-threaded and a shared mutable flag would race concurrent calls. Governs only this
    function's own semantic-search gate below -- `check_duplicate_memories`'s separate
    `is_semantic_search_enabled()` call site is unrelated and unaffected.

    use_cross_encoder (opt-in, default False; roadmap `ba2cf66f` P1#7, design `1fddc04a`/
    `8115fa4a`): an independent Stage-2 reordering alternative to `rerank_by_topic`, NOT a
    dependency of it -- either flag alone triggers pool widening and shares the same
    `_rrf_gap_confident` gap-gate (a decisive, dual-channel-corroborated hybrid winner skips BOTH
    Stage-2 mechanisms, not just the topic one). Scores the widened pool with an optional ONNX
    cross-encoder (`reranker_service.score_pairs`, feature-flagged via `SALTMDB_RERANKER_MODEL`,
    no PyTorch runtime) and full-overrides ordering by score, same shape as `rerank_by_topic`'s own
    full-override reorder. If BOTH flags are set and neither is gap-gated off, cross-encoder runs
    SECOND and its ordering wins (it's the more precise, more expensive stage) -- `topic_score`
    still stays attached to the result item alongside `cross_encoder_score`, cross-encoder never
    erases it. Deterministic fallback: disabled feature, unsupported/missing model, or any runner
    failure leaves `ranked_pool_` exactly as it was before this stage -- no exception, no widened
    result count. Cross-encoder scores are attached to `accept_or_abstain`'s evidence dict as an
    inert `cross_encoder_score` field this release -- they do NOT affect the accept/reject decision
    yet (that requires its own future, separately-calibrated gate rule, not an uncalibrated one
    invented here).

    mode (opt-in, default "broad" -- today's exact pre-existing behavior, unchanged): Part C of
    plans/scalable-strolling-stallman.md (SALTMDB memory `9c199005`).
    - "broad": no chain resolution, no relevance gate, no `is_superseded` tagging. Identical to
      this function's behavior before mode existed.
    - "strict": matched-but-superseded candidates are resolved and SUBSTITUTED with their live,
      multi-hop-revalidated `supersedes` successor (Part A); every surviving candidate must then
      independently clear a calibrated relevance-abstention gate (Part B) or is dropped. An empty
      result (`[]`) is a normal, successful outcome for a query with no sufficiently-grounded
      match -- not an error. Widens the candidate pool the same way rerank_by_topic/
      prefer_durable_types/demote_superseded already do, and retries with a larger pool (up to
      STRICT_OVERFETCH_CANDIDATE_CAP) when resolution/dedup/the gate shrink the post-policy pool
      below what `offset`+`limit` needs (Part C2 -- pagination continuity across a rejection,
      substitution, or many-to-one dedup collapse). Additionally, unconditionally and regardless
      of the `prefer_durable_types`/`demote_superseded` flags above: durable-type preference is
      always applied, and a surviving candidate is demoted (never excluded -- it already cleared
      the gate on its own evidence) if it's still the target of a currently-valid `supersedes`
      edge Part A's resolver couldn't cleanly resolve (cycle/depth-cap/archived-intermediate
      abstain) or of a currently-valid `corrects` edge (roadmap `ba2cf66f` P1#6, design
      `1fddc04a`; `_apply_strict_ranking_defaults`).
    - "history": no resolution, no gate -- every live candidate the hybrid pipeline would
      otherwise return stays visible exactly as in "broad", except a candidate that is the target
      of a currently-valid `supersedes` edge is additionally tagged `"is_superseded": true` in its
      result item. Does NOT relax entity-status visibility: every mode still starts from
      `e.status != 'archived'`; "history" only stops live-but-superseded memories from being
      silently hidden, it never exposes archived material.
    `mode` only applies to the query-keyword-based hybrid pipeline -- it has no effect on
    `explain_mode` (which returns before retrieval) or on empty-query filter/tag-only browsing
    (there is no retrieval evidence to gate or chain to resolve there).
    """
    if mode not in ("strict", "broad", "history"):
        logger.warning("search_memory: unknown mode=%r, falling back to 'broad'.", mode)
        mode = "broad"

    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    offset = 0
    if cursor and cursor.startswith("offset:"):
        try:
            offset = int(cursor.split(":")[1])
        except ValueError:
            pass

    try:
        where_clauses = ["e.status != 'archived'"]
        params: list[Any] = []  # mixed str/int SQL bind values (e.g. is_core -> 0/1)

        if owner_id:
            where_clauses.append("(e.owner_id = ? OR e.scope = 'shared')")
            params.append(owner_id)

        if context_id:
            where_clauses.append(
                "(e.context_id = ? OR json_extract(e.metadata, '$.project') = ? OR json_extract(e.metadata, '$.project_id') = ?)"
            )
            params.extend([context_id, context_id, context_id])

        if is_core is not None:
            where_clauses.append("e.is_core = ?")
            params.append(1 if is_core else 0)

        if memory_type_filter is not None:
            where_clauses.append("e.memory_type = ?")
            params.append(memory_type_filter)

        if metadata_filter and isinstance(metadata_filter, dict):
            for mk, mv in metadata_filter.items():
                where_clauses.append(f"json_extract(e.metadata, '$.{mk}') = ?")
                params.append(str(mv))

        if tags_filter:
            norm_tags = [normalize_tag_name(t) for t in tags_filter if t.strip()]
            if norm_tags:
                tag_groups = []
                for tname in norm_tags:
                    grp = set()
                    c = conn.execute(
                        "SELECT id, canonical_id FROM tags WHERE lower(name) = lower(?)", (tname,)
                    )
                    for tid, tcanon in c.fetchall():
                        grp.add(tid)
                        main_id = tcanon if tcanon else tid
                        grp.add(main_id)
                        alias_c = conn.execute(
                            "SELECT id FROM tags WHERE canonical_id = ?", (main_id,)
                        )
                        for ar in alias_c.fetchall():
                            grp.add(ar[0])
                    tag_groups.append((tname, grp))

                if tag_operator == "AND":
                    for tname, grp in tag_groups:
                        if grp:
                            placeholders = ",".join("?" for _ in grp)
                            where_clauses.append(
                                f"e.id IN (SELECT et.entity_id FROM entity_tags et WHERE et.tag_id IN ({placeholders}))"
                            )
                            params.extend(list(grp))
                        else:
                            where_clauses.append(
                                "e.id IN (SELECT et.entity_id FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE lower(t.name) = lower(?))"
                            )
                            params.append(tname)
                else:
                    all_ids = set()
                    missing_tnames = []
                    for tname, grp in tag_groups:
                        if grp:
                            all_ids.update(grp)
                        else:
                            missing_tnames.append(tname)

                    sub_clauses = []
                    sub_params = []
                    if all_ids:
                        placeholders = ",".join("?" for _ in all_ids)
                        sub_clauses.append(f"et.tag_id IN ({placeholders})")
                        sub_params.extend(list(all_ids))
                    if missing_tnames:
                        placeholders = ",".join("?" for _ in missing_tnames)
                        sub_clauses.append(
                            f"lower(t.name) IN ({','.join('lower(?)' for _ in missing_tnames)})"
                        )
                        sub_params.extend(missing_tnames)

                    if sub_clauses:
                        where_clauses.append(
                            f"e.id IN (SELECT et.entity_id FROM entity_tags et LEFT JOIN tags t ON et.tag_id = t.id WHERE {' OR '.join(sub_clauses)})"
                        )
                        params.extend(sub_params)

        sanitized_query = sanitize_fts_query(query_keywords) if query_keywords else ""

        if explain_mode:
            if rerank_by_topic:
                logger.debug("rerank_by_topic ignored: explain_mode takes precedence.")
            if use_cross_encoder:
                logger.debug("use_cross_encoder ignored: explain_mode takes precedence.")
            if mode != "broad":
                logger.debug("mode=%r ignored: explain_mode takes precedence.", mode)
            terms = sanitized_query.split() if sanitized_query else []
            searched_terms = {}
            for t in terms:
                c = conn.execute(
                    "SELECT 1 FROM entities_fts WHERE entities_fts MATCH ?", (f'"{t}"*',)
                ).fetchone()
                searched_terms[t] = bool(c)

            invalid_tags = []
            if tags_filter:
                for tf in tags_filter:
                    tname = normalize_tag_name(tf)
                    c = conn.execute(
                        "SELECT 1 FROM tags WHERE lower(name) = lower(?)", (tname,)
                    ).fetchone()
                    if not c:
                        invalid_tags.append(tf)

            return {
                "explain": {
                    "searched_terms_found": searched_terms,
                    "invalid_tags_suggestions": invalid_tags,
                    "sanitized_query": sanitized_query,
                    "where_clauses": where_clauses,
                }
            }

        rows: list[Any] = []
        # Populated only when rerank_by_topic actually runs (Part B); left empty on every other
        # path, including rerank_by_topic=True requests that get gated off below -- the result-
        # item assembly loop attaches topic_score/semantic_verdict only for ids present here, so
        # an empty map here is exactly what keeps unreranked results free of those keys.
        topic_scores_map: dict[str, dict] = {}
        # Same empty-unless-actually-scored shape as topic_scores_map above, for use_cross_encoder
        # (roadmap ba2cf66f P1#7).
        cross_encoder_scores_map: dict[str, float] = {}
        # Populated only under mode="history" -- ids in the final pool that are the target of a
        # currently-valid `supersedes` edge, tagged (not hidden/reordered) in the result item.
        superseded_ids: set = set()
        if sanitized_query:
            assert query_keywords  # nosec B101 -- mypy narrowing only, not a runtime safety check
            from saltmdb.config import is_semantic_search_enabled

            if is_semantic_search_enabled() and not disable_semantic:
                if not db_path:
                    db_path = get_db_path()

                def _compute_pool(candidate_window: int) -> dict:  # noqa: C901, PLR0912, PLR0915
                    """One full FTS+semantic+RRF-fuse+[resolve+substitute]+[rerank]+[gate]+
                    [ranking-flags] pass at a given candidate_window size (Part C pipeline
                    ordering: RRF fusion -> gap-gate check off the ORIGINAL un-substituted sets ->
                    chain-resolution/substitution (mode="strict") -> [rerank_by_topic, if
                    requested and not gap-confident] -> accept_or_abstain filter over the full
                    widened pool (mode="strict"), before offset/limit slicing -> mark superseded
                    (mode="history") -> prefer_durable_types -> demote_superseded). Returns the
                    final ordered candidate id list (not yet offset/limit-sliced) plus enough
                    metadata for the mode="strict" overfetch retry loop below (Part C2) to decide
                    whether widening further could help, and for row assembly afterward.
                    """
                    fts_rows_, used_or_fallback_ = _run_fts_search(
                        conn,
                        sanitized_query,
                        where_clauses,
                        params,
                        candidate_window,
                        0,
                        return_fallback_flag=True,
                    )
                    semantic_rows_ = semantic_search(
                        query_keywords, where_clauses, params, candidate_window, db_path, 0
                    )

                    rrf_map = reciprocal_rank_fusion(fts_rows_, semantic_rows_, candidate_window)
                    fts_ids_ = {r[0] for r in fts_rows_}
                    # True-AND-only id set (H1 fix): empty whenever used_or_fallback_ is True,
                    # since every row in fts_rows_ then came from the OR-joined retry, not a
                    # genuine AND match -- see _run_fts_search's own docstring for why this is a
                    # single pool-level bool, not a per-row property.
                    fts_and_ids_ = fts_ids_ if not used_or_fallback_ else set()
                    semantic_ids_ = {eid for eid, _ in semantic_rows_}

                    resolved_from_: dict[str, list[str]] = {}
                    predecessor_grounded_map: dict[str, bool] = {}
                    if rrf_map and mode == "strict":
                        # Part A: resolve off the ORIGINAL, un-substituted pool -- gap-gate below
                        # also reads fts_ids_/semantic_ids_ pre-substitution, same invariant.
                        pre_pool_ids = list(rrf_map.keys())
                        resolved_map = _resolve_supersession_chains(
                            conn, pre_pool_ids, where_clauses, params
                        )
                        if resolved_map:
                            # H1 predecessor-grounding fix: on-demand topic-score exactly the
                            # OR-fallback-only subset of pre_pool_ids (true-AND pre-pool candidates
                            # already have a sufficient DIRECT signal and skip this lookup) BEFORE
                            # building pre_evidence -- otherwise an OR-fallback-only predecessor can
                            # never get a semantic_verdict at all, silently always failing the
                            # in_fts_or_only rule below regardless of its real topic relevance.
                            pre_or_fallback_only_ids = (
                                [eid for eid in pre_pool_ids if eid in fts_ids_]
                                if used_or_fallback_
                                else []
                            )
                            pre_topic_scores_map = (
                                _score_topics_with_fallback(
                                    query_keywords, pre_or_fallback_only_ids, db_path
                                )
                                if pre_or_fallback_only_ids
                                else {}
                            )
                            pre_evidence = _build_candidate_evidence(
                                pre_pool_ids,
                                rrf_map,
                                fts_rows_,
                                semantic_rows_,
                                pre_topic_scores_map,
                                {},
                                used_or_fallback=used_or_fallback_,
                            )
                            for cid, head in resolved_map.items():
                                resolved_from_.setdefault(head, []).append(cid)
                            for head, preds in resolved_from_.items():
                                predecessor_grounded_map[head] = any(
                                    accept_or_abstain(pre_evidence[p])[0]
                                    for p in preds
                                    if p in pre_evidence
                                )
                            rrf_map = _substitute_resolved_heads(rrf_map, resolved_map)

                    topic_scores_map_: dict[str, dict] = {}
                    cross_encoder_scores_map_: dict[str, float] = {}
                    if rrf_map:
                        # Part 1 gap gate (SALTMDB memory 870a1d4e): skip Stage 2 entirely when
                        # hybrid search already has a decisive, dual-channel-corroborated winner.
                        # Deliberately checked against the pre-substitution fts_ids_/semantic_ids_
                        # sets (a resolved head's own channel membership is a separate, Part B
                        # evidence question, not this gate's). Shared by BOTH Stage-2 mechanisms
                        # (rerank_by_topic and use_cross_encoder, roadmap ba2cf66f P1#7) -- the
                        # gate's premise ("hybrid already has a decisive winner, Stage-2 has
                        # nothing to add") doesn't depend on which Stage-2 implementation would
                        # have run.
                        gap_confident = (
                            rerank_by_topic or use_cross_encoder
                        ) and _rrf_gap_confident(rrf_map, fts_ids_, semantic_ids_)
                        if gap_confident:
                            logger.debug(
                                "Stage-2 rerank skipped: RRF top1/top2 gap already decisive "
                                "(dual-channel top1, ratio >= RERANK_GAP_SKIP_RATIO)."
                            )
                        if rerank_by_topic and not gap_confident:
                            # Full widened pool, not yet offset/limit-sliced -- Stage 2 reranks
                            # the whole candidate_window, then offset/limit slices the reranked
                            # order.
                            pool_ids = list(rrf_map.keys())
                            topic_scores_map_ = _score_topics_with_fallback(
                                query_keywords, pool_ids, db_path
                            )
                            # Full-override semantics (per spec): reranked order is sorted purely
                            # by topic_score, replacing RRF order for Stage 2 -- not a blend.
                            ranked_pool_ = sorted(
                                pool_ids, key=lambda eid: -topic_scores_map_[eid]["topic_score"]
                            )
                        else:
                            ranked_pool_ = list(rrf_map.keys())
                            if mode == "strict":
                                # accept_or_abstain's DIRECT semantic-only rule (Part B) needs a
                                # calibrated topic_verdict, not a raw distance (see its own
                                # docstring for why a distance/margin cutoff was tried and
                                # empirically rejected) -- compute it on demand, WITHOUT
                                # reordering the pool (full-pool reordering is rerank_by_topic's
                                # own separate opt-in, untouched here), and only for candidates
                                # that actually need it: those lacking a genuine FTS AND-match
                                # (fts_and_ids_, NOT the broader fts_ids_ -- H1 fix). An
                                # OR-fallback-only candidate IS included here and DOES get a
                                # semantic_verdict computed, so accept_or_abstain's in_fts_or_only
                                # rule can actually be satisfied; only true-AND/dual-channel
                                # candidates already have a sufficient DIRECT signal and skip this
                                # lookup, keeping the added cost bounded.
                                ungrounded_ids = [
                                    eid for eid in ranked_pool_ if eid not in fts_and_ids_
                                ]
                                topic_scores_map_ = _score_topics_with_fallback(
                                    query_keywords, ungrounded_ids, db_path
                                )

                        if use_cross_encoder and not gap_confident and ranked_pool_:
                            # Independent Stage-2 alternative to rerank_by_topic (roadmap
                            # ba2cf66f P1#7) -- NOT a dependency of it. If rerank_by_topic already
                            # reordered ranked_pool_ above this pass, cross-encoder runs on top of
                            # that order and its own reorder wins (last-write-wins on final
                            # position): it's the more precise, more expensive stage, so a caller
                            # opting into both gets its final say on ordering. topic_score stays
                            # attached to the result item regardless -- cross-encoder never erases
                            # it, it only adds cross_encoder_score alongside it.
                            from saltmdb.config import CROSS_ENCODER_MAX_CANDIDATES
                            from saltmdb.domain.services import reranker_service

                            ce_scores = None
                            if reranker_service.is_cross_encoder_enabled():
                                # Skip the batch fetch entirely when disabled/misconfigured --
                                # score_pairs would return None anyway, no point paying for the
                                # SQL round-trip first. Cap the pool to CROSS_ENCODER_MAX_CANDIDATES
                                # BEFORE the fetch (Codex full-diff review finding), not after --
                                # mode="strict"'s overfetch retry loop can widen ranked_pool_ up to
                                # STRICT_OVERFETCH_CANDIDATE_CAP (200), and fetching every one of
                                # those full documents just to discard all but the first 10 would
                                # be a pointless, potentially large, unbounded-with-corpus-growth
                                # SQL read for no benefit -- score_pairs itself caps input length
                                # regardless, so nothing downstream needs the wider fetch.
                                ce_pool_ids = list(ranked_pool_)[:CROSS_ENCODER_MAX_CANDIDATES]
                                placeholders_ce = ",".join("?" for _ in ce_pool_ids)
                                ce_rows = conn.execute(
                                    f"SELECT id, title, full_content FROM entities WHERE id IN ({placeholders_ce})",
                                    ce_pool_ids,
                                ).fetchall()
                                ce_text_by_id = {row[0]: f"{row[1]}\n\n{row[2]}" for row in ce_rows}

                                scored_ids_in_order = [
                                    eid for eid in ce_pool_ids if eid in ce_text_by_id
                                ]
                                ce_texts = [ce_text_by_id[eid] for eid in scored_ids_in_order]
                                ce_scores = reranker_service.score_pairs(query_keywords, ce_texts)
                            if ce_scores is not None:
                                cross_encoder_scores_map_ = dict(
                                    zip(scored_ids_in_order, ce_scores)
                                )
                                reordered = sorted(
                                    scored_ids_in_order,
                                    key=lambda eid: -cross_encoder_scores_map_[eid],
                                )
                                unscored_tail = [
                                    eid
                                    for eid in ranked_pool_
                                    if eid not in cross_encoder_scores_map_
                                ]
                                ranked_pool_ = reordered + unscored_tail
                            # ce_scores is None (disabled/unsupported model/runner failure/
                            # malformed output): ranked_pool_ is left exactly as it was before this
                            # block -- deterministic fallback to current behavior, no widening.

                        superseded_ids_: set = set()
                        if mode == "strict":
                            evidence_map = _build_candidate_evidence(
                                ranked_pool_,
                                rrf_map,
                                fts_rows_,
                                semantic_rows_,
                                topic_scores_map_,
                                resolved_from_,
                                predecessor_grounded_map,
                                cross_encoder_scores_map_,
                                used_or_fallback=used_or_fallback_,
                            )
                            accepted_pool = []
                            for eid in ranked_pool_:
                                ok, reason = accept_or_abstain(evidence_map[eid])
                                logger.debug(
                                    "search_memory strict gate: %s -> accept=%s (%s)",
                                    eid,
                                    ok,
                                    reason,
                                )
                                if ok:
                                    accepted_pool.append(eid)
                            ranked_pool_ = accepted_pool
                        elif mode == "history":
                            superseded_ids_ = _compute_superseded_ids_bitemporal(ranked_pool_, conn)

                        # Part 2 (SALTMDB memory 870a1d4e): type bias first, then supersession
                        # demotion -- ensures an explicitly-superseded item always sinks below a
                        # merely-event-typed one, not the reverse. Applied to the FULL pool,
                        # before the offset/limit slice.
                        if prefer_durable_types:
                            ranked_pool_ = _apply_type_bias(ranked_pool_, conn)
                        if demote_superseded:
                            ranked_pool_ = _apply_supersession_demotion(ranked_pool_, conn)
                        # Roadmap ba2cf66f P1#6 / design 1fddc04a: durable-type preference and a
                        # supersession/correction safety-net demotion are forced, unconditional
                        # defaults under mode="strict", independent of the two independently-togglable flags above
                        # (which keep their existing, narrower, mode-agnostic meaning and may have
                        # already run a second time here -- harmless, a stable partition on the
                        # same criterion applied twice is a no-op the second time). broad/history
                        # are completely unreached by this branch -- their pre-existing behavior is
                        # byte-identical, unaffected by this addition.
                        if mode == "strict":
                            ranked_pool_ = _apply_strict_ranking_defaults(ranked_pool_, conn)
                    else:
                        ranked_pool_ = []
                        superseded_ids_ = set()

                    # Both raw channels returned fewer rows than requested -> the underlying
                    # corpus is exhausted for this query at this window size; growing
                    # candidate_window further cannot reveal more candidates (Part C2).
                    exhausted_ = (
                        len(fts_rows_) < candidate_window and len(semantic_rows_) < candidate_window
                    )
                    return {
                        "ordered_ids": ranked_pool_,
                        "fts_rows": fts_rows_,
                        "topic_scores_map": topic_scores_map_,
                        "cross_encoder_scores_map": cross_encoder_scores_map_,
                        "superseded_ids": superseded_ids_,
                        "exhausted": exhausted_,
                        # Post-substitution RRF fusion scores (Part A dedup-merge already applied
                        # by _substitute_resolved_heads when mode="strict") -- used for the result
                        # item's own "score" field below, same as before this refactor: the
                        # assembled item's score is always the RRF fusion score, even when
                        # rerank_by_topic's topic_score reordered `ordered_ids` (topic_score is
                        # attached separately, it never replaces this field).
                        "rrf_score_map": rrf_map,
                    }

                if (
                    rerank_by_topic
                    or use_cross_encoder
                    or prefer_durable_types
                    or demote_superseded
                    or mode == "strict"
                ):
                    # Widen the pool for rerank_by_topic, use_cross_encoder (roadmap ba2cf66f
                    # P1#7), Part 2's two independently-togglable ranking flags, AND mode="strict" (Part B
                    # pool-widening requirement) -- otherwise there's nothing meaningful to
                    # reorder/resolve/gate within (a plain search's pool is just offset+limit,
                    # often smaller than what's worth considering).
                    from saltmdb.config import RERANK_CANDIDATE_POOL_SIZE

                    base_window = max(offset + limit, RERANK_CANDIDATE_POOL_SIZE)
                else:
                    base_window = offset + limit

                if mode == "strict":
                    # Part C2 pagination redesign: resolution/dedup/the relevance gate can all
                    # shrink the raw candidate_window's survivor count below offset+limit even
                    # after the widening above. Re-run the WHOLE pass (from scratch, same
                    # candidate_window semantics as every other mode -- see _run_fts_search's own
                    # LIMIT candidate_window OFFSET 0, then this function's own final
                    # `[offset:offset+limit]` Python slice below) with a doubled window until
                    # enough survivors exist, the underlying corpus is exhausted, or the cap is
                    # hit. Because every pass recomputes the full pool deterministically from
                    # scratch (not an incremental DB offset), a later cursor call with a larger
                    # `offset` reproduces a stable superset of this same computation -- cursor
                    # continuity across a rejection/substitution/dedup collapse holds as long as
                    # the underlying corpus doesn't change between calls, exactly like this
                    # function's pre-existing offset:N cursor already assumed for every other mode.
                    # No early "no-progress" stop here (Codex review P1 finding, correctly
                    # rejected during re-review): an earlier version broke out after a single
                    # doubling found zero additional accepted survivors, on the theory that a
                    # genuinely relevant query should keep surfacing more matches as the window
                    # grows. That's false in general -- real accepted candidates can legitimately
                    # sit beyond the NEXT window too (e.g. ranks 41-80 when the window just grew
                    # from 20 to 40), so that guard could return a prematurely short/empty page for
                    # a genuinely satisfiable query. The plan's own spec is exactly "grow until
                    # enough survivors, exhaustion, or cap" -- STRICT_OVERFETCH_CANDIDATE_CAP is
                    # already the sole, deliberate safety valve on how far this is allowed to
                    # search (see its own config.py docstring); a nonsense query scanning further
                    # toward that cap in search of a real match is the accepted, documented
                    # trade-off (see accept_or_abstain's docstring and
                    # run_relevance_gate_holdout.py's held-out cases), not a bug to work around
                    # with a second, undocumented early-exit heuristic.
                    # Clamp the STARTING window to the cap too (Codex review round-2 P2 finding):
                    # base_window is max(offset+limit, RERANK_CANDIDATE_POOL_SIZE), which can
                    # itself already exceed STRICT_OVERFETCH_CANDIDATE_CAP for a large `limit` or a
                    # deep `offset` cursor -- the loop's own `window < CAP` condition only guards
                    # the DOUBLING step, not this initial value, so without this clamp the very
                    # first _compute_pool() call could silently run past the "absolute cap" the
                    # config/docs promise.
                    window = min(base_window, STRICT_OVERFETCH_CANDIDATE_CAP)
                    pool_result = _compute_pool(window)
                    while (
                        len(pool_result["ordered_ids"]) < offset + limit
                        and not pool_result["exhausted"]
                        and window < STRICT_OVERFETCH_CANDIDATE_CAP
                    ):
                        window = min(window * 2, STRICT_OVERFETCH_CANDIDATE_CAP)
                        pool_result = _compute_pool(window)
                else:
                    pool_result = _compute_pool(base_window)

                fts_rows = pool_result["fts_rows"]
                topic_scores_map = pool_result["topic_scores_map"]
                cross_encoder_scores_map = pool_result["cross_encoder_scores_map"]
                superseded_ids = pool_result["superseded_ids"]
                rrf_score_map = pool_result["rrf_score_map"]
                merged_ids = pool_result["ordered_ids"][offset : offset + limit]

                if merged_ids:
                    placeholders = ",".join("?" for _ in merged_ids)
                    id_order = {eid: i for i, eid in enumerate(merged_ids)}
                    fetch_sql = f"""
                        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                               0.0 as rank_score,
                               e.created_at, e.updated_at, e.owner_id, e.scope,
                               e.metadata, e.context_id, e.memory_type, 0 as rel_count,
                               NULL as fts_snippet
                        FROM entities e
                        WHERE e.id IN ({placeholders})
                    """
                    fetched = conn.execute(fetch_sql, merged_ids).fetchall()
                    sorted_fetched = sorted(fetched, key=lambda r: id_order.get(r[0], 9999))
                    # Rows that matched via FTS5 already carry a real query-centered excerpt in
                    # fts_rows (computed in the same query as bm25()); rows that only surfaced via
                    # semantic_search() never went through entities_fts MATCH at all, so they keep
                    # fts_snippet = None here and fall back to the heuristic extractor below.
                    fts_snippet_map = {row[0]: row[-1] for row in fts_rows if row[-1]}
                    rows = []
                    for r in sorted_fetched:
                        r_list = list(r)
                        r_list[5] = rrf_score_map.get(r[0], 0.0)
                        r_list[-1] = fts_snippet_map.get(r[0])
                        rows.append(r_list)
                else:
                    rows = []
            else:
                # Part 0 (SALTMDB memory 870a1d4e follow-on): FTS-only query retrieval is
                # retired, not silently substituted -- search_memory is a hybrid FTS+dense-vector
                # tool, and returning lower-quality FTS-only results as if nothing changed hid a
                # real precision regression from the caller. Fails loud via the function's own
                # existing except-Exception handler below instead. Empty-query browsing (no
                # query_keywords, the final `else` further down) is unaffected -- it never reaches
                # this branch.
                raise RuntimeError(
                    "Semantic search is disabled (SALTMDB_ENABLE_SEMANTIC=false); search_memory "
                    "requires the hybrid FTS+dense-vector pipeline for query-based search. Unset "
                    "SALTMDB_ENABLE_SEMANTIC (or set it to true), or call search_memory without "
                    "query_keywords to browse via tags/filters only."
                )
        else:
            if rerank_by_topic:
                logger.debug("rerank_by_topic ignored: query_keywords is empty.")
            if use_cross_encoder:
                logger.debug("use_cross_encoder ignored: query_keywords is empty.")
            sql = f"""
                SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                       0.0 as rank_score,
                       e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id,
                       e.memory_type, 0 as rel_count, NULL as fts_snippet
                FROM entities e
                WHERE {" AND ".join(where_clauses)}
                ORDER BY e.is_core DESC, e.updated_at DESC
                LIMIT ? OFFSET ?
            """
            exec_params = params + [limit, offset]
            cursor_obj = conn.execute(sql, exec_params)
            rows = cursor_obj.fetchall()

        # Batch-fetch all related entities in a single query to avoid N+1. Ordering invariant:
        # this always runs on the FINAL rows/merged_ids-derived set -- Part B's candidate-window
        # widening and Stage-2 rerank both happen strictly before merged_ids is computed above, so
        # the wider pre-rerank pool never reaches this step. A future refactor that moves the
        # widening later could break this silently -- keep it upstream of this block.
        related_map: dict[str, list[Any]] = {}  # {entity_id: [related items]}
        if include_related and rows:
            all_eids = [r[0] for r in rows]
            placeholders_r = ",".join("?" for _ in all_eids)
            batch_rel_cursor = conn.execute(
                f"""
                SELECT r.source_id, r.target_id, r.predicate, e.id, e.title
                FROM relations r
                JOIN entities e ON (r.target_id = e.id OR r.source_id = e.id)
                WHERE (r.source_id IN ({placeholders_r}) OR r.target_id IN ({placeholders_r}))
                  AND e.id NOT IN ({placeholders_r})
                  AND e.status != 'archived'
                  AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
            """,
                all_eids * 3,
            )
            for bsrc, btgt, bpred, beid, betitle in batch_rel_cursor.fetchall():
                anchor = bsrc if bsrc in all_eids else btgt
                related_map.setdefault(anchor, [])[:5]
                if len(related_map.get(anchor, [])) < 5:
                    related_map.setdefault(anchor, []).append(
                        {"predicate": bpred, "id": beid, "title": betitle}
                    )

        results = []
        for r in rows:
            (
                eid,
                etitle,
                econtent,
                eweight,
                eis_core,
                score,
                created,
                updated,
                owner,
                scope,
                meta,
                ctx,
                ememory_type,
                rel_c,
                fts_snippet_raw,
            ) = r
            if fts_snippet_raw:
                snippet = fts_snippet_raw
            else:
                _, snippet = extract_title_and_snippet(econtent)

            item = {
                "id": eid,
                "title": etitle,
                "snippet": snippet,
                "score": round(abs(score), 6),
                "weight": eweight,
                "is_core": bool(eis_core),
                "memory_type": ememory_type,
                "cursor": f"offset:{offset + limit}",
            }
            if include_related:
                item["related_entities"] = related_map.get(eid, [])
            if rerank_by_topic and eid in topic_scores_map:
                item["topic_score"] = round(topic_scores_map[eid]["topic_score"], 6)
                item["semantic_verdict"] = topic_scores_map[eid]["semantic_verdict"]
            if use_cross_encoder and eid in cross_encoder_scores_map:
                item["cross_encoder_score"] = round(cross_encoder_scores_map[eid], 6)
            if mode == "history" and eid in superseded_ids:
                item["is_superseded"] = True

            results.append(item)

        return results
    except Exception as e:
        logger.error("Error searching memory: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def fetch_memory_chunk(entity_id: str = None, db_connection=None, db_path: str = None, *, touch: bool = True) -> str:
    """Returns full markdown text of a memory."""
    if not entity_id:
        return "Error: entity_id is mandatory."
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        resolved_id = resolve_entity_id(conn, entity_id)
        if not resolved_id:
            return f"Error: Could not resolve memory entity for input '{entity_id}'."

        cursor = conn.execute(
            """
            SELECT id, title, full_content, status, created_at, updated_at, owner_id, scope, metadata
            FROM entities WHERE id = ?
        """,
            (resolved_id,),
        )
        row = cursor.fetchone()
        if row:
            if touch:
                now = datetime.now(UTC).isoformat()
                conn.execute("UPDATE entities SET last_accessed_at = ? WHERE id = ?", (now, resolved_id))
                if not is_coordinator_connection(conn):
                    conn.commit()
            return row[2]
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
    if resolved_id:
        db_connection.execute(
            "UPDATE entities SET last_accessed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), resolved_id),
        )


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

        now = datetime.now(UTC).isoformat()

        def _do_archive():
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
            from saltmdb.domain.services.embedding_service import cancel_embedding_jobs_for_entity
            cancel_embedding_jobs_for_entity(conn, resolved_id)

        if _in_transaction:
            _do_archive()
        else:

            def _write(c):
                _do_archive()

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


def check_duplicate_memories(  # noqa: C901, PLR0912, PLR0915
    title: str = None,
    content: str = None,
    owner_id: str = None,
    tags: list = None,
    context_id: str = None,
    exclude_ids: list = None,
    db_connection=None,
    db_path: str = None,
) -> dict:
    """Checks the database for potential near-duplicates of a proposed memory."""
    if not title and not content:
        return {"error": "Either title or content is required"}

    effective_db_path = db_path or get_db_path()
    should_close = False
    conn = db_connection
    if not conn:
        conn = get_connection(effective_db_path)
        should_close = True

    try:
        where = ["status != 'archived'"]
        fts_where_clauses = ["e.status != 'archived'"]
        params = []

        if exclude_ids:
            clean_excludes = [str(x) for x in exclude_ids if str(x)]
            if clean_excludes:
                placeholders = ",".join("?" for _ in clean_excludes)
                where.append(f"id NOT IN ({placeholders})")
                fts_where_clauses.append(f"e.id NOT IN ({placeholders})")
                params.extend(clean_excludes)

        if owner_id:
            where.append("(owner_id = ? OR owner_id IS NULL OR scope = 'shared')")
            fts_where_clauses.append("(e.owner_id = ? OR e.owner_id IS NULL OR e.scope = 'shared')")
            params.append(owner_id)

        if context_id:
            where.append("(context_id IS NULL OR context_id = ?)")
            fts_where_clauses.append("(e.context_id IS NULL OR e.context_id = ?)")
            params.append(context_id)

        from saltmdb.utils.text import sanitize_fts_query

        input_text = f"{title or ''} {content or ''}"
        duplicates = []

        # Pre-filter using FTS5 to reduce candidates from O(N) to ~30 max
        fts_candidates = []
        search_terms = sanitize_fts_query(input_text)
        if search_terms:
            try:
                fts_where = " AND ".join(fts_where_clauses) if fts_where_clauses else "1=1"
                fts_rows = conn.execute(
                    f"SELECT e.id, e.title, e.full_content, e.owner_id, e.scope FROM entities_fts fts "
                    f"JOIN entities e ON fts.id = e.id "
                    f"WHERE entities_fts MATCH ? AND {fts_where} LIMIT 30",
                    [search_terms] + params,
                ).fetchall()
                fts_candidates = fts_rows
            except Exception:
                pass

        # Fallback to full scan only if FTS returned nothing
        if not fts_candidates:
            cursor = conn.execute(
                f"SELECT id, title, full_content, owner_id, scope FROM entities "
                f"WHERE {' AND '.join(where) if where else '1=1'} LIMIT 30",
                params,
            )
            fts_candidates = cursor.fetchall()

        from saltmdb.config import is_semantic_search_enabled

        use_semantic = is_semantic_search_enabled()

        query_vector = None
        if use_semantic:
            try:
                from saltmdb.domain.services import embedding_service

                query_vector = embedding_service.embed_text(input_text)
            except Exception as ex:
                logger.warning("Could not generate query embedding for duplicate check: %s", ex)

        semantic_sims: dict = {}
        if use_semantic and query_vector is not None and fts_candidates:
            semantic_sims = _batch_semantic_similarities(
                [row[0] for row in fts_candidates], query_vector, effective_db_path
            )

        for eid, etitle, econtent, eowner, escope in fts_candidates:
            existing_text = f"{etitle} {econtent}"
            if eid in semantic_sims:
                sim = semantic_sims[eid]
                min_threshold = DEDUP_SUPERSESSION_THRESHOLD
            elif use_semantic and query_vector is not None:
                try:
                    from saltmdb.domain.services import embedding_service
                    import numpy as np

                    cand_vec = embedding_service.embed_text(existing_text)
                    dot_product = np.dot(query_vector, cand_vec)
                    norm_a = np.linalg.norm(query_vector)
                    norm_b = np.linalg.norm(cand_vec)
                    sim = (
                        float(dot_product / (norm_a * norm_b)) if norm_a > 0 and norm_b > 0 else 0.0
                    )
                    min_threshold = DEDUP_SUPERSESSION_THRESHOLD
                except Exception:
                    sim = word_sim(input_text, existing_text)
                    min_threshold = DEDUP_LEXICAL_THRESHOLD
            else:
                sim = word_sim(input_text, existing_text)
                min_threshold = DEDUP_LEXICAL_THRESHOLD

            if sim >= min_threshold:
                duplicates.append(
                    {
                        "id": eid,
                        "title": etitle,
                        "owner_id": eowner,
                        "scope": escope,
                        "similarity_score": round(sim, 3),
                    }
                )

        duplicates.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {"duplicate_found": len(duplicates) > 0, "potential_duplicates": duplicates}
    except Exception as e:
        logger.error("Error checking duplicate memories: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            close_connection(conn)


def scan_memories(
    owner_id: str = None,
    status_filter: str = None,
    limit: int = 20,
    offset: int = 0,
    cursor: str = None,
    db_connection=None,
    db_path: str = None,
) -> list:
    """Scans and inspects lists/contents of memories for audits."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    if cursor and cursor.startswith("offset:"):
        try:
            offset = int(cursor.split(":")[1])
        except ValueError:
            pass

    try:
        where = []
        params = []
        if owner_id:
            where.append("(owner_id = ? OR scope = 'shared')")
            params.append(owner_id)

        if status_filter:
            if status_filter == "active":
                where.append("status != 'archived'")
            else:
                where.append("status = ?")
                params.append(status_filter)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cursor_obj = conn.execute(
            f"""
            SELECT id, title, owner_id, status, weight, is_core, updated_at, memory_type
            FROM entities
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """,
            params + [limit, offset],
        )

        rows = cursor_obj.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "owner_id": r[2],
                "status": r[3],
                "weight": r[4],
                "is_core": bool(r[5]),
                "updated_at": r[6],
                "memory_type": r[7],
                "cursor": f"offset:{offset + limit}",
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Error scanning memories: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def bulk_archive_memory(archive_requests: list, db_connection=None, db_path: str = None) -> list:
    """Bulk archives memories atomically -- all-or-nothing.

    The entire batch runs inside a single write_transaction_retrying transaction. If any
    item raises (or would otherwise be reported as an error), the whole transaction rolls
    back, so no partial set of archives is ever left committed. Because a single failure
    unwinds every prior "successful" item in the same batch, a mixed per-item success/error
    list would misrepresent the outcome -- so on failure this returns a single top-level
    error result instead of claiming any individual items succeeded.
    """
    if not archive_requests or not isinstance(archive_requests, list):
        return [{"status": "error", "error": "archive_requests must be a non-empty list"}]
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
            for req in archive_requests:
                eid = req if isinstance(req, str) else req.get("entity_id")
                owner = req.get("owner_id") if isinstance(req, dict) else None
                res = archive_memory(
                    entity_id=eid, owner_id=owner, db_connection=conn, _in_transaction=True
                )
                if res.startswith("Error"):
                    raise RuntimeError(f"Bulk archive aborted (all-or-nothing): {res}")
                results.append({"status": "success", "entity_id": eid, "result": res})

        write_transaction_retrying(conn, _write)
        return results
    except Exception as e:
        logger.error("Error in bulk archive memory (batch rolled back, no items archived): %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


_TAG_NAME_RE = re.compile(r"^#[a-z0-9][a-z0-9-]*$")


def normalize_tag_name(tag_name: str) -> str:
    """Ensures a bare or malformed tag string is '#'-prefixed. Pure syntactic helper,
    reused by write paths and read-path tag filters alike (replaces duplicated
    auto-prefix one-liners across the codebase)."""
    name = (tag_name or "").strip()
    if not name:
        return name
    if not name.startswith("#"):
        name = "#" + name
    return name


def resolve_or_create_tag(conn, tag_name: str, agent_id: str = None) -> str | None:
    """Single source of truth for tag write-time resolution.

    Must be called with `conn` already inside an open write transaction (does not open
    its own). Returns the resolved (canonical, if aliased) tag id, or None if the name is
    empty/unsalvageable after sanitization.

    Resolution order:
      1. Shape-sanitize the name (lowercase, strip characters not in [a-z0-9-] after the
         '#' prefix). If sanitization actually changed the string, fire a soft
         log_event(type='issue') noting the before/after -- this never blocks resolution,
         it's visibility only.
      2. Exact `name` match.
      3. The existing normalized_name / computed-normalization fallback (mirrors
         store_memory's fuzzy lookup exactly).
      4. A simple plural/suffix fallback: only when the normalized input is longer than 3
         chars, full-scan `tags` and compare `norm_input.rstrip('s')` against each row's
         normalized form (also only when that row's normalized form is longer than 3
         chars) -- return on first match.
      5. Otherwise, create a new tag row and return its new id.

    At every step, if a row is found, return `canonical_id if canonical_id else id` --
    respecting existing alias merges (the exact behavior gap commit_consolidation is
    currently missing).
    """
    name = normalize_tag_name(tag_name)
    if not name or name == "#":
        return None

    # Step 1: shape-sanitize -- lowercase, strip anything outside [a-z0-9-] after '#'.
    raw_body = name[1:]
    sanitized_body = re.sub(r"[^a-z0-9-]", "", raw_body.lower())
    sanitized_name = ("#" + sanitized_body) if sanitized_body else name

    if sanitized_name != name:
        try:
            from saltmdb.domain.services.event_service import log_event

            log_event(
                agent_id=agent_id or "system",
                type="issue",
                content=f"Tag name sanitized during resolve_or_create_tag: '{name}' -> '{sanitized_name}'",
                db_connection=conn,
                _in_transaction=True,
            )
        except Exception as ex:
            logger.warning("Failed to log tag sanitization event: %s", ex)
        name = sanitized_name

    if not name or name == "#":
        return None

    # Step 2: exact match
    row = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    # Step 3: normalized_name / computed-normalization fallback (mirrors store_memory)
    norm_input = name.lower().lstrip("#")
    norm_input = re.sub(r"[-_\s]+", "", norm_input)

    row = conn.execute(
        "SELECT id, canonical_id FROM tags WHERE normalized_name = ? OR lower(replace(replace(replace(name,'#',''),'-',''),'_','')) = ?",
        (norm_input, norm_input),
    ).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    # Step 4: plural/suffix fallback -- full scan (small table, same cost model already
    # accepted by merge_tags_heuristics()), only for norm_input longer than 3 chars.
    if len(norm_input) > 3:
        stripped_input = norm_input.rstrip("s")
        all_rows = conn.execute(
            "SELECT id, name, normalized_name, canonical_id FROM tags"
        ).fetchall()
        for tid, tname, tnorm, tcanon in all_rows:
            existing_norm = tnorm if tnorm else re.sub(r"[-_\s]+", "", tname.lower().lstrip("#"))
            if len(existing_norm) > 3 and stripped_input == existing_norm.rstrip("s"):
                return tcanon if tcanon else tid

    # Step 5: create a new tag row
    tag_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
        (tag_id, name, norm_input),
    )
    return tag_id


# NOTE: this 'domain' param is a tag-name substring filter, unrelated to the entities table.
def get_canonical_tags(
    domain: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> list:
    """Queries canonical tags."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        if domain:
            cursor = conn.execute(
                """
                SELECT id, name FROM tags
                WHERE canonical_id IS NULL AND name LIKE ?
                LIMIT ?
            """,
                (f"%{domain}%", limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, name FROM tags
                WHERE canonical_id IS NULL
                LIMIT ?
            """,
                (limit,),
            )
        rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        logger.error("Error fetching canonical tags: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
