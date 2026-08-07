# SALTMDB: Local-First MCP Memory Server

**SALTMDB** (Short And Long-Term Memory DataBase) is a centralized, local-first memory framework designed for AI CLI tools and agents (such as Antigravity, Copilot, and Claude Code). It acts as a shared memory layer, allowing multiple concurrent agents to read, write, and consolidate contextual facts using a lightweight Python package with SQLite and ONNX-based vector embeddings.

> [!TIP]
> * **Installation:** To install and register the MCP server, see the **[Installation Guide](INSTALL.md)**.
> * **Developer Guide:** To learn how to configure your AI agents to utilize this memory system, read the **[Agent Integration & Design Guide](AGENT_GUIDE.md)**.

---

## 🏛️ System Architecture

SALTMDB is built using standard Python libraries and SQLite, prioritizing concurrency safety, security, and low memory overhead.

```mermaid
graph TD
    subgraph Active Agents
        A[Antigravity CLI]
        B[Copilot / Claude Code]
    end

    subgraph MCP Server Layer
        A -->|Stdio / MCP| Server[saltmdb.mcp.server]
        B -->|Stdio / MCP| Server
        Server -->|BEGIN IMMEDIATE / WAL / write_transaction_retrying| MainDB[(sqlite3: saltmdb.db)]
        Server -->|check_same_thread=False| EphemDB[(sqlite3: :memory:)]
    end

    subgraph Background Threads
        Server -->|_embed_pool: ThreadPoolExecutor x2| EmbedWorker[embedding_service.embed_entity_async]
        EmbedWorker -->|fastembed ONNX + sqlite_vec| VecDB[(entity_embeddings vec0)]
        Server -->|_librarian_trigger_pool x1 fire-and-forget| TriggerCheck[cooldown check / subprocess spawn]
        TriggerCheck -->|Atomic last_run_at UPDATE| Lock[_system_locks]
        TriggerCheck -->|python -m saltmdb --librarian| Lib[Librarian gc subprocess]
        Lib -->|acquire_librarian_lock| Lock
        Lib -->|merge_tags_heuristics| MainDB
        Lib -->|consolidate_vector_clusters| MainDB
        Lib -->|scout_consolidated_supersessions| MainDB
        Lib -->|WAL checkpoint + PRAGMA optimize| MainDB
    end
```

- **Mechanical Text Quality Gate & Sub-ms Deduplication:** Sub-millisecond multi-stage pre-embedding quality evaluation — idempotent auto-formatting (`auto_format_markdown`), prose extraction (`extract_prose_content`), Shannon character entropy ($H(X) \in [2.5, 5.3]$), Word 3-gram and 5-gram sequence repetition, Type-Token Ratio ($\ge 0.35$), Coleman-Liau readability bounds ($CLI \in [2.0, 26.0]$), and MSDI structural density scoring — followed by Stage A SHA-256 exact hash collision lookup before ONNX embedding generation. Calibrated cosine similarity ($\ge 0.75$) logs a reviewable `supersession_candidate` event rather than auto-linking or demoting weight (a prior auto-supersession design was reverted after it silently buried an unreviewed memory); crossing the stricter duplicate band ($\ge 0.85$) additionally auto-links a non-authoritative `similar_to` relation edge and warns the caller of a likely duplicate, while target exclusion prevents false deduplication warnings during parent memory consolidation.
- **Hybrid Search (FTS5 + Vector RRF):** Parallel FTS5/BM25 keyword search and `BAAI/bge-small-en-v1.5` dense vector search (via `fastembed` + `onnxruntime`) combined via Reciprocal Rank Fusion. Enabled by default; each search type runs on a dedicated thread pool. FTS5 uses a Porter tokenizer with title-biased BM25 weights (10:1 title-to-content, 5:1 alias-to-content). Semantic search uses a dedicated per-request connection to avoid cross-thread sqlite_vec conflicts.
- **Secrets Redaction:** Built-in regex scrubbing pipeline automatically redacts API keys, tokens, and private paths before any write. Custom patterns can be added via `.saltmdb_redact` in the working directory (one regex per line).
- **Folksonomy & Canonical Tags:** Flexible tagging with alias resolution, canonical redirects, and three seeded top-level tags (`episodic`, `semantic`, `procedural`).
- **SCD Type 2 Temporal History:** Every upsert preserves the prior version as an archived snapshot (`<entity_id>_h_<8-char-suffix>`) for full audit lineage.
- **Lossless Consolidation:** Soft-archives source memories, auto-creates `consolidated_from` graph edges — never hard-deletes.
- **Bi-Temporal Relations:** Relation edges carry both a system/transaction-time axis (`valid_from`/`valid_to`, set by consolidation) and an independent event/world-time axis (`valid_at`/`invalid_at`, settable directly by agents via `manage_relation(invalidate=True)`).

### 1. Database Schema
The SQLite database operates in **Write-Ahead Logging (WAL)** mode (`PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`). All writes use explicit `BEGIN IMMEDIATE` transactions with exponential backoff retry (up to 4 total attempts). It includes the following tables:

* **`events`**: An immutable, append-only ledger tracking agent operations (`decision`, `issue`, `fix`, `attempt`, `supersession_candidate`, `consolidation_request`, `domain_suggestion`, `consolidation_gate_override`, `relation_gate_override`). Columns: `id`, `timestamp`, `agent_id`, `type`, `content`, `error_code`, `session_id`, `context_id`.
* **`entities`**: The long-term knowledge base. Key columns: `id` (UUID), `title`, `full_content` (markdown), `status` (`raw`/`consolidated`/`archived`), `embedding_status` (`pending`/`ready`/`failed`/`archived`), `memory_type` (`fact`/`event`/`procedure`/`decision`/`preference`), `is_core`, `weight`, `scope` (`private`/`shared`), `owner_id`, `context_id`, `content_hash` (SHA-256), `quality_score`, `quality_status`, `quality_flags`, `valid_from`/`valid_to` (SCD Type 2 windows).
* **`tags`**: A folksonomy table allowing tags, categorizations, and canonical redirects. Seeded with `episodic`, `semantic`, `procedural`.
* **`entity_tags`**: A mapping table linking knowledge entities to folksonomy tags.
* **`relations`**: A typed directional edge table for the knowledge graph (`source_id → predicate → target_id`). Supports bi-temporal tracking via `valid_from`/`valid_to` (system time, set by `commit_consolidation`) and `valid_at`/`invalid_at` (event time, set by agents via `manage_relation`). A **partial unique index** `WHERE valid_to IS NULL` prevents duplicate live edges while allowing expired + live replacements to coexist.
* **`predicates`**: A canonical-predicate lookup table (mirrors `tags`' alias-resolution shape). Seeded with: `resolves`, `depends_on`, `references`, `elaborates_on`, `consolidated_from`, `supersedes`, `relates_to`, `similar_to`. `relates_to` and `references` are pre-aliased onto `elaborates_on`. Write-time predicate canonicalization via `resolve_or_create_predicate()` normalizes all submitted predicates before storage and notes any alias substitution in the result string.
* **`entities_fts`**: A virtual table using **SQLite FTS5** (Porter tokenizer) indexing `title`, `full_content`, and `search_aliases` (from `metadata.search_aliases`). Kept in sync with entities via four triggers (`insert_entity_fts`, `update_entity_fts`, `update_entity_fts_unarchived`, `archive_memory_fts`, `delete_entity_fts`).
* **`entity_embeddings`**: A `sqlite-vec` `vec0` virtual table storing 384-dimensional ONNX float32 embeddings (`embedding FLOAT[384]`). Loaded via `sqlite_vec.load(conn)` on a per-connection basis; the extension is pre-imported *before* any `BEGIN IMMEDIATE` transaction opens to prevent stalled cold-import from holding the write lock.
* **`_system_locks`**: A system table facilitating leader election mutex locks for concurrent Librarian processes. Columns: `task_name`, `locked_at`, `locked_by_pid`, `last_run_at`. Lock expiry: 10 minutes (stale safety net). Trigger cooldown: 5 minutes between subprocess spawns.
* **`_viewer_sessions`**: Tracks active web viewer sessions by `port` + `session_pid` for reference-counted lifecycle management.

---

## 🚀 Core Features

### 1. Hybrid FTS5 + Vector Search
SALTMDB runs FTS5/BM25 keyword search and dense vector semantic search **in parallel** using a `ThreadPoolExecutor`, merging results via **Reciprocal Rank Fusion (RRF)**:
* FTS5 uses SQLite's built-in `bm25` auxiliary function with a **10:1 title-to-content weight ratio** (alias weight: 5:1). An AND-query is tried first; if it returns no results with multiple terms, an OR-fallback is automatically applied.
* Semantic search uses `fastembed` (`BAAI/bge-small-en-v1.5`, 384-dim ONNX, ~66MB pre-bundled model weights) stored in a `sqlite-vec` `vec0` virtual table. The query text is `{title}\n\n{full_content}` concatenated for embedding.
* RRF merges on rank position (not raw scores) with `k=60`, keeping the existing BM25 tuning intact. Rows that matched via FTS5 carry a query-centered `fts_snippet` excerpt with `<mark>`/`</mark>` highlighting; rows that only surfaced via semantic search fall back to the heuristic snippet extractor.
* Relation count is factored into FTS5 ranking as a small boost: `ORDER BY (bm25 * weight + rel_count * 0.1) ASC`.
* Enabled by default; set `SALTMDB_ENABLE_SEMANTIC=false` to explicitly disable vector search. **Note**: `search_memory` no longer has an FTS-only fallback for query-based calls -- with semantic search disabled, a call that passes `query_keywords` returns `[{"error": "..."}]` instead of degraded FTS-only results. Filter/tag-only browsing (no `query_keywords`) is unaffected.
* Duplicate checks (`check_duplicate_memories`) use batched precomputed vector lookups (`_batch_semantic_similarities`) to avoid re-embedding each candidate, with FTS5 pre-filtering to cap candidates at ~30 before the similarity pass.
* Optional Stage-2 ONNX cross-encoder reranking (`search_memory`'s `use_cross_encoder`, experimental, opt-in): set `SALTMDB_RERANKER_MODEL` to a supported model name to enable. Independent of `rerank_by_topic` -- either flag alone widens the candidate pool and shares the same decisive-hybrid-winner gap-gate; if both are requested and neither is gated off, cross-encoder's reorder wins. No PyTorch runtime (uses `fastembed`'s existing ONNX `TextCrossEncoder` API). See the environment variable table below for supported model names. `Xenova/ms-marco-MiniLM-L-6-v2` (~88MB, pre-bundled under `src/saltmdb/models/`, same offline-first convention as the bi-encoder below) is the benchmark-recommended choice — it matched or beat every larger candidate tested (`BAAI/bge-reranker-base`, `jinaai/jina-reranker-v2-base-multilingual`, both 1GB+) on holdout top-1 accuracy at a fraction of the latency/footprint.

> [!NOTE]
> **Calibration caveat.** SALTMDB's constants fall into two different categories that must not be conflated:
> * **Benchmark-calibrated embedding thresholds** — `COHESION_MIN_PAIRWISE_THRESHOLD`, `CLUSTER_MIN_PAIRWISE_THRESHOLD`, `RERANK_SAME_TOPIC_THRESHOLD`/`RERANK_BROAD_THEME_THRESHOLD`, `DEDUP_SUPERSESSION_THRESHOLD`/`DEDUP_DUPLICATE_THRESHOLD`, `SUPERSESSION_MIN_SIMILARITY_THRESHOLD`, `RELATION_GATE_MIN_SIMILARITY_THRESHOLD` — are cosine-similarity cut points locked from English-language, codebase/engineering-domain benchmark corpora against this specific embedding model (`bge-small-en-v1.5`); they are defaults calibrated to that measurement, not a universal guarantee across other languages, content domains, or embedding models, and re-tuning any of them requires new benchmark evidence, not ad hoc adjustment.
> * **Cardinality / review-safety policy constants** — `MAX_CONSOLIDATION_REQUEST_SIZE`, `SUPERSESSION_MIN_OVERLAP_COUNT`, `COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION`, `COHESION_OVERRIDE_MIN_LENGTH` — are **not** embedding measurements; they're deliberately chosen operational safeguards (how many items is reviewable in one commit, how expensive an extraction pass is allowed to get, how long an override justification must be). These are reviewed and changed through normal policy/design review, not benchmark evidence.

### 2. Hybrid Title Extraction
When storing new knowledge, agents can optionally specify a custom `title`. If omitted, the server automatically extracts the first markdown heading (`# Heading`) as the title, falling back to a snippet of the first line if no heading is present. Title bounds: minimum 5 characters, maximum 120 characters.

### 3. Quality Gate Pipeline
All `store_memory` and `commit_consolidation` calls pass through a multi-tier quality gate before any embedding or write:
1. **Tier 1 — Boundary & Fluff:** Minimum length (20 chars), conversational fluff regex patterns, maximum symbol-to-alpha ratio (0.35), oversized payload warning (>8000 chars).
2. **Tier 1.5 — Markdown Syntax Integrity:** Balanced code fences, table pipe symmetry, header hierarchy validation (no level-skipping). Also scored: MSDI (Markdown Structural Density Index = ratio of words in headers+lists+code blocks to total words).
3. **Tier 2 — Information-Theoretic Filters:** Shannon entropy bounds (`[2.5, 5.3]` bits/char), 3-gram duplicate ratio (>0.30 → REJECT), 5-gram duplicate ratio (>0.20 → REJECT), Type-Token Ratio (>30 words: TTR < 0.35 → REJECT).
4. **Tier 2.5 — Coleman-Liau Readability (prose-only):** On extracted prose (code blocks, URLs, file paths stripped), if >30 prose words: CLI outside `[2.0, 26.0]` → REJECT.
5. **Tier 4 — Structural Scoring:** Base score 0.50; +0.15 for headers, +0.10 for lists, +0.15 for MSDI ≥ 0.35, −0.15 for MSDI < 0.10 on large (>80 word) text, −0.10 for untyped code blocks, −0.10 for non-hierarchical headers. Score clamped to `[0.0, 1.0]`.

`auto_format_markdown` runs as an idempotent pre-pass: normalizes line endings, annotates untyped code fences with language identifiers (Python/SQL/JSON/JavaScript heuristics), collapses 3+ consecutive blank lines.

### 4. Security & Redaction Middleware
Before any database writes occur, the text is evaluated by a regex-based scrubbing pipeline:
* **Core Redactions:** Automatically censors standard credentials (GitHub tokens, Anthropic API keys, OpenAI API keys, AWS credentials, Discord tokens).
* **Custom Developer Rules:** On startup, the server reads `.saltmdb_redact` from the current working directory. You can add one custom regex pattern per line (e.g. internal staging domains, proprietary IDs) to strip out company-specific secrets.

### 5. Ephemeral State Layer
For temporary data (like short-lived session tokens, OTPs, or process variables), the server maintains an isolated `:memory:` SQLite database (a module-level singleton on `connection.py`). These variables are never written to disk and disappear completely when the server stops.

### 6. Atomic Leader Election Mutex
To prevent multiple parent processes from launching redundant garbage collection tasks simultaneously, the server uses an **Atomic SQLite lock** in the `_system_locks` table.
* The lock uses a **10-minute expiry safety net**. If a terminal session crashes mid-run, the lock automatically expires, preventing permanent deadlocks.
* Trigger cooldown: the cooldown check runs on a single-worker `_librarian_trigger_pool` background thread (fire-and-forget), claiming the `last_run_at` timestamp atomically via a single `UPDATE ... WHERE last_run_at IS NULL OR last_run_at < now - 300s`. This prevents both redundant spawns and the old two-transaction lock-check pattern from adding contention to the hot path.

### 7. Automated Session Lifecycle Hooks
SALTMDB integrates with native lifecycle hooks across major AI agent frameworks (**Claude Code**, **Google Antigravity CLI**, and **GitHub Copilot CLI**):
* **Context Digest Injection (`SessionStart` / `PreInvocation` / `sessionStart`):** Automatically injects core rules and project memory digests at session initialization.
* **Pre-Action Memory Search Gate (`PreToolUse`):** Enforces Rule 1 ("Think Before You Leap") by requiring a memory search before executing code edits or terminal commands. Supports Copilot CLI's JSON `permissionDecision` (`allow`/`deny`) protocol.
* **Pre-Compaction Memory Sweeps (`PreCompact`):** Triggers autonomous background agent sweeps to persist unrecorded decisions and bug fixes before transcript truncation.
* **Stop Self-Critique Gate (`Stop` / `agentStop`):** Triggers mandatory self-reflection checks on confidence and unknown risks before finishing complex turns.

---


## 🧹 The Librarian Process (Garbage Collection)

Whenever the database is modified, the server schedules a fire-and-forget cooldown check on a background thread pool (`_librarian_trigger_pool`, 1 worker). If at least 2 raw entities exist and 5 minutes have elapsed since the last librarian spawn, a detached background subprocess is launched:

```
python -m saltmdb --librarian
```

* **Windows Detachment:** Spawns with `0x08000000` (`CREATE_NO_WINDOW`) to prevent distracting terminal window popups.
* **Unix Detachment:** Does not pass `start_new_session=True`; the subprocess's stdout/stderr are redirected to an append-mode `librarian.log` file in the same directory as `saltmdb.db` (rotated at 5 MB → `.1` backup). This allows debugging Librarian output while keeping it off the MCP stdio channel.

Once the background Librarian acquires the atomic lock, it runs three passes in order:

1. **Tag Merging (`merge_tags_heuristics`):** Merges case-insensitive, punctuation-stripped tag aliases (e.g. `#Auth-Error` and `#auth_error` normalize to `autherror`) into a canonical tag to prevent folksonomy fragmentation. Arbitrary SQL row order determines the canonical winner.

2. **Vector Topic Clustering (`consolidate_vector_clusters`, request-based):** Requires both `sqlite_vec` and `numpy`. Fetches fresh chunk-embedding centroids for all raw entities via `cohesion_service.get_fresh_entity_centroids` (`entity_chunk_embeddings`, not a stale doc-level cache). Builds a cosine similarity adjacency matrix using NumPy (`np.dot(X_norm, X_norm.T)`) and discovers connected components above a **0.75 cosine similarity threshold** with a minimum cluster size of **3 entities** via a BFS walk (`find_connected_vector_clusters`). Each component is then run through multi-subset cohesive extraction (`_extract_cohesive_clusters`): every disjoint subset whose own minimum pairwise similarity clears `CLUSTER_MIN_PAIRWISE_THRESHOLD` is peeled out separately, so one weak bridging edge can't chain two otherwise-unrelated cohesive groups into a single proposal. Components larger than `COHESION_MAX_COMPONENT_SIZE_FOR_EXTRACTION` (75) are skipped (logged, not proposed) rather than run through the O(k⁴)-worst-case extraction. For each extracted cluster not already covered by a pending request:
   * Any extracted cluster larger than `MAX_CONSOLIDATION_REQUEST_SIZE` (8) is deterministically split into several smaller, individually-reviewable requests before emission — a sorted-id balanced partition (e.g. 9→5+4, 10→5+5, 17→6+6+5), each re-scored independently (its own recomputed mean pairwise similarity, its own c-TF-IDF tags/confidence) rather than inheriting the parent cluster's values. This keeps a single `commit_consolidation` review from having to synthesize an oversized omnibus group.
   * Runs **c-TF-IDF** (`extract_c_tfidf_tags`) to extract the top-3 most cluster-specific terms: computes TF within the cluster and IDF against a 100-document corpus sample, applies standard TF-IDF scoring, then maps terms to existing canonical tags where available.
   * Computes a **composite confidence score**: `0.5 × mean_pairwise_similarity + 0.5 × c-TF-IDF_confidence`, where c-TF-IDF confidence = `clamp(0.5 + top_score × 0.5, 0.5, 1.0)`.
   * Logs a `consolidation_request` event with `target="vector_cluster"` and `suggested_tags`.
   * Also logs a separate `domain_suggestion` event with the full cluster membership and confidence score.

3. **Consolidated Supersession Scouting (`scout_consolidated_supersessions`):** For each consolidated entity with a ready embedding, queries for raw entities created *after* the consolidated node's `valid_from` date with cosine distance ≤ 0.25 (i.e., similarity ≥ 0.75). If ≥3 such new-raw entities overlap, and no pending supersession request covers this consolidated entity, logs a `consolidation_request` event with `target="supersession_candidate"`.

**Maintenance pass (`_run_librarian_maintenance`):** Runs unconditionally after all consolidation passes (even on partial failure), while the leader lock is still held: `PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA optimize=0x10002`.

### No LRU Decay
Memories are **never** weight-decremented or archived due to inactivity or disuse. Archiving occurs only upon explicit supersession or synthesis consolidation. The previously-present `decay_low_quality_memories` function was removed in alpha.62 as confirmed-dead code.

---

## 🛠️ API & MCP Tools Reference

The server exposes **13 consolidated tools** over standard I/O (stdio MCP):

| Tool Name | Key Parameters | Description |
| :--- | :--- | :--- |
| `search_memory` | `query_keywords`, `tags_filter`, `owner_id`, `entity_id`, `fetch_full`, `limit`, `context_id`, `is_core`, `memory_type_filter`, `cursor`, `include_related`, `rerank_by_topic` | Hybrid FTS5 + vector RRF search. Setting `entity_id` retrieves full markdown text directly; `fetch_full=True` without `entity_id` is a no-op (falls through to normal search). `include_related=True` (default) batches 1-hop active linked entities in a single query. `rerank_by_topic` (alias `rerank`, default `False`) widens the Stage-1 candidate pool to `RERANK_CANDIDATE_POOL_SIZE` (20), scores the full pool via `rerank_candidates_by_topic` using precomputed chunk embeddings (entities without chunk rows fall back to entity-level similarity, capped at `"BROADLY_RELATED_THEMES"` — never `"SAME_SPECIFIC_TOPIC"`), and **fully re-orders** results by `topic_score` descending (not a blend with the FTS/vector RRF ranking). Each result gains `topic_score` and `semantic_verdict` (`"SAME_SPECIFIC_TOPIC"` ≥ 0.7680, `"BROADLY_RELATED_THEMES"` ≥ 0.5322, else `"DIFFERENT_TOPICS"`). Silently a no-op under `explain_mode=True`, semantic-search-disabled, or an empty `query_keywords`. |
| `store_memory` | `content`, `title`, `tags`, `is_core`, `memory_type`, `owner_id`, `context_id`, `scope`, `check_duplicates_only` | Stores/upserts facts in raw markdown. Enforces quality gates and SHA-256 hash deduplication, logs reviewable supersession-candidate signals (≥0.75), auto-links `similar_to` edges (≥0.85), triggers background embedding generation. `check_duplicates_only=True` returns duplicate detection without writing. On brand-new stores with `tags`, appends a one-time nudge to consider `manage_relation`. |
| `get_canonical_tags` | `query` (alias: `domain`), `limit` (default 50) | Queries non-alias tags matching the search filter to prevent tag fragmentation. Capped by `limit` even when `query` is omitted. |
| `get_canonical_predicates` | `query`, `limit` (default 50) | Queries existing canonical relation predicates matching a search substring, to reduce predicate drift. Capped by `limit` even when `query` is omitted. |
| `merge_tags` | `keep_tag`, `tags_to_merge` (list) | Merges one or more fragmented/synonym tags into an explicitly chosen canonical tag, repointing all affected entity_tags associations. |
| `log_event` | `agent_id`, `type`, `content`, `error_code`, `session_id`, `context_id` | Appends a scrubbed entry to the immutable short-term ledger. Aliases: `event_type`, `message`, `description`. |
| `get_events` | `agent_id`, `type_filter`, `session_id`, `limit`, `offset`, `status_filter`, `owner_id`, `mode` | Query events (`mode='events'`, default), session summaries (`mode='session'`), or scan memory logs (`mode='memories'`). Reviewable `consolidation_request`/`supersession_candidate` event items carry a computed top-level `status`: `'dismissed'` (an `event_dismissed` record targets it, wins over natural resolution), `'resolved'` (relevant source entities — `content.entity_ids`/`content.new_raw_entity_ids` for `consolidation_request`, `content.new_entity_id` for `supersession_candidate` — are no longer `status='raw'`), or `'pending'` otherwise. `status_filter` (`mode='events'`) filters on this computed status. |
| `archive_memory` | `entity_id` (str \| list[str]), `owner_id` | Polymorphic: archives one or multiple long-term memories. Bulk archive is all-or-nothing (a single failed item rolls back the entire batch). Archiving also sets `valid_to` on all active outgoing/incoming relation edges. |
| `manage_relation` | `relations` (list), `source_id`, `target_id`, `predicate`, `invalidate`, `valid_at`, `invalid_at`, `override_justification`, `owner_id` | Polymorphic: store single or multiple directional semantic relationship edges (bulk via `relations` list), or invalidate an existing live edge (`invalidate=True`, sets `invalid_at` on the event/world-time axis; does not touch `valid_to`). Predicate strings are canonicalized at write time via `resolve_or_create_predicate()`. **Governance gate** (memory-core rework Phase 5): applies only to `RELATION_GATE_STRONG_PREDICATES` (`elaborates_on`, `resolves`, `supersedes`), rejecting with `REJECT_LOW_RELATION_SIMILARITY` when source/target centroid similarity is below `RELATION_GATE_MIN_SIMILARITY_THRESHOLD` (0.6505); independently, any predicate is rejected with `REJECT_CONTRADICTORY_PREDICATE` if it and an existing predicate on the same directional edge form a pair in `RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS` (currently just `{supersedes, elaborates_on}`). Re-submitting an already-active identical edge always short-circuits as a no-op before the gate runs. `owner_id` (defaults `"system"`) becomes the audit event's `agent_id`. `override_justification` (≥ `COHESION_OVERRIDE_MIN_LENGTH` = 20 chars, no separate relation-gate constant) force-passes any violation and is stored **only** in the audit event (relations have no free-text body to append to, unlike consolidation). Bulk calls via the `relations` list thread `override_justification`/`owner_id` **per item**, never as one batch-level value; a single item failing aborts the whole bulk call all-or-nothing (`RuntimeError`, no partial apply). Logs a `relation_gate_override` event (`source_id`, `target_id`, `predicate`, `violations`, `similarity`, `similarity_threshold`, `contradicting_predicates`, `justification`, `relation_id`), atomic with the relation insert. |
| `commit_consolidation` | `consolidations` (list), `parent_ids`, `title`, `content`, `tags`, `owner_id`, `context_id`, `override_justification` | Polymorphic: commit single or multiple synthesized consolidations. Soft-archives parents, creates `consolidated_from` lineage edges, expires old relation edges and re-inserts them pointing at the consolidated entity. Triggers background embedding generation for the new consolidated entity. `title` and `content` are mandatory on every call — no ID-only shortcut. **Cohesion gate** (memory-core rework Phase 3): for ≥2 resolved parents, rejects with `REJECT_LOW_COHESION` when the minimum pairwise centroid cosine similarity falls below `COHESION_MIN_PAIRWISE_THRESHOLD` (0.6547), naming the offending pair. An unresolved (unembeddable) parent forces a 0.0 similarity, guaranteeing rejection without an override. Passing `override_justification` (≥ `COHESION_OVERRIDE_MIN_LENGTH` = 20 chars) force-passes the gate, appends the justification text verbatim into the committed entity's `content` (a permanent `[Consolidation Override]` block, not just an audit trail), and atomically logs a `consolidation_gate_override` event (`parent_ids`, `min_pairwise_similarity`, `threshold`, `offending_pair`, `unresolved`, `justification`, `consolidated_id`) as the first write in the same transaction — a logging failure rolls back the whole commit. |
| `inspect_graph` | `entity_id` (optional), `mode` (`dependencies` \| `lineage` \| `orphans`), `max_depth`, `owner_id`, `point_in_time` | Unifies dependency CTE traversals, consolidation lineage tracing (`consolidated_from` edges, recursive CTE), and orphan memory detection. `point_in_time` (aliases `as_of`/`at`) filters `dependencies`/`lineage` to relation edges valid as of a past ISO timestamp (checks `valid_to`, `valid_from`, `invalid_at`, `valid_at`); ignored for `mode='orphans'`. |
| `ephemeral_memory` | `action` (`get` \| `store`), `key`, `value` | Unified volatile in-memory secret storage manager. Backed by a module-level singleton `:memory:` SQLite connection. |
| `dismiss_event` | `event_id`, `reason`, `agent_id` | Appends an `event_dismissed` record to safely mark one or multiple pending review signals — `consolidation_request` (covers the `vector_cluster`, `supersession_candidate`, and historical tag/general `content.target` flavors) or the top-level `supersession_candidate` event type — as dismissed. This allows cleaning up obsolete operational backlogs without mutating live entities or deleting original events. Idempotent and atomic in bulk mode. |


---

## ⚙️ Configuration & Installation

### 1. Configuration Path
By default, the server initializes the database under `~/.saltmdb/saltmdb.db`. You can override this behavior by setting the `SALTMDB_DB_PATH` environment variable:
```bash
# Unix
export SALTMDB_DB_PATH="/custom/path/memory.db"
# Windows PowerShell
$env:SALTMDB_DB_PATH = "C:\custom_path\memory.db"
```

### 2. Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SALTMDB_DB_PATH` | `~/.saltmdb/saltmdb.db` | Path to the SQLite database file. |
| `SALTMDB_ENABLE_SEMANTIC` | `true` | Set to `false`/`0`/`off`/`no` to disable vector search. Query-based `search_memory` calls then return an error instead of an FTS-only fallback -- see the Search Architecture section above. |
| `SALTMDB_RERANKER_MODEL` | _(unset)_ | Experimental, opt-in: set to an ONNX cross-encoder model name (`Xenova/ms-marco-MiniLM-L-6-v2`, `Xenova/ms-marco-MiniLM-L-12-v2`, `BAAI/bge-reranker-base`, `jinaai/jina-reranker-v1-tiny-en`, `jinaai/jina-reranker-v1-turbo-en`, or `jinaai/jina-reranker-v2-base-multilingual`) to enable `search_memory`'s `use_cross_encoder` Stage-2 reranking flag. Unset (default) or an unsupported name leaves `use_cross_encoder` a no-op. No PyTorch runtime -- uses `fastembed`'s existing ONNX Runtime backend, the same one already used for the bi-encoder. |
| `SALTMDB_VIEWER_PORT` | `8080` | Port for the database dashboard viewer. |
| `SALTMDB_VIEWER_HOST` | `127.0.0.1` | Bind host for the viewer (loopback-only by default). |
| `SALTMDB_VIEWER_ENABLED` | `true` | Set to `false` to disable auto-start of the viewer on MCP server startup. |
| `SALTMDB_DISABLE_LIBRARIAN` | _(unset)_ | Set to any value to suppress all Librarian subprocess spawns. |
| `SALTMDB_TEST_MODE` | _(unset)_ | Set to any value in test environments to suppress Librarian spawns. |

### 3. Registering with MCP Clients
MCP clients do not inherit your shell's PATH — always use the **full path** to your Python executable. Find it first:
```bash
python -c "import sys; print(sys.executable)"
```

Then add to your MCP client configuration file:
```json
"mcpServers": {
  "saltmdb": {
    "command": "/full/path/to/python",
    "args": ["-m", "saltmdb"],
    "env": {
      "SALTMDB_DB_PATH": "/path/to/saltmdb.db",
      "SALTMDB_ENABLE_SEMANTIC": "true"
    }
  }
}
```

See [INSTALL.md](INSTALL.md) for platform-specific examples and troubleshooting.

### 4. Database Dashboard Viewer
SALTMDB includes a sleek, zero-dependency dark-mode dashboard to inspect events, memories, tags, system locks, **Lineage Explorer (tree & graph)**, and **interactive SVG Force-Directed Relations Topology**:
1. Run the viewer:
   ```bash
   python -m saltmdb.viewer.server
   # or if installed via pip install -e .:
   saltmdb-viewer
   ```
   Override the default port with `--port <PORT>` or the `SALTMDB_VIEWER_PORT` environment variable.
2. Open your web browser and navigate to:
   [http://localhost:8080](http://localhost:8080)

### 5. Running Unit Tests
Run the hybrid search test suite (against the refactored package):
```bash
python -m unittest discover tests
```

---

## 📄 License & Community

* **License:** Distributed under the **[GNU Affero General Public License v3 (AGPLv3)](LICENSE)**.
* **Contributing:** Read the **[Contributing Guidelines](CONTRIBUTING.md)** for details on testing and branch setups.
* **Conduct:** We adhere to the **[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md)**.