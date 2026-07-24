import sys
import logging
from saltmdb.config import get_db_path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

def main():
    if "--librarian" in sys.argv:
        from saltmdb.db.connection import get_connection
        from saltmdb.db.schema import init_db
        from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock
        from saltmdb.domain.services.librarian_service import (
            merge_tags_heuristics,
            consolidate_cluttered_tags,
            consolidate_memories,
            consolidate_vector_clusters,
            scout_consolidated_supersessions
        )
        db_path = get_db_path()
        conn = init_db(db_path)
        if not acquire_librarian_lock(conn):
            logger.info("Librarian is already running or locked. Exiting.")
            print("Librarian is already running or locked. Exiting.", flush=True)
            conn.close()
            sys.exit(0)
        try:
            logger.info("Starting SALTMDB Librarian on %s...", db_path)
            merge_tags_heuristics(conn)
            consolidate_cluttered_tags(conn)
            consolidate_memories(conn)
            consolidate_vector_clusters(conn)
            scout_consolidated_supersessions(conn)
        finally:
            release_librarian_lock(conn)
            conn.close()
            logger.info("Librarian consolidation complete.")
            print("Librarian consolidation complete.", flush=True)
    else:
        from saltmdb.mcp.server import mcp
        import saltmdb.mcp.tools  # Register all MCP tools
        mcp.run()

if __name__ == "__main__":
    main()
