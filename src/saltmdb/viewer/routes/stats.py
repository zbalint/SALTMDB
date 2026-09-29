"""Aggregate health/quality endpoints.

GET /api/stats, GET /api/operations, GET /api/quality, GET /api/embeddings_stats,
plus the shared _collect_stats() helper they both use.
"""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from saltmdb.db.vector_schema import try_load_vector_extension
from saltmdb.viewer.routes._shared import MAX_ENTITY_LIMIT, _bounded_query_int
from saltmdb.viewer.routes.sessions import _daemon_liveness, _load_sessions

if TYPE_CHECKING:
    from saltmdb.viewer.routes._protocol import ViewerHandlerProtocol
else:
    ViewerHandlerProtocol = object

logger = logging.getLogger(__name__)


def _latest_snapshot(db_path: str | None) -> dict | None:
    """Newest ``backups/saltmdb_snapshot_*.db`` next to the database (the layout backup.py writes)."""
    if not db_path:
        return None
    snapshots = sorted((Path(db_path).parent / "backups").glob("saltmdb_snapshot_*.db"))
    if not snapshots:
        return None
    newest = snapshots[-1]  # names embed a UTC timestamp, so lexical order is chronological
    info = newest.stat()
    return {
        "name": newest.name,
        "bytes": info.st_size,
        "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
    }


class StatsMixin(ViewerHandlerProtocol):
    """Provides _collect_stats/get_stats/get_operations/get_quality/get_embeddings_stats."""

    def _collect_stats(self, conn):
        """Collect the legacy stats payload from a caller-owned read connection."""
        stats = {}
        for status in ["raw", "consolidated", "archived"]:
            cur = conn.execute("SELECT COUNT(*) FROM entities WHERE status = ?", (status,))
            stats[f"{status}_count"] = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM entities")
        stats["total_entities"] = cur.fetchone()[0]
        stats["active_entities"] = stats["raw_count"] + stats["consolidated_count"]

        for scope in ["shared", "private"]:
            cur = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE scope = ? AND status != 'archived'",
                (scope,),
            )
            stats[f"scope_{scope}"] = cur.fetchone()[0]

        cur = conn.execute("SELECT COUNT(*) FROM events")
        stats["total_events"] = cur.fetchone()[0]
        cur = conn.execute(
            "SELECT COUNT(*) FROM events WHERE datetime(timestamp) >= datetime('now', '-24 hours')"
        )
        stats["events_last_24h"] = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM conversation_traces")
        stats["total_traces"] = cur.fetchone()[0]
        cur = conn.execute(
            "SELECT COUNT(*) FROM conversation_traces "
            "WHERE datetime(created_at) >= datetime('now', '-24 hours')"
        )
        stats["traces_last_24h"] = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM relations")
        stats["total_relations"] = cur.fetchone()[0]
        cur = conn.execute(
            """SELECT COUNT(*) FROM entities e WHERE e.status = 'raw'
               AND NOT EXISTS (
                   SELECT 1 FROM relations r WHERE r.source_id = e.id OR r.target_id = e.id
               )"""
        )
        stats["orphan_raw_count"] = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM tags")
        stats["total_tags"] = cur.fetchone()[0]
        for emb_status in ["ready", "pending", "failed"]:
            cur = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE embedding_status = ? AND status != 'archived'",
                (emb_status,),
            )
            stats[f"embeddings_{emb_status}"] = cur.fetchone()[0]

        gateway = getattr(self.server, "viewer_gateway", None)
        db_path = getattr(gateway, "db_path", None)
        stats["db_size_bytes"] = (
            os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
        )
        stats["db_size_mb"] = round(stats["db_size_bytes"] / (1024 * 1024), 2)

        active_session_ids, liveness_known = _daemon_liveness(self.server)
        sessions = _load_sessions(conn, active_session_ids, liveness_known)
        stats["total_agent_sessions"] = len(sessions)
        stats["agent_session_liveness_available"] = liveness_known
        stats["active_agent_sessions"] = (
            sum(session["session_id"] in active_session_ids for session in sessions)
            if liveness_known
            else None
        )
        return stats

    def get_stats(self):
        conn = None
        try:
            conn = self.get_db_connection()
            self.send_json(self._collect_stats(conn))
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()

    def get_operations(self):
        """Expose a truthful point-in-time daemon and database health snapshot."""
        conn = None
        try:
            state = getattr(self.server, "daemon_state", None)
            if state is None:
                self.send_json({"error": "Daemon snapshot unavailable"}, 503)
                return
            from saltmdb import __version__

            conn = self.get_db_connection()
            stats = self._collect_stats(conn)
            db_path = getattr(getattr(self.server, "viewer_gateway", None), "db_path", None)
            files = {}
            for label, suffix in (
                ("db_bytes", ""),
                ("wal_bytes", "-wal"),
                ("shm_bytes", "-shm"),
            ):
                path = f"{db_path}{suffix}" if db_path else ""
                files[label] = os.path.getsize(path) if path and os.path.exists(path) else 0
            snapshot = state.viewer_snapshot()
            snapshot["version"] = __version__
            vector_available = try_load_vector_extension(conn)
            warnings = []
            if not snapshot.get("ready", True):
                warnings.append({"code": "DAEMON_NOT_READY", "message": "The daemon is not ready."})
            if not vector_available:
                warnings.append(
                    {"code": "VECTOR_UNAVAILABLE", "message": "Vector search is unavailable."}
                )
            if stats["embeddings_failed"]:
                warnings.append(
                    {
                        "code": "EMBEDDINGS_FAILED",
                        "message": f"{stats['embeddings_failed']} memories failed embedding.",
                    }
                )
            self.send_json(
                {
                    "api_version": 1,
                    "daemon": snapshot,
                    "database": {
                        "stats": stats,
                        "files": files,
                        "sqlite": {
                            "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
                            "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
                        },
                        "vector": {"available": vector_available},
                        "latest_snapshot": _latest_snapshot(db_path),
                        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
                    },
                    "warnings": warnings,
                }
            )
        except Exception as e:
            logger.error("SALTMDB Operations snapshot error: %s", e, exc_info=True)
            self.send_json({"error": "Operations snapshot unavailable"}, 503)
        finally:
            if conn:
                conn.close()

    def get_quality(self, query):
        """Return durable data-quality signals without inventing historical telemetry."""
        conn = None
        try:
            limit = _bounded_query_int(query, "limit", 50, 1, MAX_ENTITY_LIMIT)
            page = _bounded_query_int(query, "page", 1, 1, 1_000_000)
            conn = self.get_db_connection()
            items_where = """status != 'archived'
                   AND (embedding_status IN ('pending', 'failed') OR quality_status IS NOT NULL)"""
            items_params: list = []
            for column in ("embedding_status", "quality_status"):
                value = query.get(column, [None])[0]
                if value:
                    items_where += f" AND {column} = ?"
                    items_params.append(value)
            items_total = conn.execute(
                f"SELECT COUNT(*) FROM entities WHERE {items_where}", items_params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT id, title, status, embedding_status, quality_score, quality_status, quality_flags
                   FROM entities WHERE {items_where}
                   ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?""",
                [*items_params, limit, (page - 1) * limit],
            ).fetchall()
            orphan_from = """FROM entities e LEFT JOIN relations r
                   ON r.source_id = e.id OR r.target_id = e.id
                   WHERE e.status = 'raw' GROUP BY e.id HAVING COUNT(r.id) = 0"""
            orphan_raw_total = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT e.id {orphan_from})"
            ).fetchone()[0]
            orphan_rows = conn.execute(
                f"""SELECT e.id, e.title {orphan_from}
                   ORDER BY e.updated_at DESC, e.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            self.send_json(
                {
                    "items": [
                        {
                            "id": r[0],
                            "title": r[1],
                            "status": r[2],
                            "embedding_status": r[3],
                            "quality_score": r[4],
                            "quality_status": r[5],
                            "quality_flags": json.loads(r[6]) if r[6] else [],
                        }
                        for r in rows
                    ],
                    "orphan_raw": [{"id": r[0], "title": r[1]} for r in orphan_rows],
                    "page": page,
                    "limit": limit,
                    "items_total": items_total,
                    "total_pages": (items_total + limit - 1) // limit,
                    "orphan_raw_total": orphan_raw_total,
                }
            )
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error("SALTMDB Quality snapshot error: %s", e, exc_info=True)
            self.send_json({"error": "Quality snapshot unavailable"}, 500)
        finally:
            if conn:
                conn.close()

    def get_embeddings_stats(self):
        conn = None
        try:
            conn = self.get_db_connection()
            counts = {}
            for emb_status in ["pending", "ready", "failed"]:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE embedding_status = ? AND status != 'archived'",
                    (emb_status,),
                )
                counts[emb_status] = cur.fetchone()[0]
            cur = conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'archived'")
            counts["archived"] = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE (embedding_status IS NULL OR embedding_status = '') AND status != 'archived'"
            )
            counts["null"] = cur.fetchone()[0]
            self.send_json(counts)
        except Exception as e:
            logger.error("SALTMDB Viewer handler error: %s", e, exc_info=True)
            self.send_json({"error": "Internal server error. Check viewer logs for details."}, 500)
        finally:
            if conn:
                conn.close()
