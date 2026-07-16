import sqlite3
import json
from saltmdb_server import get_db_path

def main():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    try:
        # 1. Inspect Entities
        cursor = conn.execute("""
            SELECT id, title, owner_id, scope, status, weight, is_core, valid_from, valid_to 
            FROM entities
        """)
        entities = cursor.fetchall()
        print(f"=== Entities Total: {len(entities)} ===")
        for e in entities:
            print(f"ID: {e[0]} | Title: {e[1]} | Owner: {e[2]} | Scope: {e[3]} | Status: {e[4]} | W: {e[5]} | Core: {e[6]} | Valid From: {e[7]} | Valid To: {e[8]}")
        print()

        # 2. Inspect Relations
        cursor = conn.execute("""
            SELECT r.id, e1.title, e2.title, r.predicate, r.valid_from, r.valid_to 
            FROM relations r
            JOIN entities e1 ON r.source_id = e1.id
            JOIN entities e2 ON r.target_id = e2.id
        """)
        relations = cursor.fetchall()
        print(f"=== Relations Total: {len(relations)} ===")
        for r in relations:
            print(f"ID: {r[0]} | {r[1]} --({r[3]})--> {r[2]} | Valid: {r[4]} to {r[5]}")
        print()

        # 3. Inspect Events
        cursor = conn.execute("""
            SELECT timestamp, agent_id, type, content 
            FROM events 
            ORDER BY timestamp ASC
        """)
        events = cursor.fetchall()
        print(f"=== Events Total: {len(events)} ===")
        for ev in events:
            print(f"[{ev[0]}] [{ev[1]}] [{ev[2]}] {ev[3][:120]}...")
        print()

    finally:
        conn.close()

if __name__ == "__main__":
    main()
