import sys
import os
import re
import json
import traceback

sys.path.insert(0, os.getcwd())
import saltmdb_server

def extract_uuid(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', text)
    return match.group(0) if match else None

def test_scenario_1_ephemeral():
    print("==================================================")
    print("SCENARIO 1: store_ephemeral_memory & get_ephemeral_memory")
    print("==================================================")
    
    # 1. Standard string store & get
    r1 = saltmdb_server.store_ephemeral_memory(key="session_token", value="token_abc_123_xyz")
    print("[1.1] Store standard token:", repr(r1))
    
    r2 = saltmdb_server.get_ephemeral_memory(key="session_token")
    print("[1.2] Get standard token:", repr(r2))
    
    # 2. Key overwrite
    r3 = saltmdb_server.store_ephemeral_memory(key="session_token", value="token_NEW_456")
    print("[1.3] Overwrite existing key:", repr(r3))
    r4 = saltmdb_server.get_ephemeral_memory(key="session_token")
    print("[1.4] Get overwritten token:", repr(r4))

    # 3. Non-existent key lookup
    r5 = saltmdb_server.get_ephemeral_memory(key="non_existent_key_999")
    print("[1.5] Non-existent key:", repr(r5))

    # 4. Non-string types (integer, dict) - testing type robustness
    try:
        r6 = saltmdb_server.store_ephemeral_memory(key="num_key", value=12345)
        print("[1.6] Store integer value:", repr(r6))
        r7 = saltmdb_server.get_ephemeral_memory(key="num_key")
        print("[1.7] Get integer value:", repr(r7))
    except Exception as e:
        print("[1.6/1.7] Integer value error:", e)

    try:
        r8 = saltmdb_server.store_ephemeral_memory(key=999, value="str_val")
        print("[1.8] Store integer key:", repr(r8))
    except Exception as e:
        print("[1.8] Integer key error:", e)


def test_scenario_2_canonical_tags():
    print("\n==================================================")
    print("SCENARIO 2: get_canonical_tags")
    print("==================================================")
    
    # 1. No domain parameter
    r1 = saltmdb_server.get_canonical_tags()
    print(f"[2.1] get_canonical_tags(): found {len(r1)} tags.")
    print("Sample tags (first 5):", r1[:5])

    # 2. Filter by domain='auth'
    r2 = saltmdb_server.get_canonical_tags(domain="auth")
    print("[2.2] get_canonical_tags(domain='auth'):", r2)

    # 3. Filter by domain='#auth' (with hash prefix)
    r3 = saltmdb_server.get_canonical_tags(domain="#auth")
    print("[2.3] get_canonical_tags(domain='#auth'):", r3)

    # 4. Filter with partial substring e.g. domain='gate'
    r4 = saltmdb_server.get_canonical_tags(domain="gate")
    print("[2.4] get_canonical_tags(domain='gate'):", r4)

    # 5. Non-existent domain substring
    r5 = saltmdb_server.get_canonical_tags(domain="non_existent_domain_xyz")
    print("[2.5] get_canonical_tags(domain='non_existent'):", r5)


def test_scenario_3_db_viewer():
    print("\n==================================================")
    print("SCENARIO 3: start_db_viewer & stop_db_viewer")
    print("==================================================")
    
    # 1. Start DB viewer
    r1 = saltmdb_server.start_db_viewer()
    print("[3.1] start_db_viewer():", repr(r1))

    # 2. Start again when running
    r2 = saltmdb_server.start_db_viewer()
    print("[3.2] start_db_viewer() idempotent check:", repr(r2))

    # 3. Stop DB viewer
    r3 = saltmdb_server.stop_db_viewer()
    print("[3.3] stop_db_viewer():", repr(r3))

    # 4. Stop again when not running
    r4 = saltmdb_server.stop_db_viewer()
    print("[3.4] stop_db_viewer() when already stopped:", repr(r4))


def test_scenario_4_session_summary():
    print("\n==================================================")
    print("SCENARIO 4: get_session_summary")
    print("==================================================")

    session_id = "test_usability_session_999"

    # 1. Query before logging any events
    r1 = saltmdb_server.get_session_summary(session_id=session_id)
    print("[4.1] Summary for empty session:", repr(r1))

    # 2. Log several events under session_id
    l1 = saltmdb_server.log_event(agent_id="test_agent", type="attempt", content="Starting user authentication flow", session_id=session_id)
    l2 = saltmdb_server.log_event(agent_id="test_agent", type="issue", content="Password check failed twice", error_code="ERR_AUTH_FAIL", session_id=session_id)
    l3 = saltmdb_server.log_event(agent_id="test_agent", type="fix", content="User requested OTP reset", session_id=session_id)
    print("[4.2] Logged events:", l1, l2, l3)

    # 3. Query session summary after logging events
    r2 = saltmdb_server.get_session_summary(session_id=session_id)
    print("[4.3] Summary for active session:", repr(r2))

    # 4. Query with non-existent session_id
    r3 = saltmdb_server.get_session_summary(session_id="totally_fake_session_000")
    print("[4.4] Summary for non-existent session:", repr(r3))


def test_scenario_5_bulk_operations():
    print("\n==================================================")
    print("SCENARIO 5: bulk_archive_memory & bulk_commit_consolidation")
    print("==================================================")

    # Step A: Store memories to test bulk archiving
    m1 = saltmdb_server.store_memory(
        content="---\ntitle: Archiving Test Memory 1\ntags: [test_archive]\n---\n[FACT] Temporary record 1 for bulk archive.",
        tags=["test_archive"],
        owner_id="usability_tester",
        title="Archiving Test Memory 1",
        skip_duplicate_check=True
    )
    m2 = saltmdb_server.store_memory(
        content="---\ntitle: Archiving Test Memory 2\ntags: [test_archive]\n---\n[FACT] Temporary record 2 for bulk archive.",
        tags=["test_archive"],
        owner_id="usability_tester",
        title="Archiving Test Memory 2",
        skip_duplicate_check=True
    )
    id1 = extract_uuid(str(m1))
    id2 = extract_uuid(str(m2))
    print(f"[5.1] Created memories for archiving. ID1: {id1}, ID2: {id2}")

    # Test 5.A.1: Valid bulk_archive_memory
    if id1 and id2:
        reqs = [
            {"entity_id": id1, "owner_id": "usability_tester"},
            {"entity_id": id2, "owner_id": "usability_tester"}
        ]
        r_arch = saltmdb_server.bulk_archive_memory(archive_requests=reqs)
        print("[5.2] bulk_archive_memory valid requests:", repr(r_arch))

    # Test 5.A.2: bulk_archive_memory without owner_id in objects
    if id1:
        try:
            reqs_no_owner = [{"entity_id": id1}]
            r_no_owner = saltmdb_server.bulk_archive_memory(archive_requests=reqs_no_owner)
            print("[5.3] bulk_archive_memory missing owner_id:", repr(r_no_owner))
        except Exception as e:
            print("[5.3] bulk_archive_memory missing owner_id ERROR:", e)

    # Test 5.A.3: bulk_archive_memory with list of string UUIDs instead of dicts
    if id1:
        try:
            reqs_strings = [id1]
            r_strings = saltmdb_server.bulk_archive_memory(archive_requests=reqs_strings)
            print("[5.4] bulk_archive_memory list of string UUIDs:", repr(r_strings))
        except Exception as e:
            print("[5.4] bulk_archive_memory list of string UUIDs ERROR:", e)
            traceback.print_exc()

    # Step B: Store memories for bulk consolidation
    m3 = saltmdb_server.store_memory(
        content="---\ntitle: Consolidation Source 1\ntags: [test_consolidation]\n---\n[FACT] Component A initialized on port 8080.",
        tags=["test_consolidation"],
        owner_id="usability_tester",
        title="Consolidation Source 1",
        skip_duplicate_check=True
    )
    m4 = saltmdb_server.store_memory(
        content="---\ntitle: Consolidation Source 2\ntags: [test_consolidation]\n---\n[FACT] Component B initialized on port 8081.",
        tags=["test_consolidation"],
        owner_id="usability_tester",
        title="Consolidation Source 2",
        skip_duplicate_check=True
    )
    id3 = extract_uuid(str(m3))
    id4 = extract_uuid(str(m4))
    print(f"[5.5] Created memories for consolidation. ID3: {id3}, ID4: {id4}")

    # Test 5.B.1: Valid bulk_commit_consolidation
    if id3 and id4:
        consolidations = [
            {
                "parent_ids": [id3, id4],
                "title": "Consolidated System Initialization",
                "content": "---\ntitle: Consolidated System Initialization\ntags: [test_consolidation]\n---\n[FACT] Components A & B initialized on ports 8080 and 8081.",
                "tags": ["test_consolidation"],
                "scope": "shared",
                "weight": 1
            }
        ]
        r_cons = saltmdb_server.bulk_commit_consolidation(consolidations=consolidations)
        print("[5.6] bulk_commit_consolidation valid payload:", repr(r_cons))

    # Test 5.B.2: bulk_commit_consolidation with non-existent parent UUIDs
    try:
        fake_uuid1 = "00000000-0000-0000-0000-000000000001"
        fake_uuid2 = "00000000-0000-0000-0000-000000000002"
        consolidations_fake = [
            {
                "parent_ids": [fake_uuid1, fake_uuid2],
                "title": "Fake Consolidation",
                "content": "Consolidated content for missing parents",
                "tags": ["fake"],
            }
        ]
        r_cons_fake = saltmdb_server.bulk_commit_consolidation(consolidations=consolidations_fake)
        print("[5.7] bulk_commit_consolidation fake parents:", repr(r_cons_fake))
    except Exception as e:
        print("[5.7] bulk_commit_consolidation fake parents ERROR:", e)
        traceback.print_exc()

if __name__ == "__main__":
    test_scenario_1_ephemeral()
    test_scenario_2_canonical_tags()
    test_scenario_3_db_viewer()
    test_scenario_4_session_summary()
    test_scenario_5_bulk_operations()
