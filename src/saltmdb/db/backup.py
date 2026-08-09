import os
import sqlite3
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import open_read_connection, close_connection


def create_snapshot(db_path: str = None) -> str:
    """Safely creates a timestamped database backup snapshot in backups/ using SQLite's backup API."""
    db_path = db_path or get_db_path()
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"saltmdb_snapshot_{timestamp}.db"
    backup_path = os.path.join(backup_dir, backup_filename)

    src_conn = open_read_connection(db_path)
    dest_conn = None
    try:
        dest_conn = sqlite3.connect(backup_path)
        src_conn.backup(dest_conn)
        return f"snapshot successfully created: {backup_path}"
    except Exception as e:
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass
        return f"Error creating snapshot: {e}"
    finally:
        if dest_conn is not None:
            close_connection(dest_conn)
        close_connection(src_conn)
