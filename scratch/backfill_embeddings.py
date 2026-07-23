"""
One-time backfill: generate embeddings for all entities where embedding_status='pending'.

Run after deploying Phase 4 (write-path embedding generation) and before enabling
the read-path (SALTMDB_ENABLE_SEMANTIC=true). Processes in batches of 10 threads.

Usage (from repo root):
    python scratch/backfill_embeddings.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection
from saltmdb.domain.services.embedding_service import embed_entity_async

BATCH_SIZE = 10


def backfill():
    db_path = get_db_path()
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT id, title, full_content FROM entities "
        "WHERE embedding_status = 'pending' AND status != 'archived'"
    ).fetchall()
    conn.close()

    print(f"Found {len(rows)} entities to backfill.")
    pending = list(rows)
    while pending:
        batch, pending = pending[:BATCH_SIZE], pending[BATCH_SIZE:]
        threads = [
            threading.Thread(target=embed_entity_async, args=(eid, title, content, db_path))
            for eid, title, content in batch
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        print(f"  Processed batch of {len(batch)}. Remaining: {len(pending)}")

    print("Backfill complete.")


if __name__ == "__main__":
    backfill()
