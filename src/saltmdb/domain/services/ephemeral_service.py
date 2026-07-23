import logging
from saltmdb.db.connection import EPHEMERAL_CONN

logger = logging.getLogger(__name__)

def store_ephemeral_memory(key: str, value: str) -> str:
    """Stores a volatile secret key/value in the isolated in-memory SQLite database."""
    if not key or not value:
        return "Error: Both key and value are mandatory for ephemeral memory storage."
    try:
        with EPHEMERAL_CONN:
            EPHEMERAL_CONN.execute("""
                INSERT INTO ephemeral_memories (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, created_at = CURRENT_TIMESTAMP
            """, (key, value))
        return f"Ephemeral secret stored successfully for key: {key}"
    except Exception as e:
        logger.error("Error storing ephemeral memory: %s", e)
        return f"Error storing ephemeral memory: {e}"

def get_ephemeral_memory(key: str) -> str:
    """Retrieves a volatile secret from the isolated in-memory database."""
    if not key:
        return "Error: key is mandatory."
    try:
        cursor = EPHEMERAL_CONN.execute("SELECT value FROM ephemeral_memories WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            return row[0]
        return f"Key '{key}' not found in ephemeral memory."
    except Exception as e:
        logger.error("Error retrieving ephemeral memory: %s", e)
        return f"Error retrieving ephemeral memory: {e}"
