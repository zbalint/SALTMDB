import os
import sqlite3
from contextlib import suppress
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import open_read_connection, close_connection


def create_snapshot(db_path: str | None = None, dest_dir: str | None = None) -> str:
    """Create a timestamped snapshot with SQLite's backup API and return its absolute path.

    The order is fixed because cleanup deletes files (SPEC-DB-SNAPSHOT-CLI D3): the target is
    checked against the database and created exclusively before the copy, so cleanup only ever
    removes a file this call created. Raises on failure.
    """
    db_path = db_path or get_db_path()
    dest_dir = dest_dir or os.path.join(os.path.dirname(db_path), "backups")
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(os.path.realpath(dest_dir), f"saltmdb_snapshot_{timestamp}.db")
    if backup_path == os.path.realpath(db_path):
        raise ValueError(f"snapshot target is the database itself: {backup_path}")

    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    os.close(os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))

    src_conn = None
    dest_conn = None
    try:
        src_conn = open_read_connection(db_path)
        dest_conn = sqlite3.connect(backup_path)
        src_conn.backup(dest_conn)
        dest_conn.execute("PRAGMA journal_mode=DELETE;")
    except BaseException:
        if dest_conn is not None:
            close_connection(dest_conn)
            dest_conn = None
        for path in (backup_path, backup_path + "-journal"):
            with suppress(FileNotFoundError):
                os.remove(path)
        raise
    finally:
        if dest_conn is not None:
            close_connection(dest_conn)
        if src_conn is not None:
            close_connection(src_conn)
    return backup_path
