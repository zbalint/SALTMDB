import sys
import logging
from saltmdb.config import get_db_path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():  # noqa: PLR0915
    if "--backfill-chunk-embeddings" in sys.argv:
        # Manual, on-demand entry point for ops use (see plans/ and SALTMDB memory `5c09effa`):
        # populates/repairs entity_chunk_embeddings against a real DB outside the normal startup
        # sweep below -- e.g. to force an immediate repair pass without restarting the server.
        # As of Phase 2 Part A, this table IS wired into the live write path (store_memory,
        # commit_consolidation) and is also swept unconditionally at normal server startup (see
        # the mcp.run() branch below) -- this flag is a convenience, not the only way it runs.
        # This is a local process flag, not an addition to the MCP tool surface.
        from saltmdb.db.schema import init_db
        from saltmdb.domain.services.embedding_service import backfill_chunk_embeddings

        db_path = get_db_path()
        conn = init_db(db_path)
        conn.close()
        logger.info("Starting entity_chunk_embeddings backfill on %s...", db_path)
        count = backfill_chunk_embeddings(db_path)
        logger.info("Chunk-embedding backfill complete: %d entities written.", count)
        print(f"Chunk-embedding backfill complete: {count} entities written.", flush=True)
    elif "--librarian" in sys.argv:
        from saltmdb.db.connection import close_connection
        from saltmdb.db.schema import init_db
        from saltmdb.db.locks import acquire_librarian_lock, release_librarian_lock
        from saltmdb.domain.services.librarian_service import (
            merge_tags_heuristics,
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
            # Track A store-time disposition rewrite (see
            # scratch/plans/track_a_disposition_detailed.md §1.1/§6): consolidate_vector_clusters
            # and scout_consolidated_supersessions retired outright, no replacement scan --
            # merge_tags_heuristics is Librarian's only remaining consolidation-adjacent job.
            merge_tags_heuristics(conn)
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

        try:
            # Part A3 (chunk-embedding freshness lifecycle): unconditional, synchronous repair
            # sweep over entity_chunk_embeddings -- self-heals anything an async _embed_pool job
            # (store_memory's/commit_consolidation's chunk-write trigger) never completed or
            # completed incorrectly, plus any Foundation-era stale rows this DB was carrying
            # before Part A0's content_hash column existed to detect staleness at all. Runs
            # synchronously per user decision, matching backfill_chunk_embeddings' existing
            # contract; log-and-continue on failure, same shape as the block above.
            from saltmdb.domain.services.embedding_service import backfill_chunk_embeddings

            chunk_count = backfill_chunk_embeddings()
            if chunk_count > 0:
                logger.info(
                    "Chunk-embedding startup sweep repaired/backfilled %d entities.", chunk_count
                )
        except Exception as e:
            logger.warning("Startup chunk-embedding backfill sweep failed: %s", e)

        mcp.run()


if __name__ == "__main__":
    main()
