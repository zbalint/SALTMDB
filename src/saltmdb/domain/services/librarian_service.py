import sys
import os
import sqlite3
import subprocess
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
