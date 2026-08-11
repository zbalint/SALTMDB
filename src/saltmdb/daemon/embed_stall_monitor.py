"""Periodic, read-only visibility for stalled asynchronous embedding work.

The daemon shares one long-lived ``_embed_pool``. A wedged worker can leave entities in
``embedding_status='pending'`` without an exception. This monitor periodically reports those
entities so the condition is diagnosable from normal daemon logs.

It deliberately does not terminate the daemon. When the daemon has no sessions or in-flight
RPCs, its established 30-second grace shutdown already ends the process and the next client gets
a fresh pool. Ending a daemon while a client keeps a session open is a separate lifecycle policy
decision, not something a diagnostic monitor may infer.
"""

import logging
import threading

from saltmdb import config

logger = logging.getLogger(__name__)


class EmbedStallMonitor:
    """Owns the periodic stale-pending check for one daemon process."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="saltmdb-embed-stall-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.wait(config.EMBED_STALL_CHECK_INTERVAL_S):
            try:
                self._check_once()
            except Exception:
                logger.exception(
                    "embed-stall-monitor: check failed (non-fatal, will retry next tick)"
                )

    def _check_once(self) -> None:
        """Log one warning whenever an active entity has been pending past the configured age."""
        stale_count, oldest_id, oldest_created_at = self._query_stale_pending()
        retry_wait, expired_leases, terminal_failed = self._query_job_health()
        if stale_count == 0 and retry_wait == 0 and expired_leases == 0 and terminal_failed == 0:
            return

        logger.warning(
            "embed-stall-monitor: %d entities stuck embedding_status='pending'; "
            "retry_wait=%d expired_leases=%d terminal_failed=%d threshold=%ds oldest=%s created_at=%s",
            stale_count,
            retry_wait,
            expired_leases,
            terminal_failed,
            config.EMBED_STALL_PENDING_AGE_THRESHOLD_S,
            oldest_id,
            oldest_created_at,
        )

    def _query_stale_pending(self) -> tuple[int, str | None, str | None]:
        """Compatibility pending-entity view, using a read-only connection."""
        from saltmdb.db.connection import open_read_connection

        conn = open_read_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT id, created_at FROM entities "
                "WHERE embedding_status = 'pending' AND status != 'archived' "
                "AND datetime(created_at) <= datetime('now', ?) "
                "ORDER BY datetime(created_at) ASC",
                (f"-{config.EMBED_STALL_PENDING_AGE_THRESHOLD_S} seconds",),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return 0, None, None
        return len(rows), rows[0][0], rows[0][1]

    def _query_job_health(self) -> tuple[int, int, int]:
        from saltmdb.db.connection import open_read_connection

        conn = open_read_connection(self._db_path)
        try:
            return tuple(
                conn.execute(q).fetchone()[0]
                for q in (
                    "SELECT COUNT(*) FROM embedding_jobs WHERE state='retry_wait'",
                    "SELECT COUNT(*) FROM embedding_jobs WHERE state='running' AND datetime(lease_expires_at) < datetime('now')",
                    "SELECT COUNT(*) FROM embedding_jobs WHERE state='failed'",
                )
            )
        finally:
            conn.close()
