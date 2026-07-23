import os
import sqlite3
from datetime import datetime, UTC
from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection

def create_snapshot(db_path: str = None) -> str:
    """Safely creates a timestamped database backup snapshot in backups/ using SQLite's backup API."""
    db_path = db_path or get_db_path()
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"saltmdb_snapshot_{timestamp}.db"
    backup_path = os.path.join(backup_dir, backup_filename)
    
    src_conn = get_connection(db_path)
    try:
        dest_conn = sqlite3.connect(backup_path)
        try:
            with dest_conn:
                src_conn.backup(dest_conn)
            return f"snapshot successfully created: {backup_path}"
        finally:
            dest_conn.close()
    except Exception as e:
        return f"Error creating snapshot: {e}"
    finally:
        src_conn.close()
