import sys
import logging
from saltmdb.config import get_db_path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    if "--librarian" in sys.argv:
        from saltmdb.db.connection import close_connection
        from saltmdb.db.schema import init_db
        from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock
        from saltmdb.domain.services.librarian_service import (
            merge_tags_heuristics,
            consolidate_vector_clusters,
            scout_consolidated_supersessions,
            _run_librarian_maintenance,
        )

        db_path = get_db_path()
        conn = init_db(db_path)
        if not acquire_librarian_lock(conn):
            logger.info("Librarian is already running or locked. Exiting.")
            print("Librarian is already running or locked. Exiting.", flush=True)
            close_connection(conn)
            sys.exit(0)
        try:
            logger.info("Starting SALTMDB Librarian on %s...", db_path)
            merge_tags_heuristics(conn)
            consolidate_vector_clusters(conn)
            scout_consolidated_supersessions(conn)
        finally:
            # Runs unconditionally (even if a consolidation pass above raised) as long as we
            # still hold the leader lock -- checkpoint/optimize maintenance shouldn't be skipped
            # just because one consolidation pass failed.
            _run_librarian_maintenance(conn)
            release_librarian_lock(conn)
            close_connection(conn)
            logger.info("Librarian consolidation complete.")
            print("Librarian consolidation complete.", flush=True)
    else:
        from saltmdb.mcp.server import mcp

        try:
            from saltmdb.domain.services.embedding_service import backfill_pending_embeddings

            count = backfill_pending_embeddings()
            if count > 0:
                logger.info("Queued %d pending entity embeddings for background generation.", count)
        except Exception as e:
            logger.warning("Startup embedding backfill check failed: %s", e)
        mcp.run()


if __name__ == "__main__":
    main()
