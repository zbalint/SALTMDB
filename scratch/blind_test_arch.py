import os
import sys
import json
import re

# Ensure repository root is on sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import saltmdb_server

def extract_uuid(text):
    if not isinstance(text, str):
        return None
    match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', text)
    return match.group(1) if match else text

def main():
    print("--- Starting Blind Usability Test (Iter 2) for SALTMDB Architectural Scenario ---")
    
    # 1. Store 3 long-term memories
    memories = [
        {
            "name": "API Gateway",
            "title": "API Gateway",
            "content": "---\ntitle: API Gateway\ntags: [gateway, arch]\nsource_path: docs/arch.md\ndate: 2026-07-23\n---\n- [FACT] API Gateway handles routing and ingress filtering.",
            "tags": ["gateway", "arch"],
            "owner_id": "arch_agent",
            "metadata": {"source_path": "docs/arch.md"}
        },
        {
            "name": "Auth Service",
            "title": "Auth Service",
            "content": "---\ntitle: Auth Service\ntags: [auth, arch]\nsource_path: docs/arch.md\ndate: 2026-07-23\n---\n- [FACT] Auth Service manages OAuth2 tokens and session validation.",
            "tags": ["auth", "arch"],
            "owner_id": "arch_agent",
            "metadata": {"source_path": "docs/arch.md"}
        },
        {
            "name": "User Database",
            "title": "User Database",
            "content": "---\ntitle: User Database\ntags: [database, arch]\nsource_path: docs/arch.md\ndate: 2026-07-23\n---\n- [FACT] User Database stores user credentials and metadata.",
            "tags": ["database", "arch"],
            "owner_id": "arch_agent",
            "metadata": {"source_path": "docs/arch.md"}
        }
    ]
    
    raw_responses = {}
    stored_ids = {}
    
    print("\n1. Testing store_memory for 3 components...")
    for mem in memories:
        try:
            print(f"Calling store_memory for '{mem['name']}'...")
            res = saltmdb_server.store_memory(
                content=mem["content"],
                tags=mem["tags"],
                owner_id=mem["owner_id"],
                title=mem["title"],
                metadata=mem["metadata"]
            )
            print(f"Raw Result for '{mem['name']}': {res}")
            raw_responses[mem["name"]] = res
            uuid = extract_uuid(res)
            stored_ids[mem["name"]] = uuid
        except Exception as e:
            print(f"Error storing memory for '{mem['name']}': {e}")
            stored_ids[mem["name"]] = None

    print(f"\nExtracted UUIDs mapping: {stored_ids}")

    # 1b. Testing scan_memories
    print("\n1b. Testing scan_memories for owner_id='arch_agent'...")
    try:
        scan_res = saltmdb_server.scan_memories(owner_id="arch_agent")
        print(f"scan_memories result:\n{scan_res}")
    except Exception as e:
        print(f"Error in scan_memories: {e}")

    # 2. Store directional semantic relations
    print("\n2. Testing store_relation...")
    api_gw_id = stored_ids.get("API Gateway")
    auth_srv_id = stored_ids.get("Auth Service")
    user_db_id = stored_ids.get("User Database")

    rel1_res = None
    rel2_res = None
    
    if api_gw_id and auth_srv_id:
        try:
            print(f"Calling store_relation: '{api_gw_id}' depends_on '{auth_srv_id}'")
            rel1_res = saltmdb_server.store_relation(
                source_id=api_gw_id,
                target_id=auth_srv_id,
                predicate="depends_on"
            )
            print(f"Result rel1: {rel1_res}")
        except Exception as e:
            print(f"Error in store_relation 1: {e}")
            
    if auth_srv_id and user_db_id:
        try:
            print(f"Calling store_relation: '{auth_srv_id}' depends_on '{user_db_id}'")
            rel2_res = saltmdb_server.store_relation(
                source_id=auth_srv_id,
                target_id=user_db_id,
                predicate="depends_on"
            )
            print(f"Result rel2: {rel2_res}")
        except Exception as e:
            print(f"Error in store_relation 2: {e}")

    # 3. Run analyze_dependencies starting from API Gateway
    print("\n3. Testing analyze_dependencies starting from API Gateway...")
    if api_gw_id:
        try:
            print(f"Calling analyze_dependencies(root_entity_id='{api_gw_id}')...")
            dep_res = saltmdb_server.analyze_dependencies(root_entity_id=api_gw_id)
            print(f"analyze_dependencies result:\n{dep_res}")
        except Exception as e:
            print(f"Error in analyze_dependencies: {e}")

    # 4. Run detect_orphaned_memories for owner_id='arch_agent'
    print("\n4. Testing detect_orphaned_memories for owner_id='arch_agent'...")
    try:
        orph_res = saltmdb_server.detect_orphaned_memories(owner_id="arch_agent")
        print(f"detect_orphaned_memories result:\n{orph_res}")
    except Exception as e:
        print(f"Error in detect_orphaned_memories: {e}")

    # 5. Try bulk_store_relations and create_snapshot
    print("\n5a. Testing bulk_store_relations...")
    try:
        bulk_data = [
            {
                "source_id": api_gw_id,
                "target_id": user_db_id,
                "predicate": "depends_on"
            }
        ]
        print(f"Calling bulk_store_relations with: {bulk_data}")
        bulk_res = saltmdb_server.bulk_store_relations(relations=bulk_data)
        print(f"bulk_store_relations result: {bulk_res}")
    except Exception as e:
        print(f"Error in bulk_store_relations: {e}")

    print("\n5b. Testing create_snapshot...")
    try:
        snap_res = saltmdb_server.create_snapshot()
        print(f"create_snapshot result: {snap_res}")
    except Exception as e:
        print(f"Error in create_snapshot: {e}")

if __name__ == "__main__":
    main()

