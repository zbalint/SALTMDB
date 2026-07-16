import sys
import sqlite3
import os

# Add parent directory to sys.path so we can import saltmdb_server
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)

from saltmdb_server import acquire_librarian_lock

def main():
    if len(sys.argv) < 2:
        print("ERROR: database path required")
        sys.exit(1)
        
    db_path = sys.argv[1]
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        acquired = acquire_librarian_lock(conn)
        if acquired:
            print("ACQUIRED")
        else:
            print("FAILED")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
