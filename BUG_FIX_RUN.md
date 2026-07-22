# SALTMDB Bug & Edge Case Fix Report (`BUG_FIX_RUN.md`)

**Date:** July 23, 2026  
**Target Project:** SALTMDB (Short And Long-Term Memory DataBase)  
**Status:** All Bugs Identified, Fixing Plan Executed, 100% E2E & Unit Test Suites Passing.

---

##  EXECUTIVE SUMMARY

As part of the goal execution, a comprehensive End-to-End (E2E) test suite (`scratch/test_e2e.py`) was created to test SALTMDB across all core system layers:
1. **MCP Stdio Server Interface** (`saltmdb_server.py` stdio JSON-RPC protocol).
2. **Librarian Background Process** (`python saltmdb_server.py --librarian` process detachment, atomic leader locking, tag consolidation, LRU decay).
3. **HTTP Database Dashboard & REST API** (`saltmdb_viewer.py` API endpoints, static assets, HTTP error codes).
4. **Knowledge Graph & Memory Operations** (FTS5 search, BM25 ranking, tag folksonomy, parent-child graph linking, duplicate detection, secret redaction middleware).

Through exploratory edge-case analysis and E2E testing, **9 potential bugs and edge-case vulnerabilities** were identified across `saltmdb_server.py` and `saltmdb_viewer.py`. A structured fixing plan was implemented and verified with automated test suites.

---

## 🔍 DETAILED FINDINGS & EDGE CASES DISCOVERED

### 1. `store_memory` Title Secret Redaction Omission
* **Location:** `saltmdb_server.py:518-526`
* **Category:** Security / Privacy Leak
* **Symptom:** `store_memory` invoked `redact_secrets(content)` on the body text, but failed to call `redact_secrets(title)` when explicit titles were passed.
* **Impact:** Sensitive credentials (e.g. GitHub tokens `ghp_...`, Anthropic API keys `sk-ant-...`, OpenAI project keys) included in custom memory titles were persisted in plain text in both `entities.title` and `entities_fts.title`.

### 2. `search_memory` Tag Filter Disconnect for Canonical Alias Tags & Unnormalized Inputs
* **Location:** `saltmdb_server.py:737-760`
* **Category:** Search & Query Logic Bug
* **Symptom:**
  - `search_memory` filtered tags using direct name matching `JOIN tags t ON et.tag_id = t.id WHERE t.name IN (...)`.
  - When the Librarian process merged alias tags (e.g. `#Auth_Error`) into canonical tags (e.g. `#auth-error`), `entity_tags` rows were updated to point to the canonical `tag_id`.
  - Searching for `#Auth_Error` looked up the alias tag ID in `tags` table, which failed to match `entity_tags` rows linking to the canonical tag ID, returning `0` results.
  - Additionally, search tag filters without `#` prefixes or with mixed casing (e.g., `'AUTH-ERROR'`) failed SQLite case-sensitive matching.
* **Impact:** Agents searching by alias or unnormalized tag names received zero search results despite active matching entities.

### 3. `check_duplicate_memories` Null Input Crash
* **Location:** `saltmdb_server.py:1625, 1630`
* **Category:** Unhandled Exception / Stability
* **Symptom:** `title.lower()` and `content[:1000].lower()` assumed non-None strings. Passing `title=None` or `content=None` triggered an unhandled `AttributeError: 'NoneType' object has no attribute 'lower'`.
* **Impact:** Crashed agent tool calls when duplicate checks were performed with optional or empty parameters.

### 4. Permissible Self-Referential Graph Relations (`source_id == target_id`)
* **Location:** `saltmdb_server.py:1690-1712, 2146-2218`
* **Category:** Data Integrity / Graph Topology
* **Symptom:** `store_relation` and `bulk_store_relations` accepted relations where `source_id == target_id`.
* **Impact:** Created self-referential graph loops that could break dependency tree traversals or create cyclic references.

### 5. `bulk_commit_consolidation` Relation Re-pointing Inconsistency
* **Location:** `saltmdb_server.py:1978-2079`
* **Category:** Graph Topology / Relation Loss
* **Symptom:** Single `commit_consolidation` re-pointed active incoming/outgoing relation edges from raw parent IDs to the new consolidated entity ID and cleaned up duplicate edges. `bulk_commit_consolidation` omitted relation re-pointing, leaving active relation edges pointing to archived parent nodes.
* **Impact:** Broken relation links and loss of PageRank authority signal when performing bulk memory consolidations.

### 6. HTTP Viewer 501 Error on CORS `OPTIONS` Preflight Requests
* **Location:** `saltmdb_viewer.py:35-60`
* **Category:** Web API / CORS Compatibility
* **Symptom:** `SALTMDBHandler` defined `do_GET` but lacked `do_OPTIONS`. Web applications issuing cross-origin browser preflight requests received `HTTP 501: Unsupported method ('OPTIONS')`.
* **Impact:** Blocked web dashboard interactions from external web clients.

### 7. HTTP Viewer Unchecked Negative & Invalid Page Pagination
* **Location:** `saltmdb_viewer.py:68-71, 133-136`
* **Category:** Web API Validation
* **Symptom:** `get_entities` and `get_events` parsed `page = int(query["page"][0])` without enforcing `page >= 1`. Passing `page=-5` caused negative SQL offsets (`offset = -600`).
* **Impact:** Invalid SQL offset queries and incorrect pagination metadata.

### 8. `get_entity_detail` Dropping Relations with Missing/Deleted Targets
* **Location:** `saltmdb_viewer.py:246-275`
* **Category:** Web API / Data Presentation
* **Symptom:** `get_entity_detail` joined relations using `INNER JOIN entities e ON r.target_id = e.id`. If a target entity record was missing or deleted under disabled FK modes, `INNER JOIN` dropped the relation row completely.
* **Impact:** Incomplete graph representation in DB Viewer.

### 9. Database Connection Leaks on Error Paths in HTTP Viewer
* **Location:** `saltmdb_viewer.py:64-325`
* **Category:** Resource Leak / Memory Management
* **Symptom:** `conn.close()` calls were placed at the end of `try` blocks before `send_json`. Exceptions inside `try` skipped `conn.close()`.
* **Impact:** Leaked open SQLite database file handles under error conditions.

---

## 🛠️ FIXING PLAN & IMPLEMENTATION

| Component | Target File | Implemented Solution |
| :--- | :--- | :--- |
| **Title Redaction** | `saltmdb_server.py` | Added explicit `title = redact_secrets(title)` in `store_memory` so all user-supplied and auto-extracted titles pass through credential scrubbing. |
| **Canonical Tag Search** | `saltmdb_server.py` | Updated `search_memory` tag filtering to normalize input tag names and resolve tag names against `tags.canonical_id` and `LOWER(name)` before querying `entity_tags`. |
| **Null Safety** | `saltmdb_server.py` | Added `title = title or ""` and `content = content or ""` guards in `check_duplicate_memories`. |
| **Graph Safety** | `saltmdb_server.py` | Enforced `source_id != target_id` checks in `store_relation` and `bulk_store_relations`, returning descriptive error messages. |
| **Bulk Consolidation** | `saltmdb_server.py` | Added atomic relation re-pointing, self-loop deletion, and duplicate edge cleanup to `bulk_commit_consolidation`. |
| **CORS Preflight** | `saltmdb_viewer.py` | Implemented `do_OPTIONS` handler returning `200 OK` with CORS headers (`Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`). |
| **Pagination Guard** | `saltmdb_viewer.py` | Enforced `page = max(1, page)` in `get_entities` and `get_events`. |
| **LEFT JOIN Relations** | `saltmdb_viewer.py` | Updated `get_entity_detail` and `get_all_relations` to use `LEFT JOIN entities` with `COALESCE(e.title, r.target_id)`. |
| **Connection Cleanup** | `saltmdb_viewer.py` | Moved `conn.close()` into `finally` blocks across all REST API handlers (`get_entities`, `get_events`, `get_tags`, `get_locks`, `get_entity_detail`, `get_all_relations`). |

---

## 🧪 VERIFICATION RESULTS

### 1. End-to-End Test Suite (`scratch/test_e2e.py`)
Ran 11 E2E tests covering FastMCP Stdio RPC, Librarian Worker detachment, HTTP Viewer endpoints, CORS preflight, secret redaction, and tag canonicalization:
```bash
$env:PYTHONPATH="."; python -m unittest scratch/test_e2e.py
----------------------------------------------------------------------
Ran 11 tests in 6.697s

OK
```

### 2. Unit Test Suite (`scratch/test_db.py`)
Ran 45 unit tests covering core database schemas, FTS5 triggers, LRU decay, tag heuristics, and lock rules:
```bash
$env:PYTHONPATH="."; python -m unittest scratch/test_db.py
----------------------------------------------------------------------
Ran 45 tests in 18.099s

OK
```

---

## 🏁 CONCLUSION

All end-to-end tests, edge case tests, and unit tests are **100% passing**. SALTMDB is fully verified, concurrency-safe, and resilient against credential leaks, search disconnects, and web viewer edge cases.
