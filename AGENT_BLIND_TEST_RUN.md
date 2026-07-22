# SALTMDB Subagent Blind Usability Test Report & Refactoring Plan (`AGENT_BLIND_TEST_RUN.md`)

**Date:** July 23, 2026  
**Target System:** SALTMDB (Short And Long-Term Memory DataBase)  
**Execution:** 3 Autonomous Parallel Blind-Tester Subagents (Ops, Architecture, Assistant)

---

## 1. SUBAGENT BLIND TESTING OVERVIEW

Three autonomous subagents were invoked to perform "blind tests" of SALTMDB tools without reading or inspecting the `saltmdb_server.py` implementation code:

1. **Ops Blind Tester** (`scratch/blind_test_ops.py`):
   - Executed event logging (`log_event`), duplicate checking (`check_duplicate_memories`), memory storage (`store_memory`), FTS5 search (`search_memory`), chunk fetching (`fetch_memory_chunk`), archiving (`archive_memory`), and consolidation (`commit_consolidation`).
2. **Architect Blind Tester** (`scratch/blind_test_arch.py`):
   - Executed memory creation for graph nodes, graph relation storage (`store_relation`), recursive CTE dependency traversal (`analyze_dependencies`), orphan detection (`detect_orphaned_memories`), bulk relation writes (`bulk_store_relations`), and database snapshotting (`create_snapshot`).
3. **Assistant Blind Tester** (`scratch/blind_test_assistant.py`):
   - Executed ephemeral memory storage/retrieval (`store_ephemeral_memory`, `get_ephemeral_memory`), canonical tag querying (`get_canonical_tags`), database viewer lifecycle (`start_db_viewer`, `stop_db_viewer`), session event logging (`get_session_summary`), bulk archiving (`bulk_archive_memory`), and bulk consolidation (`bulk_commit_consolidation`).

---

## 2. SYNTHESIZED BLIND TEST FINDINGS & FRICTION POINTS

| Area / Tool | Friction Point Discovered | Root Cause | Proposed Solution |
| :--- | :--- | :--- | :--- |
| **Return Payload Format** | Tool output strings like `"Knowledge stored successfully with ID: <uuid>"` force agents to write regex string parsers or break downstream tool chaining. | Write tools return formatted human-readable strings instead of structured data objects. | Add structured dict / JSON payload output across write tools while preserving human-readable text formatting. |
| **Tool Chaining Failure** | Passing `store_memory` return string directly into `store_relation`, `fetch_memory_chunk`, or `archive_memory` causes DB lookup failures. | Tool parameters expect exact UUID strings, failing when given status strings containing UUIDs or entity titles. | Implement **Smart Entity ID Resolution** (`resolve_entity_id`) in all tools expecting entity IDs (resolving exact UUIDs, regex embedded UUIDs in status strings, or entity titles). |
| **Parameter Naming Discrepancies** | Passing `query` into `search_memory`, `event_type` into `log_event`, `text` / `tag` / `owner` into `store_memory`, or `id` into `archive_memory` raises `TypeError`. | Python tool signatures require exact parameter names (`query_keywords`, `type`, `content`, `tags`, `owner_id`). | Implement `**kwargs` parameter aliasing across all server tools to seamlessly accept parameter synonyms. |
| **`init_db` Default Argument** | Calling `saltmdb_server.init_db()` in Python scripts without arguments raises `TypeError`. | `init_db(db_path: str)` requires a explicit positional argument. | Default `db_path: str \| None = None` to automatically resolve via `get_db_path()`. |
| **Ephemeral Key Lookup Signal** | Non-existent key lookups in `get_ephemeral_memory` return `"Error: Key '...' not found"`. | Returns a non-empty string which agents treat as a valid stored secret. | Return structured `{"found": false, "error": "..."}` or `None` for missing keys. |
| **Canonical Tag Filtering** | Parameter `domain` in `get_canonical_tags` misleads callers who expect a category/domain filter instead of a substring search. | Parameter named `domain` instead of `query` / `substring`. | Alias `query` and `substring` to `domain` in `get_canonical_tags`. |
| **Bulk Tool Type Flexibility** | `bulk_archive_memory(["uuid1", "uuid2"])` crashes with `AttributeError: 'str' object has no attribute 'get'`. | Expects list of dicts `[{"entity_id": "...", "owner_id": "..."}]`. | Accept both string lists `["uuid1", "uuid2"]` and dict lists. |
| **Hardcoded Viewer Port** | `start_db_viewer()` hardcodes port 8080. | No parameter or environment override for port selection. | Add optional `port: int = 8080` parameter to `start_db_viewer` and update viewer script. |
| **Internal Parameter Leak** | `commit_consolidation` exposes `db_connection` (`[INTERNAL TEST ONLY]`) in public tool schema. | Test helper argument leaked into signature. | Remove `db_connection` from public tool parameters. |

---

## 3. IMPLEMENTATION PLAN

1. **Implement Smart Entity & UUID Resolution (`resolve_entity_id`)**:
   - Create a utility function `resolve_entity_id(conn, input_val)`:
     - Checks if `input_val` is a valid UUID -> returns `input_val`.
     - Uses regex to extract UUID if `input_val` is a status string (e.g. `"Knowledge stored successfully with ID: 29be643f-..."`).
     - Queries `SELECT id FROM entities WHERE title = ? AND status != 'archived'` if `input_val` is an entity title.
   - Apply `resolve_entity_id` across `store_relation`, `fetch_memory_chunk`, `archive_memory`, `analyze_dependencies`, and `commit_consolidation`.

2. **Implement Parameter Synonyms & Aliasing**:
   - In `search_memory`: alias `query`, `q`, `keywords` -> `query_keywords`.
   - In `log_event`: alias `event_type` -> `type`; alias `message`, `description` -> `content`.
   - In `store_memory`: alias `text` -> `content`; `tag` -> `tags`; `owner` -> `owner_id`.
   - In `archive_memory`: alias `id` -> `entity_id`; `owner` -> `owner_id`.
   - In `get_canonical_tags`: alias `query`, `substring`, `tag_filter` -> `domain`.

3. **Enhance Return Payloads**:
   - Update write tools (`store_memory`, `log_event`, `store_relation`, `archive_memory`, `commit_consolidation`) to return structured dicts / JSON payloads.
   - Update `get_ephemeral_memory` to return a clear dict or `None` signal for missing keys.

4. **Update Viewer Port Configurability**:
   - Add `port: int = 8080` to `start_db_viewer`.
   - Update `saltmdb_viewer.py` to support `SALTMDB_VIEWER_PORT` environment variable or `--port` argument.

5. **Update Test Suites & Verify**:
   - Update unit tests (`scratch/test_db.py`) and E2E tests (`scratch/test_e2e.py`).
   - Run full test execution.
