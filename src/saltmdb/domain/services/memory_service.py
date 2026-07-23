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
    normalize_search_query
)
from saltmdb.utils.nlp import word_sim
from saltmdb.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)

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
        
    context_id = context_id or project_id
    if not context_id and metadata and isinstance(metadata, dict):
        context_id = metadata.get("project") or metadata.get("project_id")
    project_id = context_id
        
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
                     INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id)
                     SELECT ?, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, 'archived', parent_ids, title, full_content, ?, ?, metadata, project_id, context_id
                     FROM entities WHERE id = ?
                 """, (hist_id, valid_from if valid_from else created_at, now, entity_id))
                 
                 conn.execute("""
                     INSERT INTO entity_tags (entity_id, tag_id)
                     SELECT ?, tag_id FROM entity_tags WHERE entity_id = ?
                 """, (hist_id, entity_id))
                 
            conn.execute("DELETE FROM entity_tags WHERE entity_id = ?", (entity_id,))
            
            metadata_str = json.dumps(metadata) if metadata else None
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, valid_to, metadata, project_id, context_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?, ?, ?, NULL, ?, ?, ?)
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
                    context_id = COALESCE(excluded.context_id, entities.context_id)
            """, (entity_id, now, now, now, owner_id, scope, 1 if is_core else 0, weight, json.dumps([]), title, redacted_content, now, metadata_str, project_id, context_id))
            
            cursor = conn.execute("SELECT id, name, canonical_id FROM tags")
            db_tags = cursor.fetchall()
            tag_lookup = {}
            for tid, tname, tcanon in db_tags:
                norm = tname.strip().lower().lstrip('#')
                norm = re.sub(r'[-_\s]+', '', norm)
                tag_lookup[norm] = tcanon if tcanon else tid

            tags = tags or []
            for tag_name in tags:
                tag_name = tag_name.strip()
                if not tag_name:
                    continue
                if not tag_name.startswith('#'):
                    tag_name = '#' + tag_name
                    
                norm_input = tag_name.lower().lstrip('#')
                norm_input = re.sub(r'[-_\s]+', '', norm_input)
                
                if norm_input in tag_lookup:
                    tag_id = tag_lookup[norm_input]
                else:
                    cursor = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (tag_name,))
                    row = cursor.fetchone()
                    if row:
                        tag_id = row[1] if row[1] else row[0]
                    else:
                        tag_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO tags (id, name, canonical_id) VALUES (?, ?, NULL)", (tag_id, tag_name))
                        tag_lookup[norm_input] = tag_id
                
                conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (entity_id, tag_id))
                
        from saltmdb.domain.services.librarian_service import trigger_librarian
        trigger_librarian(db_path=db_path)

        if db_path:
            import threading
            from saltmdb.domain.services import embedding_service
            threading.Thread(
                target=embedding_service.embed_entity_async,
                args=(entity_id, title, redacted_content, db_path),
                daemon=True
            ).start()

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
    db_connection,
) -> list[tuple[str, float]]:
    """Return [(entity_id, cosine_distance), ...] ascending by distance."""
    try:
        import sqlite_vec
        from saltmdb.domain.services import embedding_service

        db_connection.enable_load_extension(True)
        sqlite_vec.load(db_connection)
        db_connection.enable_load_extension(False)

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
        rows = db_connection.execute(sql, exec_params).fetchall()
        return [(row[0], row[1]) for row in rows]
    except Exception as e:
        logger.warning("Semantic search failed, falling back to FTS5 only: %s", e)
        return []


def reciprocal_rank_fusion(
    fts_results: list,
    semantic_results: list[tuple[str, float]],
    limit: int,
    k: int = 60,
) -> list[str]:
    """Merge two ranked lists by rank position (not raw score)."""
    scores: dict[str, float] = {}
    for rank, row in enumerate(fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (entity_id, _distance) in enumerate(semantic_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return [entity_id for entity_id, _ in ranked[:limit]]


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
    include_related: bool = False,
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
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    fts_future = executor.submit(
                        _run_fts_search, conn, sanitized_query, where_clauses,
                        params, limit, offset
                    )
                    semantic_future = executor.submit(
                        semantic_search, query_keywords, where_clauses, params,
                        limit, conn
                    )
                    fts_rows = fts_future.result()
                    semantic_rows = semantic_future.result()

                merged_ids = reciprocal_rank_fusion(fts_rows, semantic_rows, limit)

                if merged_ids:
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
                    rows = sorted(fetched, key=lambda r: id_order.get(r[0], 9999))
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

        results = []
        for r in rows:
            eid, etitle, econtent, eweight, eis_core, score, created, updated, owner, scope, meta, ctx, rel_c = r
            _, snippet = extract_title_and_snippet(econtent)
            
            item = {
                "id": eid,
                "title": etitle,
                "snippet": snippet,
                "score": round(abs(score), 4),
                "weight": eweight,
                "is_core": bool(eis_core),
                "cursor": f"offset:{offset + limit}"
            }
            if include_related:
                rel_cursor = conn.execute("""
                    SELECT r.predicate, e.id, e.title
                    FROM relations r
                    JOIN entities e ON (r.target_id = e.id OR r.source_id = e.id)
                    WHERE (r.source_id = ? OR r.target_id = ?) AND e.id != ? AND e.status != 'archived'
                    LIMIT 5
                """, (eid, eid, eid))
                item["related_entities"] = [{"predicate": rr[0], "id": rr[1], "title": rr[2]} for rr in rel_cursor.fetchall()]

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
                SET status = 'archived', updated_at = ?, valid_to = ?
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
        if owner_id:
            where.append("(owner_id = ? OR owner_id IS NULL)")
            params.append(owner_id)
            
        if project_id:
            where.append("(project_id IS NULL OR project_id = ? OR context_id = ?)")
            params.extend([project_id, project_id])
            
        cursor = conn.execute(f"SELECT id, title, full_content FROM entities WHERE {' AND '.join(where)}", params)
        rows = cursor.fetchall()
        
        input_text = f"{title or ''} {content or ''}"
        duplicates = []
        
        for eid, etitle, econtent in rows:
            existing_text = f"{etitle} {econtent}"
            sim = word_sim(input_text, existing_text)
            if sim >= 0.25:
                duplicates.append({
                    "id": eid,
                    "title": etitle,
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
