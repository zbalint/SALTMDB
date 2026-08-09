"""Daemon-owned read access shared by SALTMDB Viewer request handlers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from saltmdb.db.connection import open_read_connection
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
        conn = open_read_connection(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
