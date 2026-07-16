import sys
import os

# Add parent directory to sys.path so we can import saltmdb_server
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)

from saltmdb_server import store_memory

def main():
    if len(sys.argv) < 4:
        print("ERROR: database path, index, and owner_id required")
        sys.exit(1)
        
    db_path = sys.argv[1]
    idx = sys.argv[2]
    owner_id = sys.argv[3]
    
    # Store dynamic db path in environment so get_db_path returns this path
    os.environ["SALTMDB_DB_PATH"] = db_path
    
    try:
        res = store_memory(
            content=f"Concurrent content {idx}",
            tags=["#concurrent"],
            scope="shared",
            owner_id=owner_id,
            title=f"Concurrent Title {idx}",
            skip_duplicate_check=True
        )
        if "stored successfully" in res:
            print("SUCCESS")
        else:
            print(f"FAILED: {res}")
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    main()
