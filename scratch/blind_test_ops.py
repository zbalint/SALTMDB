import sys
import os
import json
import re
import traceback

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import saltmdb_server

def extract_uuid(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", text, re.IGNORECASE)
    return match.group(1) if match else None

def run_test():
    print("=" * 80)
    print("STARTING BLIND USABILITY TEST FOR SALTMDB (ROUND 2)")
    print("=" * 80)

    db_file = os.path.abspath("test_saltmdb_blind_ops.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass
    saltmdb_server.init_db(db_file)

    # ----------------------------------------------------
    # STEP 1: Log an issue event about Nginx 502 Bad Gateway
    # ----------------------------------------------------
    print("\n--- STEP 1: Log an issue event using log_event ---")

    # Test 1a: Intentional parameter confusion test (passing 'event_type' instead of 'type')
    try:
        print("Testing log_event with unexpected arg 'event_type' instead of 'type'...")
        saltmdb_server.log_event(agent_id="ops_agent", event_type="issue", content="Nginx 502 Bad Gateway in production")
    except Exception as e:
        print(f"Caught expected error when using wrong kwarg 'event_type': {type(e).__name__}: {e}")

    # Test 1b: Valid log_event call
    try:
        res1 = saltmdb_server.log_event(
            agent_id="ops_agent",
            type="issue",
            content="Nginx 502 Bad Gateway issue in production. Upstream server timed out after 30s.",
            error_code="HTTP_502",
            session_id="session_ops_101"
        )
        print(f"log_event result:\n{res1}")
    except Exception as e:
        print(f"Error in log_event: {e}")
        traceback.print_exc()

    # Test 1c: Retrieve recent events using get_recent_events
    try:
        events = saltmdb_server.get_recent_events(agent_id="ops_agent", type_filter="issue")
        print(f"get_recent_events result:\n{events}")
    except Exception as e:
        print(f"Error in get_recent_events: {e}")

    # ----------------------------------------------------
    # STEP 2: Check for duplicates using check_duplicate_memories
    # ----------------------------------------------------
    print("\n--- STEP 2: Check duplicates using check_duplicate_memories ---")
    
    proposed_title = "Nginx 502 Bad Gateway Upstream Timeout Fix"
    proposed_content = """# Nginx 502 Bad Gateway Upstream Timeout Fix

[FACT] Nginx returns 502 Bad Gateway when upstream proxy_read_timeout is exceeded.
[SOLUTION] Increase proxy_read_timeout to 300s and proxy_connect_timeout to 75s in nginx.conf.
"""
    proposed_tags = ["nginx", "502-bad-gateway", "ops", "troubleshooting"]

    try:
        dups1 = saltmdb_server.check_duplicate_memories(
            title=proposed_title,
            content=proposed_content,
            owner_id="ops_agent",
            tags=proposed_tags
        )
        print(f"check_duplicate_memories result (before store):\n{dups1}")
    except Exception as e:
        print(f"Error in check_duplicate_memories: {e}")

    # Test 2b: Missing owner_id check
    try:
        print("Testing check_duplicate_memories without owner_id...")
        saltmdb_server.check_duplicate_memories(title=proposed_title, content=proposed_content)
    except Exception as e:
        print(f"Caught error without owner_id: {type(e).__name__}: {e}")

    # ----------------------------------------------------
    # STEP 3: Store long-term memory using store_memory
    # ----------------------------------------------------
    print("\n--- STEP 3: Store memory using store_memory ---")

    sfb_content = """---
title: Nginx 502 Bad Gateway Upstream Timeout Fix
tags: [nginx, 502-bad-gateway, ops, troubleshooting]
source_path: docs/ops/nginx_502_fix.md
date: 2026-07-23
---

# Nginx 502 Bad Gateway Upstream Timeout Fix

- [FACT] High backend response latency (>30s) triggers 502 Bad Gateway in Nginx proxy module.
- [DECISION] Increased proxy_read_timeout and proxy_send_timeout from 60s to 300s in `/etc/nginx/conf.d/proxy.conf`.
- [SOLUTION] Reload Nginx configuration with `nginx -s reload`. Verified 200 OK on long-running queries.
"""
    
    stored_entity_id = None
    try:
        res_store = saltmdb_server.store_memory(
            content=sfb_content,
            tags=["#nginx", "#502-bad-gateway", "#ops"],
            owner_id="ops_agent",
            title="Nginx 502 Bad Gateway Upstream Timeout Fix",
            metadata={
                "source_path": "docs/ops/nginx_502_fix.md",
                "search_aliases": ["nginx timeout", "502 bad gateway fix", "proxy_read_timeout"]
            },
            scope="shared",
            weight=2
        )
        print(f"store_memory result:\n{res_store}")
        stored_entity_id = extract_uuid(res_store)
    except Exception as e:
        print(f"Error in store_memory: {e}")
        traceback.print_exc()

    print(f"\nExtracted Stored entity ID: {stored_entity_id}")

    # Test 3b: Post-store check_duplicate_memories
    try:
        dups2 = saltmdb_server.check_duplicate_memories(
            title="Nginx 502 Bad Gateway Fix",
            content=proposed_content,
            owner_id="ops_agent",
            tags=["#nginx", "#502-bad-gateway"]
        )
        print(f"\ncheck_duplicate_memories result (after store):\n{dups2}")
    except Exception as e:
        print(f"Error in post-store check_duplicate_memories: {e}")

    # ----------------------------------------------------
    # STEP 4: Search memory using search_memory
    # ----------------------------------------------------
    print("\n--- STEP 4: Search memory using search_memory ---")

    # Test 4a: Passing 'query' instead of 'query_keywords'
    try:
        print("Testing search_memory with kwarg 'query' instead of 'query_keywords'...")
        saltmdb_server.search_memory(owner_id="ops_agent", query="nginx 502")
    except Exception as e:
        print(f"Caught expected error when using 'query': {type(e).__name__}: {e}")

    # Test 4b: Normal keyword search
    try:
        res_search1 = saltmdb_server.search_memory(
            owner_id="ops_agent",
            query_keywords="nginx timeout 502",
            tags_filter=["nginx"]
        )
        print(f"\nsearch_memory (query_keywords='nginx timeout 502', tags_filter=['nginx']) result:\n{res_search1}")
    except Exception as e:
        print(f"Error in search_memory 4b: {e}")

    # Test 4c: Tag filter with '#'
    try:
        res_search1_taghash = saltmdb_server.search_memory(
            owner_id="ops_agent",
            query_keywords="nginx",
            tags_filter=["#nginx"]
        )
        print(f"\nsearch_memory (tags_filter=['#nginx']) result:\n{res_search1_taghash}")
    except Exception as e:
        print(f"Error in search_memory 4c: {e}")

    # Test 4d: Explain mode = True on zero results
    try:
        res_explain = saltmdb_server.search_memory(
            owner_id="ops_agent",
            query_keywords="nonexistent_xyz_term_12345",
            explain_mode=True
        )
        print(f"\nsearch_memory (explain_mode=True on 0 matches) result:\n{res_explain}")
    except Exception as e:
        print(f"Error in search_memory explain_mode: {e}")

    # Test 4e: Explain mode = True on matching query
    try:
        res_explain_match = saltmdb_server.search_memory(
            owner_id="ops_agent",
            query_keywords="nginx",
            explain_mode=True
        )
        print(f"\nsearch_memory (explain_mode=True on matching query) result:\n{res_explain_match}")
    except Exception as e:
        print(f"Error in search_memory explain_mode match: {e}")

    # ----------------------------------------------------
    # STEP 5: Fetch memory chunk using fetch_memory_chunk
    # ----------------------------------------------------
    print("\n--- STEP 5: Fetch chunk using fetch_memory_chunk ---")
    if stored_entity_id:
        try:
            chunk = saltmdb_server.fetch_memory_chunk(entity_id=stored_entity_id)
            print(f"fetch_memory_chunk('{stored_entity_id}') result:\n{chunk}")
        except Exception as e:
            print(f"Error in fetch_memory_chunk: {e}")
    else:
        print("Skipping fetch_memory_chunk because stored_entity_id was not captured.")

    # ----------------------------------------------------
    # STEP 6: Archive memory / Commit consolidation
    # ----------------------------------------------------
    print("\n--- STEP 6: Archive memory or Commit consolidation ---")

    # Store a second raw memory to consolidate
    raw_mem_2_id = None
    try:
        res_store2 = saltmdb_server.store_memory(
            content="""---
title: Upstream Proxy Buffer Tuning for Nginx 502
tags: [nginx, buffers, ops]
source_path: docs/ops/nginx_buffers.md
date: 2026-07-23
---
# Upstream Proxy Buffer Tuning for Nginx 502
- [FACT] Nginx proxy_buffer_size must be enlarged to 16k for large header responses.
""",
            tags=["nginx", "buffers"],
            owner_id="ops_agent",
            title="Upstream Proxy Buffer Tuning for Nginx 502",
            metadata={"source_path": "docs/ops/nginx_buffers.md"}
        )
        print(f"store_memory raw_mem_2 result:\n{res_store2}")
        raw_mem_2_id = extract_uuid(res_store2)
    except Exception as e:
        print(f"Error storing raw memory 2: {e}")

    print(f"\nRaw Memory 1 ID: {stored_entity_id}")
    print(f"Raw Memory 2 ID: {raw_mem_2_id}")

    # Test commit_consolidation
    consol_id = None
    if stored_entity_id and raw_mem_2_id:
        try:
            print(f"\nTesting commit_consolidation with parent_ids=[{stored_entity_id}, {raw_mem_2_id}]...")
            res_consol = saltmdb_server.commit_consolidation(
                parent_ids=[stored_entity_id, raw_mem_2_id],
                title="Consolidated Nginx 502 Bad Gateway and Buffer Fixes",
                content="""---
title: Consolidated Nginx 502 Bad Gateway and Buffer Fixes
tags: [nginx, 502, buffers, ops]
source_path: docs/ops/nginx_consolidated.md
date: 2026-07-23
---
# Consolidated Nginx 502 Bad Gateway and Buffer Fixes
- [FACT] Nginx 502 errors stem from upstream timeouts (>30s) and small buffer sizes.
- [DECISION] Increase proxy_read_timeout to 300s and proxy_buffer_size to 16k.
""",
                tags=["nginx", "502", "buffers", "ops"]
            )
            print(f"commit_consolidation result:\n{res_consol}")
            consol_id = extract_uuid(res_consol)
        except Exception as e:
            print(f"Error in commit_consolidation: {e}")
            traceback.print_exc()

    # Verify parent memories are archived after consolidation
    if stored_entity_id:
        try:
            chunk_parent1 = saltmdb_server.fetch_memory_chunk(entity_id=stored_entity_id)
            print(f"\nfetch_memory_chunk on consolidated parent 1 ({stored_entity_id}) result:\n{chunk_parent1}")
        except Exception as e:
            print(f"fetch_memory_chunk parent 1 error: {e}")

    # Test archive_memory on an individual memory
    try:
        print("\nCreating temporary memory to test archive_memory...")
        temp_store = saltmdb_server.store_memory(
            content="Temporary memory to archive",
            tags=["temp"],
            owner_id="ops_agent",
            title="Temporary Memory"
        )
        temp_id = extract_uuid(temp_store)
        print(f"Archiving temp memory {temp_id}...")
        res_arch = saltmdb_server.archive_memory(entity_id=temp_id, owner_id="ops_agent")
        print(f"archive_memory result:\n{res_arch}")
    except Exception as e:
        print(f"Error in archive_memory: {e}")

    # Test archive_memory without owner_id (check parameter requirement)
    try:
        print("\nTesting archive_memory without owner_id...")
        saltmdb_server.archive_memory(entity_id=temp_id)
    except Exception as e:
        print(f"Caught error calling archive_memory without owner_id: {type(e).__name__}: {e}")

    print("\n" + "=" * 80)
    print("FINISHED BLIND USABILITY TEST")
    print("=" * 80)

if __name__ == "__main__":
    run_test()
