import uuid
import json
import re
import logging
from datetime import datetime, UTC
from typing import Literal
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.utils.text import (
    resolve_entity_id,
    extract_title_and_snippet,
    sanitize_fts_query,
    normalize_search_query,
    compute_content_hash
)
from saltmdb.utils.nlp import word_sim, evaluate_memory_quality
from saltmdb.utils.redaction import redact_secrets
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
_embed_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="saltmdb-embed")

def validate_memory_input(title: str, content: str, metadata: dict) -> None:
    """Validates memory input to enforce title hygiene and relative path constraints."""
    if title:
        pattern = r"^[a-zA-Z0-9_\-\.]+\.(md|txt|json|yml|yaml)\s*[-—–:|]\s*"
        if re.search(pattern, title, re.IGNORECASE):
            raise ValueError(
                "Error: Title violates clean title guidelines. Do not prefix memory titles with file names or file extensions (e.g., use 'Language Rules' instead of 'CORE.md — Language Rules')."
            )
            
    if metadata and isinstance(metadata, dict):
        source_path = metadata.get("source_path")
        if source_path:
            is_absolute = (
                re.match(r"^[a-zA-Z]:", source_path) or
                source_path.startswith("/") or
                source_path.startswith("\\") or
                "/Users/" in source_path or
                "\\Users\\" in source_path or
                "/home/" in source_path
            )
            if is_absolute:
                raise ValueError(
                    "Error: 'source_path' must be a relative repository path (e.g., 'CORE.md' or 'notes.md'). Absolute system paths are forbidden."
                )

def store_memory(
    content: str = None,
    tags: list = None,
    owner_id: str = None,
    scope: Literal['private', 'shared'] = "shared",
    weight: int = 1,
    is_core: bool = False,
    title: str = None,
    entity_id: str = None,
    relevance: int = None,
    impact: int = None,
    novelty: int = None,
    actionability: int = None,
    metadata: dict = None,
    skip_duplicate_check: bool = False,
    project_id: str = None,
    context_id: str = None,
    db_connection = None,
    db_path: str = None
) -> str:
    """Stores a consolidated Markdown fact chunk as a long-term memory."""
    if not owner_id:
        return "Error: owner_id is mandatory in this version of SALTMDB to prevent cross-lane signal contamination."
        
    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."

    if scope not in ('private', 'shared'):
        return "Error: scope must be either 'private' or 'shared'"
        
    if relevance is not None or impact is not None or novelty is not None or actionability is not None:
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
            conn.close()
        return str(e)
        
    # Stage 1: Auto-Formatting (Idempotent cleanup: f(f(x)) = f(x))
    from saltmdb.utils.nlp import auto_format_markdown
    redacted_content = auto_format_markdown(redacted_content)

    context_id = context_id or project_id
    if not context_id and metadata and isinstance(metadata, dict):
        context_id = metadata.get("project") or metadata.get("project_id")
    project_id = context_id

    # Stage 2 & 3: Extract Prose & Pre-Embedding Quality Gate Evaluation
    quality_res = evaluate_memory_quality(redacted_content, title)
    if quality_res["status"] == "REJECT":
        if should_close:
            conn.close()
        return f"Error: Memory quality check rejected (Score: {quality_res['quality_score']:.2f}). Reason: {quality_res['reason']}"

    content_hash = compute_content_hash(redacted_content)
    quality_score = quality_res["quality_score"]
    quality_status = quality_res["status"]
    quality_flags_str = json.dumps(quality_res["quality_flags"])

    # Stage 4: Stage A Exact Hash Collision Lookup
    if not entity_id:
        try:
            cursor = conn.execute("""
                SELECT id FROM entities
                WHERE content_hash = ? AND owner_id = ? AND status != 'archived'
            """, (content_hash, owner_id))
            row = cursor.fetchone()
            if row:
                if should_close:
                    conn.close()
                return f"Error: REJECT_EXACT_DUPLICATE - Memory with exact content hash already exists with ID: {row[0]}"
        except Exception:
            pass

    if not entity_id:
        try:
            cursor = conn.execute("""
                SELECT id FROM entities 
                WHERE title = ? AND owner_id = ? AND scope = ? AND status != 'archived'
            """, (title, owner_id, scope))
            row = cursor.fetchone()
            if row:
                entity_id = row[0]
                logger.debug("Deduplication: Matched existing memory '%s' (ID: %s). Routing to temporal upsert.", title, entity_id)
        except Exception:
            pass

    matched_supersession_id = None
    if not entity_id and not skip_duplicate_check:
        try:
            dup_check = check_duplicate_memories(
                title=title,
                content=redacted_content,
                owner_id=owner_id,
                tags=tags,
                project_id=project_id,
                db_connection=conn
            )
            if dup_check.get("duplicate_found") and "error" not in dup_check:
                top = dup_check["potential_duplicates"][0]
                sim_score = top.get("similarity_score", 0.0)
                matched_owner = top.get("owner_id")
                
                # Calibrated Auto-Supersession: similarity >= 0.88 (Cosine Distance <= 0.12)
                # Enforce owner_id namespace isolation: match caller_owner_id or 'shared'
                if sim_score >= 0.88 and (matched_owner is None or matched_owner == owner_id or matched_owner == "shared" or owner_id == "shared"):
                    matched_supersession_id = top["id"]
                    logger.info("Calibrated Auto-Supersession: New memory '%s' supersedes existing memory '%s' (ID: %s, Similarity: %.2f)", title, top["title"], top["id"], sim_score)
                else:
                    if should_close:
                        conn.close()
                    return (f"Warning: Potential duplicate of existing memory '{top['title']}' "
                            f"(ID: {top['id']}, similarity {top['similarity_score']}). "
                            f"Call store_memory with entity_id='{top['id']}' to update it instead, "
                            f"or set skip_duplicate_check=True to force a new entry.")
        except Exception:
            pass
            
    if not entity_id:
        entity_id = str(uuid.uuid4())
        
    try:
        with conn:
            cursor = conn.execute("SELECT created_at, owner_id, valid_from FROM entities WHERE id = ?", (entity_id,))
            existing = cursor.fetchone()
            if existing:
                 created_at, owner, valid_from = existing
                 hist_id = f"{entity_id}_h_{str(uuid.uuid4())[:8]}"
                 
                 conn.execute("""
                     INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id, embedding_status, content_hash, quality_score, quality_status, quality_flags)
                     SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, project_id, context_id, 'archived', content_hash, quality_score, quality_status, quality_flags
                     FROM entities WHERE id = ?
                 """, (hist_id, valid_from if valid_from else created_at, now, entity_id))
                 
                 conn.execute("""
                     INSERT INTO entity_tags (entity_id, tag_id)
                     SELECT ?, tag_id FROM entity_tags WHERE entity_id = ?
                 """, (hist_id, entity_id))
                 
            conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))
            
            metadata_str = json.dumps(metadata) if metadata else None
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id, content_hash, quality_score, quality_status, quality_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    last_accessed_at = excluded.last_accessed_at,
                    owner_id = COALESCE(excluded.owner_id, entities.owner_id),
                    scope = excluded.scope,
                    is_core = excluded.is_core,
                    weight = excluded.weight,
                    status = excluded.status,
                    title = excluded.title,
                    full_content = excluded.full_content,
                    valid_from = excluded.valid_from,
                    valid_to = NULL,
                    metadata = excluded.metadata,
                    project_id = COALESCE(excluded.project_id, entities.project_id),
                    context_id = COALESCE(excluded.context_id, entities.context_id),
                    content_hash = excluded.content_hash,
                    quality_score = excluded.quality_score,
                    quality_status = excluded.quality_status,
                    quality_flags = excluded.quality_flags
            """, (entity_id, now, now, now, owner_id, scope, 1 if is_core else 0, weight, json.dumps([]), title, redacted_content, now, metadata_str, project_id, context_id, content_hash, quality_score, quality_status, quality_flags_str))
            
            tags = tags or []
            tag_lookup = {}  # norm -> canonical_or_tag_id, built lazily
            for tag_name in tags:
                tag_name = tag_name.strip()
                if not tag_name:
                    continue
                if not tag_name.startswith('#'):
                    tag_name = '#' + tag_name

                norm_input = tag_name.lower().lstrip('#')
                norm_input = re.sub(r'[-_\s]+', '', norm_input)

                # Use cached result if we already resolved this tag
                if norm_input in tag_lookup:
                    tag_id = tag_lookup[norm_input]
                else:
                    # Targeted point query instead of full table scan
                    row = conn.execute(
                        "SELECT id, canonical_id FROM tags WHERE name = ?", (tag_name,)
                    ).fetchone()
                    if row:
                        tag_id = row[1] if row[1] else row[0]
                        tag_lookup[norm_input] = tag_id
                    else:
                        # Try normalized lookup for fuzzy match
                        fuzzy_row = conn.execute(
                            "SELECT id, canonical_id FROM tags WHERE lower(replace(replace(replace(name,'#',''),'-',''),'_','')) = ?",
                            (norm_input,)
                        ).fetchone()
                        if fuzzy_row:
                            tag_id = fuzzy_row[1] if fuzzy_row[1] else fuzzy_row[0]
                            tag_lookup[norm_input] = tag_id
                        else:
                            tag_id = str(uuid.uuid4())
                            conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)", (tag_id, tag_name))
                            tag_lookup[norm_input] = tag_id

                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                
            # Stage 5: Calibrated Auto-Supersession Relation Edge Insertion & Weight Adjustment
            if matched_supersession_id:
                try:
                    from saltmdb.domain.services.relation_service import store_relation
                    store_relation(
                        source_id=entity_id,
                        target_id=matched_supersession_id,
                        predicate="supersedes",
                        db_connection=conn
                    )
                    # Lower older superseded entity's weight to 1
                    conn.execute("UPDATE entities SET weight = 1 WHERE id = ?", (matched_supersession_id,))
                    logger.info("Auto-Supersession: Created 'supersedes' relation edge from %s -> %s and set older entity weight to 1", entity_id, matched_supersession_id)
                except Exception as ex:
                    logger.warning("Failed to auto-store supersedes relation edge: %s", ex)

        from saltmdb.domain.services.librarian_service import trigger_librarian
        trigger_librarian(db_path=db_path)

        target_db = db_path or get_db_path()
        if target_db:
            from saltmdb.domain.services import embedding_service
            _embed_pool.submit(embedding_service.embed_entity_async, entity_id, title, redacted_content, target_db)

        return f"Knowledge stored successfully with ID: {entity_id}"
    except Exception as e:
        logger.error("Error storing knowledge: %s", e)
        return f"Error storing knowledge: {e}"
    finally:
        if should_close:
            conn.close()

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he", "in", "is", "it", "its",
    "of", "on", "that", "the", "to", "was", "were", "will", "with", "what", "my", "when", "i", "type", "did",
    "how", "does", "or", "which", "who", "whom", "this", "these", "those"
}

def _run_fts_search(
    conn, sanitized_query: str, where_clauses: list, params: list,
    limit: int, offset: int
) -> list:
    """Execute the FTS5/BM25 query with AND->OR fallback. Returns sqlite3 Row list."""
    raw_terms = sanitized_query.split()
    terms = [t for t in raw_terms if t.lower() not in STOP_WORDS]
    if not terms:
        terms = raw_terms

    if not terms:
        return []

    fts_query_str = " ".join(f'"{t}"*' for t in terms)
    where_sql = f" AND {' AND '.join(where_clauses)}" if where_clauses else ""
    sql = f"""
        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
               bm25(entities_fts, 10.0, 1.0, 5.0) as rank_score,
               e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id,
               (SELECT COUNT(*) FROM relations r WHERE r.target_id = e.id
                AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))) as rel_count
        FROM entities_fts fts
        JOIN entities e ON fts.id = e.id
        WHERE fts.entities_fts MATCH ?{where_sql}
        ORDER BY (bm25(entities_fts, 10.0, 1.0, 5.0) * e.weight - (rel_count * 0.1)) ASC,
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
            LIMIT ?
        """
        exec_params = [sqlite_vec.serialize_float32(query_vector)] + params + [limit]
        rows = conn.execute(sql, exec_params).fetchall()
        return [(row[0], row[1]) for row in rows]
    except Exception as e:
        logger.warning("Semantic search failed, falling back to FTS5 only: %s", e)
        return []
    finally:
        if conn:
            conn.close()


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


def search_memory(
    owner_id: str = None,
    query_keywords: str = None,
    tags_filter: list = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
    project_id: str = None,
    context_id: str = None,
    is_core: bool = None,
    tag_operator: Literal['AND', 'OR'] = "AND",
    cursor: str = None,
    include_related: bool = True,
    db_connection = None,
    db_path: str = None
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
        params = []

        if owner_id:
            where_clauses.append("(e.owner_id = ? OR e.scope = 'shared')")
            params.append(owner_id)

        context_val = context_id or project_id
        if context_val:
            where_clauses.append("(e.context_id = ? OR e.project_id = ? OR json_extract(e.metadata, '$.project') = ? OR json_extract(e.metadata, '$.project_id') = ?)")
            params.extend([context_val, context_val, context_val, context_val])

        if is_core is not None:
            where_clauses.append("e.is_core = ?")
            params.append(1 if is_core else 0)

        if metadata_filter and isinstance(metadata_filter, dict):
            for mk, mv in metadata_filter.items():
                where_clauses.append(f"json_extract(e.metadata, '$.{mk}') = ?")
                params.append(str(mv))

        if tags_filter:
            norm_tags = [t.strip() if t.strip().startswith('#') else '#' + t.strip() for t in tags_filter if t.strip()]
            if norm_tags:
                expanded_tag_ids = set()
                for tname in norm_tags:
                    c = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (tname,))
                    r = c.fetchone()
                    if r:
                        tid, tcanon = r
                        main_id = tcanon if tcanon else tid
                        expanded_tag_ids.add(main_id)
                        alias_c = conn.execute("SELECT id FROM tags WHERE canonical_id = ?", (main_id,))
                        for ar in alias_c.fetchall():
                            expanded_tag_ids.add(ar[0])
                    else:
                        norm_input = tname.lower().lstrip('#')
                        norm_input = re.sub(r'[-_\s]+', '', norm_input)
                        alias_c = conn.execute("SELECT id, canonical_id FROM tags")
                        for ar_id, ar_name, ar_canon in alias_c.fetchall():
                            ar_norm = ar_name.lower().lstrip('#')
                            ar_norm = re.sub(r'[-_\s]+', '', ar_norm)
                            if ar_norm == norm_input:
                                expanded_tag_ids.add(ar_canon if ar_canon else ar_id)

                if expanded_tag_ids:
                    tag_placeholders = ",".join("?" for _ in expanded_tag_ids)
                    where_clauses.append(f"""
                        e.id IN (
                            SELECT et.entity_id 
                            FROM entity_tags et
                            WHERE et.tag_id IN ({tag_placeholders})
                        )
                    """)
                    params.extend(list(expanded_tag_ids))
                else:
                    tag_placeholders = ",".join("?" for _ in norm_tags)
                    where_clauses.append(f"""
                        e.id IN (
                            SELECT et.entity_id 
                            FROM entity_tags et
                            JOIN tags t ON et.tag_id = t.id
                            WHERE t.name IN ({tag_placeholders})
                        )
                    """)
                    params.extend(norm_tags)

        sanitized_query = sanitize_fts_query(query_keywords) if query_keywords else ""

        if explain_mode:
            terms = sanitized_query.split() if sanitized_query else []
            searched_terms = {}
            for t in terms:
                c = conn.execute("SELECT 1 FROM entities_fts WHERE entities_fts MATCH ?", (f'"{t}"*',)).fetchone()
                searched_terms[t] = bool(c)
                
            invalid_tags = []
            if tags_filter:
                for tf in tags_filter:
                    tname = tf.strip() if tf.strip().startswith('#') else '#' + tf.strip()
                    c = conn.execute("SELECT 1 FROM tags WHERE name = ?", (tname,)).fetchone()
                    if not c:
                        invalid_tags.append(tf)

            return {
                "explain": {
                    "searched_terms_found": searched_terms,
                    "invalid_tags_suggestions": invalid_tags,
                    "sanitized_query": sanitized_query,
                    "where_clauses": where_clauses
                }
            }

        rows = []
        if sanitized_query:
            from saltmdb.config import is_semantic_search_enabled
            if is_semantic_search_enabled():
                if not db_path:
                    db_path = get_db_path()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    fts_future = executor.submit(
                        _run_fts_search, conn, sanitized_query, where_clauses,
                        params, limit, offset
                    )
                    # semantic_search gets db_path so it opens its OWN connection
                    # — never share a connection across threads with sqlite_vec loaded
                    semantic_future = executor.submit(
                        semantic_search, query_keywords, where_clauses, params,
                        limit, db_path
                    )
                    fts_rows = fts_future.result()
                    semantic_rows = semantic_future.result()

                rrf_score_map = reciprocal_rank_fusion(fts_rows, semantic_rows, limit)

                if rrf_score_map:
                    merged_ids = list(rrf_score_map.keys())
                    placeholders = ",".join("?" for _ in merged_ids)
                    id_order = {eid: i for i, eid in enumerate(merged_ids)}
                    fetch_sql = f"""
                        SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                               0.0 as rank_score,
                               e.created_at, e.updated_at, e.owner_id, e.scope,
                               e.metadata, e.context_id, 0 as rel_count
                        FROM entities e
                        WHERE e.id IN ({placeholders})
                    """
                    fetched = conn.execute(fetch_sql, merged_ids).fetchall()
                    sorted_fetched = sorted(fetched, key=lambda r: id_order.get(r[0], 9999))
                    rows = []
                    for r in sorted_fetched:
                        r_list = list(r)
                        r_list[5] = rrf_score_map.get(r[0], 0.0)
                        rows.append(r_list)
                else:
                    rows = fts_rows
            else:
                rows = _run_fts_search(conn, sanitized_query, where_clauses, params, limit, offset)
        else:
            sql = f"""
                SELECT e.id, e.title, e.full_content, e.weight, e.is_core,
                       0.0 as rank_score,
                       e.created_at, e.updated_at, e.owner_id, e.scope, e.metadata, e.context_id,
                       0 as rel_count
                FROM entities e
                WHERE {" AND ".join(where_clauses)}
                ORDER BY e.is_core DESC, e.updated_at DESC
                LIMIT ? OFFSET ?
            """
            exec_params = params + [limit, offset]
            cursor_obj = conn.execute(sql, exec_params)
            rows = cursor_obj.fetchall()

        # Batch-fetch all related entities in a single query to avoid N+1
        related_map = {}  # {entity_id: [related items]}
        if include_related and rows:
            all_eids = [r[0] for r in rows]
            placeholders_r = ",".join("?" for _ in all_eids)
            batch_rel_cursor = conn.execute(f"""
                SELECT r.source_id, r.target_id, r.predicate, e.id, e.title
                FROM relations r
                JOIN entities e ON (r.target_id = e.id OR r.source_id = e.id)
                WHERE (r.source_id IN ({placeholders_r}) OR r.target_id IN ({placeholders_r}))
                  AND e.id NOT IN ({placeholders_r})
                  AND e.status != 'archived'
            """, all_eids * 3)
            for bsrc, btgt, bpred, beid, betitle in batch_rel_cursor.fetchall():
                anchor = bsrc if bsrc in all_eids else btgt
                related_map.setdefault(anchor, [])[:5]
                if len(related_map.get(anchor, [])) < 5:
                    related_map.setdefault(anchor, []).append({"predicate": bpred, "id": beid, "title": betitle})

        results = []
        for r in rows:
            eid, etitle, econtent, eweight, eis_core, score, created, updated, owner, scope, meta, ctx, rel_c = r
            _, snippet = extract_title_and_snippet(econtent)
            
            item = {
                "id": eid,
                "title": etitle,
                "snippet": snippet,
                "score": round(abs(score), 6),
                "weight": eweight,
                "is_core": bool(eis_core),
                "cursor": f"offset:{offset + limit}"
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
            conn.close()

def fetch_memory_chunk(entity_id: str = None, db_connection = None, db_path: str = None) -> str:
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
            
        cursor = conn.execute("""
            SELECT id, title, full_content, status, created_at, updated_at, owner_id, scope, metadata
            FROM entities WHERE id = ?
        """, (resolved_id,))
        row = cursor.fetchone()
        if row:
            now = datetime.now(UTC).isoformat()
            conn.execute("UPDATE entities SET last_accessed_at = ? WHERE id = ?", (now, resolved_id))
            conn.commit()
            return row[2]
        return f"Memory not found for ID: {resolved_id}"
    except Exception as e:
        logger.error("Error fetching memory chunk: %s", e)
        return f"Error fetching memory chunk: {e}"
    finally:
        if should_close:
            conn.close()

def archive_memory(entity_id: str = None, owner_id: str = None, db_connection = None, db_path: str = None) -> str:
    """Explicitly archives (retires) a long-term memory."""
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
            
        cursor = conn.execute("SELECT owner_id, scope, status FROM entities WHERE id = ?", (resolved_id,))
        row = cursor.fetchone()
        if not row:
            return f"Error: Memory '{resolved_id}' not found."
            
        existing_owner, scope, status = row
        if status == "archived":
            return f"Error: Memory '{resolved_id}' is already archived."
        if owner_id and existing_owner and existing_owner != owner_id:
            return f"Error: Memory '{resolved_id}' owner mismatch."
            
        now = datetime.now(UTC).isoformat()
        with conn:
            conn.execute("""
                UPDATE entities
                SET status = 'archived', embedding_status = 'archived', updated_at = ?, valid_to = ?
                WHERE id = ? AND status != 'archived'
            """, (now, now, resolved_id))
            
        return f"Memory '{resolved_id}' was successfully archived."
    except Exception as e:
        logger.error("Error archiving memory: %s", e)
        return f"Error archiving memory: {e}"
    finally:
        if should_close:
            conn.close()

def detect_orphaned_memories(owner_id: str = None, db_connection = None, db_path: str = None) -> dict:
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
        LEFT JOIN relations r ON (e.id = r.source_id OR e.id = r.target_id)
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
            "orphans_detected": len(orphans),
            "details": [{"orphan": o} for o in orphans],
            "orphaned_memories": orphans
        }
    except Exception as e:
        logger.error("Error detecting orphans: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            conn.close()

def check_duplicate_memories(
    title: str = None,
    content: str = None,
    owner_id: str = None,
    tags: list = None,
    project_id: str = None,
    exclude_ids: list = None,
    db_connection = None,
    db_path: str = None
) -> dict:
    """Checks the database for potential near-duplicates of a proposed memory."""
    if not title and not content:
        return {"error": "Either title or content is required"}
        
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        where = ["status != 'archived'"]
        params = []
        
        if exclude_ids:
            clean_excludes = [str(x) for x in exclude_ids if str(x)]
            if clean_excludes:
                placeholders = ",".join("?" for _ in clean_excludes)
                where.append(f"id NOT IN ({placeholders})")
                params.extend(clean_excludes)

        if owner_id:
            where.append("(owner_id = ? OR owner_id IS NULL)")
            params.append(owner_id)
            
        if project_id:
            where.append("(project_id IS NULL OR project_id = ? OR context_id = ?)")
            params.extend([project_id, project_id])
            
        from saltmdb.utils.text import sanitize_fts_query
        input_text = f"{title or ''} {content or ''}"
        duplicates = []
        
        # Pre-filter using FTS5 to reduce candidates from O(N) to ~30 max
        fts_candidates = []
        search_terms = sanitize_fts_query(title or content or "")
        if search_terms:
            try:
                fts_where = " AND ".join(f"e.{c}" for c in where) if where else "1=1"
                fts_rows = conn.execute(
                    f"SELECT e.id, e.title, e.full_content, e.owner_id FROM entities_fts fts "
                    f"JOIN entities e ON fts.id = e.id "
                    f"WHERE entities_fts MATCH ? AND {fts_where} LIMIT 30",
                    [search_terms] + params
                ).fetchall()
                fts_candidates = fts_rows
            except Exception:
                pass
        
        # Fallback to full scan only if FTS returned nothing
        if not fts_candidates:
            cursor = conn.execute(f"SELECT id, title, full_content, owner_id FROM entities WHERE {' AND '.join(where) if where else '1=1'}", params)
            fts_candidates = cursor.fetchall()
        
        for eid, etitle, econtent, eowner in fts_candidates:
            existing_text = f"{etitle} {econtent}"
            sim = word_sim(input_text, existing_text)
            if sim >= 0.25:
                duplicates.append({
                    "id": eid,
                    "title": etitle,
                    "owner_id": eowner,
                    "similarity_score": round(sim, 3)
                })
                
        duplicates.sort(key=lambda x: x["similarity_score"], reverse=True)
        return {
            "duplicate_found": len(duplicates) > 0,
            "potential_duplicates": duplicates
        }
    except Exception as e:
        logger.error("Error checking duplicate memories: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            conn.close()

def scan_memories(
    owner_id: str = None,
    status_filter: str = None,
    limit: int = 20,
    offset: int = 0,
    cursor: str = None,
    db_connection = None,
    db_path: str = None
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
        cursor_obj = conn.execute(f"""
            SELECT id, title, owner_id, status, weight, is_core, updated_at
            FROM entities
            {where_sql}
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset])
        
        rows = cursor_obj.fetchall()
        return [{
            "id": r[0],
            "title": r[1],
            "owner_id": r[2],
            "status": r[3],
            "weight": r[4],
            "is_core": bool(r[5]),
            "updated_at": r[6],
            "cursor": f"offset:{offset + limit}"
        } for r in rows]
    except Exception as e:
        logger.error("Error scanning memories: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            conn.close()

def bulk_archive_memory(archive_requests: list, db_connection = None, db_path: str = None) -> list:
    """Bulk archives memories atomically."""
    if not archive_requests or not isinstance(archive_requests, list):
        return [{"status": "error", "error": "archive_requests must be a non-empty list"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    results = []
    try:
        with conn:
            for req in archive_requests:
                eid = req if isinstance(req, str) else req.get("entity_id")
                owner = req.get("owner_id") if isinstance(req, dict) else None
                res = archive_memory(entity_id=eid, owner_id=owner, db_connection=conn)
                if res.startswith("Error"):
                    results.append({"status": "error", "entity_id": eid, "result": res})
                else:
                    results.append({"status": "success", "entity_id": eid, "result": res})
        return results
    except Exception as e:
        logger.error("Error in bulk archive memory: %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            conn.close()

def get_canonical_tags(domain: str = None, db_connection = None, db_path: str = None) -> list:
    """Queries canonical tags."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        if domain:
            cursor = conn.execute("""
                SELECT id, name FROM tags 
                WHERE canonical_id IS NULL AND name LIKE ?
            """, (f"%{domain}%",))
        else:
            cursor = conn.execute("""
                SELECT id, name FROM tags 
                WHERE canonical_id IS NULL
            """)
        rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        logger.error("Error fetching canonical tags: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            conn.close()
