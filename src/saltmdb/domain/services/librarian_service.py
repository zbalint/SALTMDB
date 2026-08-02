import math
import re
import collections
import sys
import os
import sqlite3
import subprocess
import json
import uuid
import logging
from typing import Any
from datetime import datetime, UTC
from concurrent.futures import ThreadPoolExecutor
from saltmdb.config import get_db_path, LIBRARIAN_TRIGGER_COOLDOWN_S
from saltmdb.db.connection import get_connection, write_transaction_retrying, close_connection
from saltmdb.domain.services.memory_service import normalize_tag_name

logger = logging.getLogger(__name__)

# trigger_librarian's cooldown-check + spawn used to run synchronously inline with every
# store_memory/log_event call, adding 1-2 extra write transactions to that call's critical
# path. Offloading it to a single background worker (fire-and-forget, like _embed_pool in
# memory_service.py) keeps that work off the hot path entirely -- callers no longer wait on
# or contend with it. max_workers=1 is deliberate: the cooldown-claim UPDATE below is already
# meant to collapse concurrent triggers to a single winner, so there is never a reason to run
# more than one of these checks at a time.
_librarian_trigger_pool = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="saltmdb-librarian-trigger"
)


def trigger_librarian(db_path: str = None):
    """Fire-and-forget: schedules the cooldown check + subprocess spawn on a background
    thread so this never blocks or adds write-lock contention to the caller's request."""
    if (
        os.environ.get("SALTMDB_DISABLE_LIBRARIAN")
        or os.environ.get("SALTMDB_TEST_MODE")
        or getattr(trigger_librarian, "disabled", False)
    ):
        return
    db_path = db_path or get_db_path()
    _librarian_trigger_pool.submit(_trigger_librarian_impl, db_path)


def _trigger_librarian_impl(db_path: str) -> None:
    """Checks the raw-entity threshold and cooldown, then spawns the librarian subprocess.

    Runs on the background trigger thread (see trigger_librarian). The cooldown claim is a
    single atomic UPDATE on last_run_at guarded by its own WHERE clause, so concurrent
    callers racing here still collapse to exactly one winner -- unlike the previous
    acquire_librarian_lock() + immediate release_librarian_lock() dance, which spent two
    separate BEGIN IMMEDIATE write transactions just to perform this same throttle check.
    locked_at/locked_by_pid are intentionally left untouched here: that field is the
    subprocess's own real leader-election mutex (see db/locks.py, __main__.py), not this
    parent-side throttle.
    """
    try:
        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'raw'")
            raw_count = cursor.fetchone()[0]
            if raw_count < 2:
                return

            def _claim_cooldown(c):
                now = datetime.now(UTC).isoformat()
                cur = c.execute(
                    f"""
                    UPDATE _system_locks
                    SET last_run_at = ?
                    WHERE task_name = 'librarian_consolidation'
                      AND (last_run_at IS NULL OR datetime(last_run_at) < datetime('now', '-{LIBRARIAN_TRIGGER_COOLDOWN_S} seconds'))
                """,
                    (now,),
                )
                return cur.rowcount == 1

            if not write_transaction_retrying(conn, _claim_cooldown):
                return
        finally:
            close_connection(conn)
    except Exception as e:
        logger.debug("Cooldown/lock check exception in trigger_librarian: %s", e)
        return

    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        # Redirect stdout/stderr to librarian.log (same directory as the DB) instead of DEVNULL
        # so Librarian subprocess output/errors are actually visible for debugging, matching the
        # viewer.log redirection precedent in saltmdb/viewer/server.py. Uses the already-resolved
        # local `db_path` (set above via `db_path = db_path or get_db_path()`), NOT a fresh
        # get_db_path() call -- calling get_db_path() again here would silently ignore a caller-
        # supplied non-default db_path and always point at the default ~/.saltmdb directory
        # regardless of which database this invocation is actually operating on.
        log_path = os.path.join(os.path.dirname(db_path), "librarian.log")
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            try:
                os.replace(log_path, f"{log_path}.1")
            except OSError:
                pass
        with open(log_path, "a", encoding="utf-8") as log_f:
            subprocess.Popen(
                [sys.executable, "-m", "saltmdb", "--librarian"],
                stdout=log_f,
                stderr=log_f,
                creationflags=creationflags,
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

        def _write(c):
            cursor = c.execute("SELECT id, name, canonical_id FROM tags")
            tags = cursor.fetchall()

            grouped: dict[str, list[Any]] = {}
            for tag_id, name, canonical_id in tags:
                if canonical_id is not None:
                    continue
                norm = name.lower().strip().replace("-", "").replace("_", "").replace("#", "")
                grouped.setdefault(norm, []).append((tag_id, name))

            for norm, tag_list in grouped.items():
                if len(tag_list) > 1:
                    canonical_id, canonical_name = tag_list[0]
                    logger.info(
                        "Merging tags into canonical tag: '%s' (%s)", canonical_name, canonical_id
                    )
                    for tag_id, name in tag_list[1:]:
                        logger.info("  - Marking alias tag: '%s' (%s)", name, tag_id)
                        c.execute(
                            "UPDATE tags SET canonical_id = ? WHERE id = ?", (canonical_id, tag_id)
                        )
                        c.execute(
                            "UPDATE OR IGNORE entity_tags SET tag_id = ? WHERE tag_id = ?",
                            (canonical_id, tag_id),
                        )
                        c.execute(
                            "DELETE FROM entity_tags WHERE tag_id = ? AND entity_id IN (SELECT entity_id FROM entity_tags WHERE tag_id = ?)",
                            (tag_id, canonical_id),
                        )
                        c.execute(
                            "UPDATE entity_tags SET tag_id = ? WHERE tag_id = ?",
                            (canonical_id, tag_id),
                        )

        write_transaction_retrying(conn, _write)
    finally:
        if should_close:
            close_connection(conn)


def _resolve_tag_id(conn: sqlite3.Connection, tag_name: str):
    """Resolves a tag name to its canonical tag id (case-insensitive, '#'-prefix-tolerant)."""
    if not tag_name:
        return None
    name = normalize_tag_name(tag_name)
    row = conn.execute(
        "SELECT id, canonical_id FROM tags WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if not row:
        return None
    tag_id, canonical_id = row
    return canonical_id if canonical_id else tag_id


def merge_tags(
    keep_tag: str, tags_to_merge: list, conn: sqlite3.Connection = None, db_path: str = None
) -> str:
    """Merges one or more tags into an explicitly chosen canonical tag, repointing entity_tags associations.

    Unlike merge_tags_heuristics (which picks the canonical tag arbitrarily by SQL row order),
    this lets the caller pick which tag name survives as canonical.
    """
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        canonical_id = _resolve_tag_id(conn, keep_tag)
        if not canonical_id:
            return f"Error: keep_tag '{keep_tag}' does not exist in the tags table."

        merged: list[Any] = []
        skipped: list[Any] = []

        def _write(c):
            merged.clear()
            skipped.clear()
            for name in tags_to_merge or []:
                alias_id = _resolve_tag_id(c, name)
                if not alias_id:
                    skipped.append({"tag": name, "reason": "not found"})
                    continue
                if alias_id == canonical_id:
                    skipped.append({"tag": name, "reason": "already canonical"})
                    continue

                c.execute("UPDATE tags SET canonical_id = ? WHERE id = ?", (canonical_id, alias_id))
                c.execute(
                    "UPDATE OR IGNORE entity_tags SET tag_id = ? WHERE tag_id = ?",
                    (canonical_id, alias_id),
                )
                c.execute(
                    "DELETE FROM entity_tags WHERE tag_id = ? AND entity_id IN (SELECT entity_id FROM entity_tags WHERE tag_id = ?)",
                    (alias_id, canonical_id),
                )
                c.execute(
                    "UPDATE entity_tags SET tag_id = ? WHERE tag_id = ?", (canonical_id, alias_id)
                )
                merged.append(name)

        write_transaction_retrying(conn, _write)

        return f"Merged {len(merged)} tag(s) into canonical tag '{keep_tag}': {merged}. Skipped: {skipped}"
    finally:
        if should_close:
            close_connection(conn)


def _pending_request_exists(conn: sqlite3.Connection, target: str, **key_filters) -> bool:
    """True if an unresolved consolidation_request event already covers this target.

    "Unresolved" means at least one entity_id listed in that prior event's content is still
    status='raw' (mirrors the status logic in event_service.get_recent_events). Without this
    guard, every librarian run re-logs an identical event for the same still-unprocessed
    backlog forever -- confirmed in production via librarian.log showing the same
    supersession/tag/cluster candidates re-logged run after run, each one costing its own
    write transaction and growing the events table without bound as a session goes on. This
    keeps the passes idempotent: a target only gets a fresh request once its previous one has
    actually been acted on (or its entity_ids archived).

    key_filters are additional exact-match json_extract($.<field>) conditions (e.g.
    tag_name=..., or owner_id=..., scope=... together) narrowing which prior requests count
    as "the same" target instance; comparisons use IS so a NULL key_value (e.g. no owner_id)
    matches a NULL field correctly instead of silently excluding everything via SQL's
    NULL != NULL semantics.
    """
    extra_clauses = "".join(
        f" AND json_extract(content, '$.{field}') IS ?" for field in key_filters
    )
    rows = conn.execute(
        f"""
        SELECT content FROM events
        WHERE type = 'consolidation_request'
          AND json_extract(content, '$.target') = ?{extra_clauses}
        ORDER BY timestamp DESC LIMIT 5
        """,
        (target, *key_filters.values()),
    ).fetchall()
    for (content_str,) in rows:
        try:
            data = json.loads(content_str)
        except Exception:
            continue
        # "Unresolved" is judged by the raw entities the request is actually waiting on --
        # entity_ids for tag/general/vector_cluster requests, new_raw_entity_ids for
        # supersession_candidate requests (whose consolidated_entity_id is itself never
        # 'raw', so checking that field's status would always read as "resolved").
        entity_ids = data.get("entity_ids") or data.get("new_raw_entity_ids") or []
        if not entity_ids:
            continue
        placeholders = ",".join("?" for _ in entity_ids)
        still_raw = conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE id IN ({placeholders}) AND status = 'raw'",
            entity_ids,
        ).fetchone()[0]
        if still_raw > 0:
            return True
    return False


def _anchor_in_pending_cluster(conn: sqlite3.Connection, anchor_entity_id: str) -> bool:
    """True if anchor_entity_id already appears in an unresolved vector_cluster request's
    entity_ids array. See _pending_request_exists' docstring -- same idempotency rationale,
    but cluster membership lives in a JSON array rather than a scalar field, so it needs a
    containment check instead of an exact json_extract match."""
    rows = conn.execute(
        """
        SELECT content FROM events
        WHERE type = 'consolidation_request'
          AND json_extract(content, '$.target') = 'vector_cluster'
        ORDER BY timestamp DESC LIMIT 20
        """
    ).fetchall()
    for (content_str,) in rows:
        try:
            data = json.loads(content_str)
        except Exception:
            continue
        entity_ids = data.get("entity_ids") or []
        if anchor_entity_id not in entity_ids:
            continue
        placeholders = ",".join("?" for _ in entity_ids)
        still_raw = conn.execute(
            f"SELECT COUNT(*) FROM entities WHERE id IN ({placeholders}) AND status = 'raw'",
            entity_ids,
        ).fetchone()[0]
        if still_raw > 0:
            return True
    return False


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

        to_insert = []
        for tag_id, tag_name, owner_id, count in candidates:
            threshold = 5

            if count < threshold:
                continue

            if _pending_request_exists(conn, "tag", tag_name=tag_name):
                continue

            cursor = conn.execute(
                """
                SELECT e.id FROM entities e
                JOIN entity_tags et ON e.id = et.entity_id
                WHERE et.tag_id = ? AND e.status = 'raw' AND e.owner_id IS ?
            """,
                (tag_id, owner_id),
            )
            raw_ids = [r[0] for r in cursor.fetchall()]

            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            content = json.dumps({"target": "tag", "tag_name": tag_name, "entity_ids": raw_ids})
            target_agent = owner_id if owner_id else "librarian"
            to_insert.append((event_id, now, target_agent, content, tag_name, threshold, raw_ids))

        if not to_insert:
            return

        def _write(c):
            for event_id, now, target_agent, content, *_ in to_insert:
                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """,
                    (event_id, now, target_agent, content),
                )

        write_transaction_retrying(conn, _write)

        for _, _, target_agent, _, tag_name, threshold, raw_ids in to_insert:
            logger.info(
                "Logged consolidation request for tag '%s' (Owner: %s, Threshold: %d, Entity IDs: %s)",
                tag_name,
                target_agent,
                threshold,
                raw_ids,
            )
    finally:
        if should_close:
            close_connection(conn)


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

        groups: dict[Any, list[Any]] = {}
        for eid, owner_id, scope in raw_entities:
            key = (owner_id, scope)
            groups.setdefault(key, []).append(eid)

        to_insert = []
        for (owner_id, scope), entity_ids in groups.items():
            if len(entity_ids) < 5:
                continue

            if _pending_request_exists(conn, "general", owner_id=owner_id, scope=scope):
                continue

            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            content = json.dumps(
                {
                    "target": "general",
                    "owner_id": owner_id,
                    "scope": scope,
                    "entity_ids": entity_ids,
                }
            )
            target_agent = owner_id if owner_id else "librarian"
            to_insert.append((event_id, now, target_agent, content, owner_id, scope, entity_ids))

        if not to_insert:
            return

        def _write(c):
            for event_id, now, target_agent, content, *_ in to_insert:
                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """,
                    (event_id, now, target_agent, content),
                )

        write_transaction_retrying(conn, _write)

        for _, _, _, _, owner_id, scope, entity_ids in to_insert:
            logger.info(
                "Logged general consolidation request for %s/%s (Entity IDs: %s)",
                owner_id,
                scope,
                entity_ids,
            )
    finally:
        if should_close:
            close_connection(conn)


ENGLISH_STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "aren't",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "cannot",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "done",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "with",
    "would",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
}


def _compute_doc_frequencies(
    conn: sqlite3.Connection, stopwords: set[str]
) -> tuple[collections.Counter[str], int]:
    corpus_cursor = conn.execute(
        "SELECT title, full_content FROM entities WHERE status IN ('raw', 'consolidated') LIMIT 100"
    )
    corpus_rows = corpus_cursor.fetchall()
    corpus_doc_count = max(len(corpus_rows), 1)

    doc_frequency: collections.Counter[str] = collections.Counter()
    for title, content in corpus_rows:
        doc_text = f"{title or ''} {content or ''}".lower()
        unique_words = set(re.findall(r"\b[a-z0-9_-]{3,}\b", doc_text)) - stopwords
        for w in unique_words:
            doc_frequency[w] += 1
    return doc_frequency, corpus_doc_count


def extract_c_tfidf_tags(
    conn: sqlite3.Connection, cluster_ids: list[str], top_k: int = 3
) -> tuple[list[str], float]:
    """Extracts top canonical tags for a cluster using c-TF-IDF and returns candidate tags with confidence score."""
    if not cluster_ids:
        return [], 0.0

    placeholders = ",".join("?" for _ in cluster_ids)
    cursor = conn.execute(
        f"SELECT title, full_content FROM entities WHERE id IN ({placeholders})", cluster_ids
    )
    rows = cursor.fetchall()
    if not rows:
        return [], 0.0

    cluster_tokens = []
    for title, content in rows:
        text = f"{title or ''} {content or ''}".lower()
        words = re.findall(r"\b[a-z0-9_-]{3,}\b", text)
        cluster_tokens.extend([w for w in words if w not in ENGLISH_STOPWORDS])

    if not cluster_tokens:
        return [], 0.0

    tf_counts = collections.Counter(cluster_tokens)
    total_cluster_terms = len(cluster_tokens)
    doc_frequency, corpus_doc_count = _compute_doc_frequencies(conn, ENGLISH_STOPWORDS)

    tfidf_scores = {
        term: (count / total_cluster_terms)
        * math.log(1.0 + (corpus_doc_count / (doc_frequency.get(term, 1) + 1.0)))
        for term, count in tf_counts.items()
    }

    sorted_terms = sorted(tfidf_scores.items(), key=lambda x: x[1], reverse=True)
    if not sorted_terms:
        return [], 0.0

    canonical_tags = []
    try:
        from saltmdb.domain.services.memory_service import get_canonical_tags

        canonical_tags = get_canonical_tags(db_connection=conn)
    except Exception as e:
        logger.debug("Failed to fetch canonical tags in extract_c_tfidf_tags: %s", e)

    canonical_map = {}
    for item in canonical_tags:
        if isinstance(item, dict) and "name" in item:
            tname = item["name"]
        elif isinstance(item, str):
            tname = item
        else:
            continue
        canonical_map[tname.lower().lstrip("#")] = tname

    suggested_tags = [
        canonical_map[term.lstrip("#")]
        if term.lstrip("#") in canonical_map
        else f"#{term.lstrip('#')}"
        for term, _ in sorted_terms[:top_k]
    ]

    seen = set()
    final_tags = []
    for t in suggested_tags:
        if t not in seen:
            seen.add(t)
            final_tags.append(t)

    top_score = sorted_terms[0][1]
    confidence_score = round(min(1.0, max(0.5, 0.5 + (top_score * 0.5))), 2)

    return final_tags, confidence_score


def find_connected_vector_clusters(
    valid_ids: list[str],
    vectors: list[Any],
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.75,
) -> list[tuple[list[str], float]]:
    """Discovers vector clusters via connected components on a cosine similarity adjacency graph."""
    if len(vectors) < min_cluster_size:
        return []

    try:
        import numpy as np

        X = np.vstack(vectors)
        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        X_norm = X / norms

        sim_matrix = np.dot(X_norm, X_norm.T)
        adj_matrix = sim_matrix >= similarity_threshold

        n = len(valid_ids)
        visited = [False] * n
        clusters = []

        for i in range(n):
            if visited[i]:
                continue

            component = []
            queue = [i]
            visited[i] = True

            while queue:
                curr = queue.pop(0)
                component.append(curr)
                for neighbor in range(n):
                    if adj_matrix[curr, neighbor] and not visited[neighbor]:
                        visited[neighbor] = True
                        queue.append(neighbor)

            if len(component) >= min_cluster_size:
                comp_ids = [valid_ids[idx] for idx in component]
                sub_sims = sim_matrix[np.ix_(component, component)]
                k = len(component)
                if k > 1:
                    off_diag_sum = float(np.sum(sub_sims) - np.trace(sub_sims))
                    mean_sim = off_diag_sum / (k * (k - 1))
                else:
                    mean_sim = 1.0
                clusters.append((comp_ids, round(mean_sim, 4)))

        return clusters
    except Exception as e:
        logger.warning("Error in find_connected_vector_clusters: %s", e)
        return []


def consolidate_vector_clusters(conn: sqlite3.Connection = None, db_path: str = None):  # noqa: C901, PLR0912, PLR0915
    """Discovers topically related raw memories via vector embeddings and logs consolidation request events."""
    should_close = False
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        logger.info("Running Vector Topic Clustering for Raw Memories...")
        if should_close:
            try:
                import sqlite_vec

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            except Exception as e:
                logger.debug("sqlite_vec extension load skipped or failed: %s", e)
                return

        cursor = conn.execute("""
            SELECT e.id, e.owner_id, ee.embedding
            FROM entities e
            JOIN entity_embeddings ee ON e.id = ee.entity_id
            WHERE e.status = 'raw' AND e.embedding_status = 'ready'
        """)
        raw_rows = cursor.fetchall()
        if len(raw_rows) < 3:
            return

        owner_map = {r[0]: r[1] for r in raw_rows}
        valid_rows = [(r[0], r[2]) for r in raw_rows if r[2]]
        if len(valid_rows) < 3:
            return

        import numpy as np

        valid_ids = [vr[0] for vr in valid_rows]
        vectors = [np.frombuffer(vr[1], dtype=np.float32) for vr in valid_rows]

        cluster_data = find_connected_vector_clusters(
            valid_ids, vectors, min_cluster_size=3, similarity_threshold=0.75
        )

        to_insert = []
        domain_suggestions = []

        for cluster, mean_prob in cluster_data:
            if _anchor_in_pending_cluster(conn, cluster[0]):
                continue

            primary_owner = owner_map.get(cluster[0]) or "librarian"
            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()

            # Extract c-TF-IDF candidate tags and confidence
            suggested_tags, tfidf_conf = extract_c_tfidf_tags(conn, cluster, top_k=3)
            confidence_score = round(0.5 * mean_prob + 0.5 * tfidf_conf, 2)

            content_dict = {
                "target": "vector_cluster",
                "owner_id": primary_owner,
                "entity_ids": cluster,
                "suggested_tags": suggested_tags,
            }
            content = json.dumps(content_dict)
            to_insert.append((event_id, now, primary_owner, content, cluster))

            if suggested_tags:
                sug_event_id = str(uuid.uuid4())
                sug_content = json.dumps(
                    {
                        "cluster_entity_ids": cluster,
                        "suggested_tags": suggested_tags,
                        "confidence_score": confidence_score,
                        "rationale": "Connected components cosine graph cluster with c-TF-IDF term specificity",
                    }
                )
                domain_suggestions.append((sug_event_id, now, primary_owner, sug_content))

        if not to_insert and not domain_suggestions:
            return

        def _write(c):
            for event_id, now, primary_owner, content, *_ in to_insert:
                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """,
                    (event_id, now, primary_owner, content),
                )

            for event_id, now, primary_owner, sug_content in domain_suggestions:
                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'domain_suggestion', ?)
                """,
                    (event_id, now, primary_owner, sug_content),
                )

        write_transaction_retrying(conn, _write)

        for _, _, primary_owner, _, cluster in to_insert:
            logger.info(
                "Logged vector cluster consolidation request for Owner '%s' (Entity IDs: %s)",
                primary_owner,
                cluster,
            )
    except Exception as e:
        logger.warning("Error in consolidate_vector_clusters: %s", e)
    finally:
        if should_close:
            close_connection(conn)


def scout_consolidated_supersessions(conn: sqlite3.Connection = None, db_path: str = None):  # noqa: C901
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

        to_insert = []
        for cid, ctitle, cowner, cvalid_from in consolidated_nodes:
            vec_row = conn.execute(
                "SELECT embedding FROM entity_embeddings WHERE entity_id = ?", (cid,)
            ).fetchone()
            if not vec_row or not vec_row[0]:
                continue

            vec_blob = vec_row[0]
            new_raw_cur = conn.execute(
                """
                SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
                FROM entity_embeddings ee
                JOIN entities e ON ee.entity_id = e.id
                WHERE e.status = 'raw' AND e.created_at > COALESCE(?, '1970-01-01T00:00:00')
                ORDER BY distance ASC
            """,
                (vec_blob, cvalid_from),
            )

            overlapping_new_raw = [row[0] for row in new_raw_cur.fetchall() if row[1] <= 0.25]
            if len(overlapping_new_raw) < 3:
                continue

            if _pending_request_exists(conn, "supersession_candidate", consolidated_entity_id=cid):
                continue

            event_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            target_agent = cowner or "librarian"
            content = json.dumps(
                {
                    "target": "supersession_candidate",
                    "consolidated_entity_id": cid,
                    "consolidated_title": ctitle,
                    "new_raw_entity_ids": overlapping_new_raw,
                }
            )
            to_insert.append((event_id, now, target_agent, content, ctitle, cid))

        if not to_insert:
            return

        def _write(c):
            for event_id, now, target_agent, content, *_ in to_insert:
                c.execute(
                    """
                    INSERT INTO events (id, timestamp, agent_id, type, content)
                    VALUES (?, ?, ?, 'consolidation_request', ?)
                """,
                    (event_id, now, target_agent, content),
                )

        write_transaction_retrying(conn, _write)

        for _, _, _, _, ctitle, cid in to_insert:
            logger.info(
                "Logged supersession candidate request for consolidated memory '%s' (ID: %s)",
                ctitle,
                cid,
            )
    except Exception as e:
        logger.warning("Error in scout_consolidated_supersessions: %s", e)
    finally:
        if should_close:
            close_connection(conn)


def _run_librarian_maintenance(conn) -> None:
    """Checkpoint + optimize maintenance duty. Runs once per Librarian invocation,
    only while the leader lock is held."""
    try:
        cursor = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        row = cursor.fetchone()
        if row:
            busy, log_pages, checkpointed_pages = row
            logger.info(
                "Librarian WAL checkpoint (TRUNCATE): busy=%d, wal_pages=%d, checkpointed_pages=%d",
                busy,
                log_pages,
                checkpointed_pages,
            )
    except Exception as e:
        logger.warning("Librarian WAL checkpoint failed: %s", e)
    try:
        conn.execute("PRAGMA optimize=0x10002;")
        logger.info("Librarian PRAGMA optimize=0x10002 completed.")
    except Exception as e:
        logger.warning("Librarian PRAGMA optimize failed: %s", e)
