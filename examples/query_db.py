import os
import sqlite3
import json

def get_db_path() -> str:
    """Resolves the default database path, respecting the SALTMDB_DB_PATH env override."""
    env_path = os.environ.get("SALTMDB_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    
    default_dir = os.path.expanduser("~/.saltmdb")
    return os.path.join(default_dir, "saltmdb.db")

def main():
    db_path = get_db_path()
    print(f"Connecting to SALTMDB database: {db_path}\n")
    
    if not os.path.exists(db_path):
        print(f"Error: Database file does not exist at {db_path}.")
        print("Please run the MCP server first to initialize the schema.")
        return

    # Connect to database in read-only mode to prevent write lock blocking
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # 1. Fetch count of stored memories
        cursor = conn.execute("SELECT status, COUNT(*) FROM entities GROUP BY status")
        print("=== Database Status Counts ===")
        for status, count in cursor.fetchall():
            print(f"- {status.upper()}: {count} memories")
        print()

        # 2. Fetch Core persona and system guidelines
        cursor = conn.execute("""
            SELECT title, weight, is_core 
            FROM entities 
            WHERE is_core = 1 AND status != 'archived'
            ORDER BY weight DESC
        """)
        print("=== Core Guidelines (Loaded at Bootstrap) ===")
        for title, weight, is_core in cursor.fetchall():
            print(f"- [Core={is_core}] [Weight={weight}] {title}")
        print()

        # 3. Fetch recent operational events
        cursor = conn.execute("""
            SELECT timestamp, agent_id, type, content 
            FROM events 
            ORDER BY timestamp DESC 
            LIMIT 5
        """)
        print("=== 5 Most Recent Events ===")
        for timestamp, agent_id, etype, content in cursor.fetchall():
            # Check if JSON content (like consolidation requests)
            try:
                data = json.loads(content)
                snippet = f"Consolidation request for {data.get('target')} {data.get('tag_name') or ''}"
            except (ValueError, TypeError):
                snippet = content[:80] + "..." if len(content) > 80 else content
            print(f"[{timestamp}] [{agent_id}] [{etype.upper()}] {snippet}")
        print()

        # 4. Demonstrate manual full-text search (FTS5) using BM25 relevance matching
        search_query = "ops"
        print(f"=== FTS5 Keyword Search (Query: '{search_query}') ===")
        cursor = conn.execute("""
            SELECT e.title, e.weight, bm25(entities_fts, 0.0, 10.0, 1.0) as score
            FROM entities_fts f
            JOIN entities e ON e.id = f.id
            WHERE entities_fts MATCH ? AND e.status != 'archived'
            ORDER BY score ASC
            LIMIT 3
        """, (search_query,))
        for title, weight, score in cursor.fetchall():
            print(f"- {title} (BM25 Score: {score:.4f}, Weight: {weight})")
            
    except sqlite3.OperationalError as e:
        print(f"Operational database error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
