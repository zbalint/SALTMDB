"""Daemon-owned read access shared by SALTMDB Viewer request handlers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class ViewerReadGateway:
    """Open read-only connections for the daemon's canonical database path.

    The Viewer is hosted by the daemon, but each HTTP request still needs its
    own SQLite connection.  Keeping creation here prevents handlers from
    silently resolving a different environment-selected database.
    """

    def __init__(self, db_path: str, daemon_state: Any = None):
        self.db_path = str(Path(db_path).resolve())
        self.daemon_state = daemon_state

    def connect(self) -> sqlite3.Connection:
        uri = Path(self.db_path).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn
