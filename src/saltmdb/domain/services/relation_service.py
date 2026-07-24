import uuid
import json
import logging
from datetime import datetime, UTC
from typing import Literal
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.utils.text import resolve_entity_id, extract_title_and_snippet
from saltmdb.utils.redaction import redact_secrets

logger = logging.getLogger(__name__)

def store_relation(
    source_id: str = None,
    target_id: str = None,
    predicate: str = None,
    db_connection = None,
    db_path: str = None
) -> str:
    """Stores a directional relationship edge between two knowledge entities."""
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
            conn.close()
        return "Error: Could not resolve target entity IDs."
        
    if resolved_source == resolved_target:
        if should_close:
            conn.close()
        return "Error: Self-referential relations (source_id == target_id) are forbidden."
        
    relation_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            conn.execute("""
                INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (relation_id, resolved_source, resolved_target, predicate, now, now))
        return f"Relation successfully stored: '{predicate}' between {resolved_source} and {resolved_target} (ID: {relation_id})"
    except Exception as e:
        logger.error("Error storing relation: %s", e)
        return f"Error storing relation: {e}"
    finally:
        if should_close:
            conn.close()

def analyze_dependencies(
    root_entity_id: str = None,
    max_depth: int = 5,
    db_connection = None,
    db_path: str = None
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
            conn.close()
        return {"error": f"Could not resolve entity '{root_entity_id}'"}
        
    try:
        cursor = conn.execute("SELECT id, title, status FROM entities WHERE id = ?", (root_id,))
        root_row = cursor.fetchone()
        root_info = {"id": root_row[0], "title": root_row[1], "status": root_row[2]} if root_row else {"id": root_id, "title": "Root", "status": "raw"}
        
        query = """
        WITH RECURSIVE dependency_tree(id, source_id, target_id, predicate, depth, path) AS (
            SELECT r.id, r.source_id, r.target_id, r.predicate, 1, r.source_id || '->' || r.target_id
            FROM relations r
            WHERE r.source_id = ? AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
            
            UNION ALL
            
            SELECT r.id, r.source_id, r.target_id, r.predicate, dt.depth + 1, dt.path || '->' || r.target_id
            FROM relations r
            JOIN dependency_tree dt ON r.source_id = dt.target_id
            WHERE dt.depth < ? AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime('now'))
              AND dt.path NOT LIKE '%' || r.target_id || '%'
        )
        SELECT dt.id, dt.source_id, e1.title, dt.target_id, e2.title, dt.predicate, dt.depth, dt.path
        FROM dependency_tree dt
        JOIN entities e1 ON dt.source_id = e1.id
        JOIN entities e2 ON dt.target_id = e2.id
        ORDER BY dt.depth ASC;
        """
        cursor = conn.execute(query, (root_id, max_depth))
        rows = cursor.fetchall()
        
        id_to_title = {root_id: root_info.get("title", root_id)}
        for r in rows:
            id_to_title[r[1]] = r[2]
            id_to_title[r[3]] = r[4]
            
        nodes = [{
            "id": root_id,
            "title": root_info.get("title"),
            "depth": 0,
            "path": root_info.get("title", root_id)
        }]
        seen_nodes = {root_id}
        
        edges = []
        for r in rows:
            rel_id, src_id, src_title, tgt_id, tgt_title, pred, depth, raw_path = r
            formatted_path = " -> ".join(id_to_title.get(part, part) for part in raw_path.split("->"))
            
            if tgt_id not in seen_nodes:
                nodes.append({
                    "id": tgt_id,
                    "title": tgt_title,
                    "depth": depth,
                    "path": formatted_path
                })
                seen_nodes.add(tgt_id)
                
            edges.append({
                "relation_id": rel_id,
                "source_id": src_id,
                "source_title": src_title,
                "target_id": tgt_id,
                "target_title": tgt_title,
                "predicate": pred,
                "depth": depth,
                "path": formatted_path
            })
            
        return {
            "root": root_info,
            "total_dependencies_found": len(edges),
            "graph_exhausted": len(edges) == 0 or max([e["depth"] for e in edges], default=0) < max_depth,
            "dependencies": nodes
        }
    except Exception as e:
        logger.error("Error analyzing dependencies: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            conn.close()

def analyze_lineage(entity_id: str = None, db_connection = None, db_path: str = None) -> dict:
    """Traverses full multi-generation consolidation and derivation ancestry."""
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
            conn.close()
        return {"error": f"Could not resolve entity '{entity_id}'"}
        
    try:
        query = """
        WITH RECURSIVE lineage(id, title, status, parent_ids, depth) AS (
            SELECT id, title, status, parent_ids, 0
            FROM entities WHERE id = ?
            
            UNION ALL
            
            SELECT e.id, e.title, e.status, e.parent_ids, l.depth + 1
            FROM entities e
            JOIN lineage l ON l.parent_ids LIKE '%' || e.id || '%'
            WHERE l.depth < 10
        )
        SELECT id, title, status, parent_ids, depth FROM lineage ORDER BY depth ASC;
        """
        cursor = conn.execute(query, (target_id,))
        rows = cursor.fetchall()
        
        ancestry = []
        for r in rows:
            ancestry.append({
                "id": r[0],
                "title": r[1],
                "status": r[2],
                "parent_ids": json.loads(r[3]) if r[3] else [],
                "generation_depth": r[4]
            })
            
        return {
            "entity_id": target_id,
            "total_ancestors": max(len(ancestry) - 1, 0),
            "ancestry_tree": ancestry,
            "ancestors": ancestry
        }
    except Exception as e:
        logger.error("Error analyzing lineage: %s", e)
        return {"error": str(e)}
    finally:
        if should_close:
            conn.close()

def commit_consolidation(
    parent_ids: list[str],
    title: str,
    content: str,
    tags: list[str] = None,
    scope: Literal['private', 'shared'] = "shared",
    weight: int = 1,
    owner_id: str = None,
    context_id: str = None,
    db_connection = None,
    db_path: str = None
) -> str:
    """Commits a consolidated memory synthesized by the agent, atomically archiving the raw parents and repointing relations."""
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
        
    resolved_parents = []
    for p in parent_ids:
        res = resolve_entity_id(conn, str(p))
        if res:
            resolved_parents.append(res)
            
    if not resolved_parents:
        if should_close:
            conn.close()
        return "Error: None of the provided parent_ids could be resolved."
        
    redacted_content = redact_secrets(content)
    clean_title = redact_secrets(title)
    consolidated_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    
    owner_val = owner_id or 'system'
    try:
        with conn:
            conn.execute("""
                INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope, is_core, weight, status, parent_ids, title, full_content, valid_from, context_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'consolidated', ?, ?, ?, ?, ?)
            """, (consolidated_id, now, now, now, owner_val, scope, weight, json.dumps(resolved_parents), clean_title, redacted_content, now, context_id))
            
            if tags:
                for tag_name in tags:
                    tag_name = tag_name.strip()
                    if not tag_name:
                        continue
                    if not tag_name.startswith('#'):
                        tag_name = '#' + tag_name
                    cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
                    row = cursor.fetchone()
                    if row:
                        tag_id = row[0]
                    else:
                        tag_id = str(uuid.uuid4())
                        conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
                    conn.execute("INSERT OR IGNORE INTO entity_tags (entity_id, tag_id) VALUES (?, ?)", (consolidated_id, tag_id))
                    
            placeholders = ",".join("?" for _ in resolved_parents)
            conn.execute(f"""
                UPDATE entities 
                SET status = 'archived', embedding_status = 'archived', updated_at = ?, valid_to = ? 
                WHERE id IN ({placeholders})
            """, [now, now] + resolved_parents)
            
            conn.execute(f"UPDATE relations SET source_id = ? WHERE source_id IN ({placeholders})", [consolidated_id] + resolved_parents)
            conn.execute(f"UPDATE relations SET target_id = ? WHERE target_id IN ({placeholders})", [consolidated_id] + resolved_parents)
            
            for parent_id in resolved_parents:
                rel_id = str(uuid.uuid4())
                conn.execute("""
                    INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from)
                    VALUES (?, ?, ?, 'consolidated_from', ?, ?)
                """, (rel_id, consolidated_id, parent_id, now, now))
                
        return f"Successfully committed consolidated memory with ID: {consolidated_id}"
    except Exception as e:
        logger.error("Error committing consolidation: %s", e)
        return f"Error committing consolidation: {e}"
    finally:
        if should_close:
            conn.close()

def bulk_commit_consolidation(consolidations: list, db_connection = None, db_path: str = None) -> list:
    """Executes multiple consolidation commits atomically in a single transaction."""
    if not consolidations or not isinstance(consolidations, list):
        return [{"status": "error", "error": "consolidations must be a non-empty array of objects"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    results = []
    try:
        with conn:
            for item in consolidations:
                p_ids = item.get("parent_ids", [])
                t = item.get("title")
                c = item.get("content")
                tags = item.get("tags", [])
                scope = item.get("scope", "shared")
                w = item.get("weight", 1)
                
                res = commit_consolidation(parent_ids=p_ids, title=t, content=c, tags=tags, scope=scope, weight=w, db_connection=conn)
                if res.startswith("Error"):
                    results.append({"status": "error", "title": t, "result": res})
                else:
                    new_id = res.split("ID: ")[-1].strip()
                    results.append({"status": "success", "entity_id": new_id, "title": t, "result": res})
        return results
    except Exception as e:
        logger.error("Bulk commit consolidation error: %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            conn.close()

def bulk_store_relations(relations: list, db_connection = None, db_path: str = None) -> list:
    """Executes multiple relation insertions atomically in a single transaction."""
    if not relations or not isinstance(relations, list):
        return [{"status": "error", "error": "relations must be a non-empty array of objects"}]
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    results = []
    try:
        with conn:
            for r in relations:
                src = r.get("source_id")
                tgt = r.get("target_id")
                pred = r.get("predicate")
                res = store_relation(source_id=src, target_id=tgt, predicate=pred, db_connection=conn)
                if res.startswith("Error"):
                    results.append({"status": "error", "source": src, "target": tgt, "predicate": pred, "result": res})
                else:
                    results.append({"status": "success", "source": src, "target": tgt, "predicate": pred, "result": res})
        return results
    except Exception as e:
        logger.error("Bulk store relations error: %s", e)
        return [{"status": "error", "error": str(e)}]
    finally:
        if should_close:
            conn.close()
