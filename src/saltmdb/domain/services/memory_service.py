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
)
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
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


def _handle_supersession_candidate(
    conn,
    entity_id: str,
    matched_supersession_id: str,
    matched_supersession_title: str | None,
    matched_sim_score: float,
    owner_id: str | None,
    context_id: str | None,
) -> None:
    """Logs a reviewable supersession_candidate event and, above the stricter duplicate
    band, auto-links a 'similar_to' relation edge -- additive only (no weight/is_core change,
    no suppression from search). A cosine score above the duplicate threshold is a defensible
    claim of "these are semantically close"; it is NOT a defensible claim of "this replaces
    that" (see alpha.47 regression: auto-supersedes + weight demotion on the weaker signal
    silently buried an unreviewed memory). The judgment call of whether to also add a
    directional 'supersedes' edge stays with whoever reviews the supersession_candidate event.
    Must be called inside the caller's open write transaction (mirrors resolve_or_create_tag).
    """
    try:
        from saltmdb.domain.services.event_service import log_event

        candidate_payload = json.dumps(
            {
                "new_entity_id": entity_id,
                "target_entity_id": matched_supersession_id,
                "similarity_score": matched_sim_score,
                "target_title": matched_supersession_title,
            }
        )
        log_event(
            agent_id=owner_id or "system",
            type="supersession_candidate",
            content=candidate_payload,
            context_id=context_id,
            db_connection=conn,
            _in_transaction=True,
        )
        logger.info(
            "Auto-Supersession: Logged 'supersession_candidate' event for new memory %s -> target %s",
            entity_id,
            matched_supersession_id,
        )
    except Exception as ex:
        logger.warning("Failed to log supersession_candidate event: %s", ex)

    if matched_sim_score >= DEDUP_DUPLICATE_THRESHOLD:
        try:
            from saltmdb.domain.services.relation_service import store_relation

            store_relation(
                source_id=entity_id,
                target_id=matched_supersession_id,
                predicate="similar_to",
                db_connection=conn,
                _in_transaction=True,
            )
        except Exception as ex:
            logger.warning("Failed to auto-link similar_to relation: %s", ex)


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
) -> str:
    """Stores a consolidated Markdown fact chunk as a long-term memory."""
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

    redacted_content = redact_secrets(content)
    now = datetime.now(UTC).isoformat()

    if not title:
        title, _ = extract_title_and_snippet(redacted_content)
    else:
        title = redact_secrets(title)

    if not title or not title.strip():
        return "Error: title is mandatory and cannot be empty."

    try:
        validate_memory_input(title, redacted_content, metadata)
    except ValueError as e:
        if should_close:
            close_connection(conn)
        return str(e)

    # Stage 1: Auto-Formatting (Idempotent cleanup: f(f(x)) = f(x))
    from saltmdb.utils.nlp import auto_format_markdown

    redacted_content = auto_format_markdown(redacted_content)

    if not context_id and metadata and isinstance(metadata, dict):
        context_id = metadata.get("project") or metadata.get("project_id")

    # Stage 2 & 3: Extract Prose & Pre-Embedding Quality Gate Evaluation
    quality_res = evaluate_memory_quality(redacted_content, title)
    if quality_res["status"] == "REJECT":
        if should_close:
            close_connection(conn)
        return f"Error: Memory quality check rejected (Score: {quality_res['quality_score']:.2f}). Reason: {quality_res['reason']}"

    content_hash = compute_content_hash(redacted_content)
    quality_score = quality_res["quality_score"]
    quality_status = quality_res["status"]
    quality_flags_str = json.dumps(quality_res["quality_flags"])

    # Stage 4: Stage A Exact Hash Collision Lookup
    if not entity_id:
        try:
            cursor = conn.execute(
                """
                SELECT id FROM entities
                WHERE content_hash = ? AND (owner_id = ? OR scope = 'shared') AND status != 'archived'
            """,
                (content_hash, owner_id),
            )
            row = cursor.fetchone()
            if row:
                if should_close:
                    close_connection(conn)
                return f"Error: REJECT_EXACT_DUPLICATE - Memory with exact content hash already exists with ID: {row[0]}"
        except Exception:
            pass

    if not entity_id:
        try:
            cursor = conn.execute(
                """
                SELECT id FROM entities
                WHERE title = ? AND owner_id = ? AND scope = ? AND status != 'archived'
            """,
                (title, owner_id, scope),
            )
            row = cursor.fetchone()
            if row:
                entity_id = row[0]
                logger.debug(
                    "Deduplication: Matched existing memory '%s' (ID: %s). Routing to temporal upsert.",
                    title,
                    entity_id,
                )
        except Exception:
            pass

    matched_supersession_id = None
    matched_supersession_title = None
    matched_sim_score = 0.0
    duplicate_warning_str = None

    if not entity_id and not skip_duplicate_check:
        try:
            dup_check = check_duplicate_memories(
                title=title,
                content=redacted_content,
                owner_id=owner_id,
                tags=tags,
                context_id=context_id,
                db_connection=conn,
            )
            if dup_check.get("duplicate_found") and "error" not in dup_check:
                top = dup_check["potential_duplicates"][0]
                sim_score = top.get("similarity_score", 0.0)
                matched_owner = top.get("owner_id")

                # Check namespace isolation: candidate must be ownerless, owned by the caller,
                # or itself scope='shared' (visible to any owner) - not a literal owner_id match on "shared".
                matched_scope = top.get("scope")
                if matched_owner is None or matched_owner == owner_id or matched_scope == "shared":
                    if sim_score >= DEDUP_SUPERSESSION_THRESHOLD:
                        matched_supersession_id = top["id"]
                        matched_supersession_title = top["title"]
                        matched_sim_score = sim_score
                        logger.info(
                            "Calibrated Cosine Supersession Candidate: New memory '%s' matches existing memory '%s' (ID: %s, Cosine Similarity: %.2f)",
                            title,
                            top["title"],
                            top["id"],
                            sim_score,
                        )

                    if sim_score >= DEDUP_DUPLICATE_THRESHOLD:
                        duplicate_warning_str = f" [WARNING: Potential duplicate of existing memory '{top['title']}' (ID: {top['id']}, similarity {sim_score})]"
        except Exception:
            pass

    if not entity_id:
        entity_id = str(uuid.uuid4())

    try:

        def _write(c):  # noqa: C901, PLR0912
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

            # Stage 5: Supersession Candidate Event Logging (Replaces unconfirmed auto-linking & weight demotion)
            if matched_supersession_id:
                _handle_supersession_candidate(
                    conn=conn,
                    entity_id=entity_id,
                    matched_supersession_id=matched_supersession_id,
                    matched_supersession_title=matched_supersession_title,
                    matched_sim_score=matched_sim_score,
                    owner_id=owner_id,
                    context_id=context_id,
                )

            return existing

        existing = write_transaction_retrying(conn, _write)

        from saltmdb.domain.services.librarian_service import trigger_librarian

        trigger_librarian(db_path=db_path)

        target_db = db_path or get_db_path()
        if target_db:
            from saltmdb.domain.services import embedding_service

            _embed_pool.submit(
                embedding_service.embed_entity_async, entity_id, title, redacted_content, target_db
            )

        res_msg = f"Knowledge stored successfully with ID: {entity_id}"
        if duplicate_warning_str:
            res_msg += duplicate_warning_str
        if not existing and tags:
            res_msg += " [Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]"
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
    conn, sanitized_query: str, where_clauses: list, params: list, limit: int, offset: int
) -> list:
    """Execute the FTS5/BM25 query with AND->OR fallback. Returns sqlite3 Row list.

    Each row's last column, fts_snippet, is a query-centered excerpt of full_content
    (FTS5 snippet(), column index 2) -- populated because this row genuinely matched via
    FTS5 MATCH in this query, distinct from rows that only surface via semantic_search().
    """
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in STOP_WORDS]
    if not terms:
        terms = raw_terms

    if not terms:
        return []

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
    if not rows and len(terms) > 1:
        fts_fallback_query = " OR ".join(f'"{t}"*' for t in terms)
        exec_params_fb = [fts_fallback_query] + params + [limit, offset]
        rows = conn.execute(sql, exec_params_fb).fetchall()
    return rows


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
        sql = f"""
            SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
            FROM entity_embeddings ee
            JOIN entities e ON ee.entity_id = e.id
            WHERE e.embedding_status = 'ready' AND {where_sql}
            ORDER BY distance ASC
            LIMIT ? OFFSET ?
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector)] + params + [limit, offset]
        rows = conn.execute(sql, exec_params).fetchall()
        return [(row[0], row[1]) for row in rows]
    except Exception as e:
        logger.warning("Semantic search failed, falling back to FTS5 only: %s", e)
        return []
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
    db_connection=None,
    db_path: str = None,
) -> list | dict:
    """Performs full-text keyword search and filtering in long-term memory."""
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
        if sanitized_query:
            assert query_keywords  # nosec B101 -- mypy narrowing only, not a runtime safety check
            from saltmdb.config import is_semantic_search_enabled

            if is_semantic_search_enabled():
                if not db_path:
                    db_path = get_db_path()
                candidate_window = offset + limit
                fts_future = _search_pool.submit(
                    _run_fts_search,
                    conn,
                    sanitized_query,
                    where_clauses,
                    params,
                    candidate_window,
                    0,
                )
                # semantic_search gets db_path so it opens its OWN connection
                # — never share a connection across threads with sqlite_vec loaded
                semantic_future = _search_pool.submit(
                    semantic_search,
                    query_keywords,
                    where_clauses,
                    params,
                    candidate_window,
                    db_path,
                    0,
                )
                fts_rows = fts_future.result()
                semantic_rows = semantic_future.result()

                rrf_score_map = reciprocal_rank_fusion(fts_rows, semantic_rows, candidate_window)

                if rrf_score_map:
                    merged_ids = list(rrf_score_map.keys())[offset : offset + limit]
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
                    rows = fts_rows[offset : offset + limit]
            else:
                rows = _run_fts_search(conn, sanitized_query, where_clauses, params, limit, offset)
        else:
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

        # Batch-fetch all related entities in a single query to avoid N+1
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

            results.append(item)

        return results
    except Exception as e:
        logger.error("Error searching memory: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)


def fetch_memory_chunk(entity_id: str = None, db_connection=None, db_path: str = None) -> str:
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
            now = datetime.now(UTC).isoformat()
            conn.execute(
                "UPDATE entities SET last_accessed_at = ? WHERE id = ?", (now, resolved_id)
            )
            conn.commit()
            return row[2]
        return f"Memory not found for ID: {resolved_id}"
    except Exception as e:
        logger.error("Error fetching memory chunk: %s", e)
        return f"Error fetching memory chunk: {e}"
    finally:
        if should_close:
            close_connection(conn)


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
