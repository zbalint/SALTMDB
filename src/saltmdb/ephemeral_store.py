import sqlite3
from typing import Optional

class EphemeralStore:
    def __init__(self) -> None:
        self._conn=sqlite3.connect(":memory:", check_same_thread=False, timeout=10.0)
        self.__init_schema()

    def __init_schema(self) -> None:
        with self._conn:
            self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ephemeral_memories (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """)

    def store(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ephemeral_memories (key, value) VALUES (?, ?)", 
                (key, value)
            )

    def get(self, key: str) -> Optional[str]:
        with self._conn:
            cursor = self._conn.execute(
                "SELECT value FROM ephemeral_memories WHERE key = ?", 
                (key,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

