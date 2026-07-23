import sys
import os
import sqlite3
import subprocess
import json
import uuid
import logging
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock

logger = logging.getLogger(__name__)

def trigger_librarian(db_path: str = None):
    """Asynchronously spawns the librarian consolidation process if threshold is met and cooldown has expired."""
    if os.environ.get("SALTMDB_DISABLE_LIBRARIAN") or os.environ.get("SALTMDB_TEST_MODE") or getattr(trigger_librarian, "disabled", False):
        return
        
    db_path = db_path or get_db_path()
    try:
        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'raw'")
            raw_count = cursor.fetchone()[0]
            if raw_count < 2:
                conn.close()
                return
                
            cursor = conn.execute("SELECT last_run_at FROM _system_locks WHERE task_name = 'librarian_consolidation'")
            row = cursor.fetchone()
            if row and row[0]:
                last_run_str = row[0].replace("Z", "")
                if "+" in last_run_str:
                    last_run_str = last_run_str.split("+")[0]
                last_run = datetime.fromisoformat(last_run_str)
                elapsed = (datetime.now(UTC).replace(tzinfo=None) - last_run).total_seconds()
                if elapsed < 300:
                    conn.close()
                    return
                    
            if not acquire_librarian_lock(conn):
                return
                
            release_librarian_lock(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("Cooldown/lock check exception in trigger_librarian: %s", e)
        return

    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
            
        subprocess.Popen(
            [sys.executable, "-m", "saltmdb", "--librarian"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
    except Exception as e:
        logger.warning("Failed to spawn librarian subprocess: %s", e)

def merge_tags_heuristics(conn: sqlite3.Connection = None, db_path: str = None):
    """Scans tags to merge duplicate and near-identical names to prevent folksonomy fragmentation."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        logger.info("Running Tag Merging...")
        with conn:
            cursor = conn.execute("SELECT id, name, canonical_id FROM tags")
            tags = cursor.fetchall()
            
            grouped = {}
            for tag_id, name, canonical_id in tags:
                if canonical_id is not None:
                    continue
                norm = name.lower().strip().replace("-", "").replace("_", "").replace("#", "")
                grouped.setdefault(norm, []).append((tag_id, name))
                
            for norm, tag_list in grouped.items():
                if len(tag_list) > 1:
                    canonical_id, canonical_name = tag_list[0]
                    logger.info("Merging tags into canonical tag: '%s' (%s)", canonical_name, canonical_id)
                    for tag_id, name in tag_list[1:]:
                        logger.info("  - Marking alias tag: '%s' (%s)", name, tag_id)
                        conn.execute("UPDATE tags SET canonical_id = ? WHERE id = ?", (canonical_id, tag_id))
                        conn.execute("UPDATE OR IGNORE entity_tags SET tag_id = ? WHERE tag_id = ?", (canonical_id, tag_id))
                        conn.execute("DELETE FROM entity_tags WHERE tag_id = ? AND entity_id IN (SELECT entity_id FROM entity_tags WHERE tag_id = ?)", (tag_id, canonical_id))
                        conn.execute("UPDATE entity_tags SET tag_id = ? WHERE tag_id = ?", (canonical_id, tag_id))
    finally:
        if should_close:
            conn.close()

def decay_lru_memories(conn: sqlite3.Connection = None, db_path: str = None):
    """[DEPRECATED / REMOVED PER DESIGN PRINCIPLES]"""
    pass

def consolidate_cluttered_tags(conn: sqlite3.Connection = None, db_path: str = None):
    """Scans for tags with 5 or more raw entries per owner and logs a consolidation request event."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        logger.info("Checking for high tag density clutter...")
        cursor = conn.execute("""
            SELECT et.tag_id, t.name, e.owner_id, COUNT(*) 
            FROM entity_tags et
            JOIN entities e ON et.entity_id = e.id
            JOIN tags t ON et.tag_id = t.id
            WHERE e.status = 'raw'
            GROUP BY et.tag_id, t.name, e.owner_id
        """)
        candidates = cursor.fetchall()
        
        for tag_id, tag_name, owner_id, count in candidates:
            is_high_hygiene = any(word in tag_name.lower() for word in ["runbook", "decision"])
            threshold = 3 if is_high_hygiene else 5
            
            if count < threshold:
                continue
                
            cursor = conn.execute("""
                SELECT e.id FROM entities e
                JOIN entity_tags et ON e.id = et.entity_id
                WHERE et.tag_id = ? AND e.status = 'raw' AND e.owner_id IS ?
            """, (tag_id, owner_id))
            raw_ids = [r[0] for r in cursor.fetchall()]
            
            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            content = json.dumps({
                "target": "tag",
                "tag_name": tag_name,
                "entity_ids": raw_ids
            })
            
            target_agent = owner_id if owner_id else "librarian"
            with conn:
                conn.execute("""
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """, (event_id, now, target_agent, content))
            logger.info("Logged consolidation request for tag '%s' (Owner: %s, Threshold: %d, Entity IDs: %s)", tag_name, target_agent, threshold, raw_ids)
    finally:
        if should_close:
            conn.close()

def consolidate_memories(conn: sqlite3.Connection = None, db_path: str = None):
    """General consolidator that groups raw memories by owner/scope and logs general consolidation request events."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        logger.info("Running General Memory Consolidation...")
        cursor = conn.execute("""
            SELECT e.id, e.owner_id, e.scope
            FROM entities e
            WHERE e.status = 'raw'
        """)
        raw_entities = cursor.fetchall()
        if not raw_entities:
            logger.info("No raw memories to consolidate.")
            return
            
        logger.info("Found %d raw memories for general consolidation.", len(raw_entities))
        
        groups = {}
        for eid, owner_id, scope in raw_entities:
            key = (owner_id, scope)
            groups.setdefault(key, []).append(eid)
            
        for (owner_id, scope), entity_ids in groups.items():
            if len(entity_ids) < 5:
                continue
                
            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            content = json.dumps({
                "target": "general",
                "owner_id": owner_id,
                "scope": scope,
                "entity_ids": entity_ids
            })
            target_agent = owner_id if owner_id else "librarian"
            with conn:
                conn.execute("""
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """, (event_id, now, target_agent, content))
            logger.info("Logged general consolidation request for %s/%s (Entity IDs: %s)", owner_id, scope, entity_ids)
    finally:
        if should_close:
            conn.close()

def consolidate_vector_clusters(conn: sqlite3.Connection = None, db_path: str = None):
    """Discovers topically related raw memories via vector embeddings and logs consolidation request events."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True
        
    try:
        logger.info("Running Vector Topic Clustering for Raw Memories...")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as e:
            logger.debug("sqlite-vec extension not available for vector clustering: %s", e)
            return

        cursor = conn.execute("""
            SELECT e.id, e.owner_id
            FROM entities e
            JOIN entity_embeddings ee ON e.id = ee.entity_id
            WHERE e.status = 'raw' AND e.embedding_status = 'ready'
        """)
        raw_rows = cursor.fetchall()
        if len(raw_rows) < 3:
            return

        raw_ids = [r[0] for r in raw_rows]
        owner_map = {r[0]: r[1] for r in raw_rows}
        
        clusters = []
        visited = set()

        for eid in raw_ids:
            if eid in visited:
                continue

            query_vec_cur = conn.execute("SELECT embedding FROM entity_embeddings WHERE entity_id = ?", (eid,))
            vec_row = query_vec_cur.fetchone()
            if not vec_row or not vec_row[0]:
                continue

            vec_blob = vec_row[0]
            placeholders = ",".join("?" for _ in raw_ids)
            sql = f"""
                SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
                FROM entity_embeddings ee
                JOIN entities e ON ee.entity_id = e.id
                WHERE e.id IN ({placeholders}) AND e.status = 'raw'
                ORDER BY distance ASC
            """
            neighbors_cur = conn.execute(sql, [vec_blob] + raw_ids)
            cluster_members = []
            for nid, dist in neighbors_cur.fetchall():
                if dist <= 0.25:  # Cosine distance <= 0.25 means cosine similarity >= 0.75
                    cluster_members.append(nid)

            if len(cluster_members) >= 3:
                clusters.append(cluster_members)
                visited.update(cluster_members)

        for cluster in clusters:
            primary_owner = owner_map.get(cluster[0]) or "librarian"
            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            content = json.dumps({
                "target": "vector_cluster",
                "owner_id": primary_owner,
                "entity_ids": cluster
            })
            with conn:
                conn.execute("""
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """, (event_id, now, primary_owner, content))
            logger.info("Logged vector cluster consolidation request for Owner '%s' (Entity IDs: %s)", primary_owner, cluster)
    except Exception as e:
        logger.warning("Error in consolidate_vector_clusters: %s", e)
    finally:
        if should_close:
            conn.close()

def scout_consolidated_supersessions(conn: sqlite3.Connection = None, db_path: str = None):
    """Scouts for consolidated entities that may be outdated due to new raw memories."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        logger.info("Scouting Consolidated Memories for Supersession Candidates...")
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception as e:
            logger.debug("sqlite-vec extension not available for supersession scouting: %s", e)
            return

        consolidated_cur = conn.execute("""
            SELECT e.id, e.title, e.owner_id, e.valid_from
            FROM entities e
            JOIN entity_embeddings ee ON e.id = ee.entity_id
            WHERE e.status = 'consolidated' AND e.embedding_status = 'ready'
        """)
        consolidated_nodes = consolidated_cur.fetchall()
        if not consolidated_nodes:
            return

        for cid, ctitle, cowner, cvalid_from in consolidated_nodes:
            vec_row = conn.execute("SELECT embedding FROM entity_embeddings WHERE entity_id = ?", (cid,)).fetchone()
            if not vec_row or not vec_row[0]:
                continue

            vec_blob = vec_row[0]
            new_raw_cur = conn.execute("""
                SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
                FROM entity_embeddings ee
                JOIN entities e ON ee.entity_id = e.id
                WHERE e.status = 'raw' AND e.created_at > COALESCE(?, '1970-01-01T00:00:00')
                ORDER BY distance ASC
            """, (vec_blob, cvalid_from))

            overlapping_new_raw = [row[0] for row in new_raw_cur.fetchall() if row[1] <= 0.25]
            if len(overlapping_new_raw) >= 3:
                event_id = str(uuid.uuid4())
                now = datetime.now(UTC).isoformat()
                target_agent = cowner or "librarian"
                content = json.dumps({
                    "target": "supersession_candidate",
                    "consolidated_entity_id": cid,
                    "consolidated_title": ctitle,
                    "new_raw_entity_ids": overlapping_new_raw
                })
                with conn:
                    conn.execute("""
                        INSERT INTO events (id, timestamp, agent_id, type, content)
                        VALUES (?, ?, ?, 'consolidation_request', ?)
                    """, (event_id, now, target_agent, content))
                logger.info("Logged supersession candidate request for consolidated memory '%s' (ID: %s)", ctitle, cid)
    except Exception as e:
        logger.warning("Error in scout_consolidated_supersessions: %s", e)
    finally:
        if should_close:
            conn.close()

