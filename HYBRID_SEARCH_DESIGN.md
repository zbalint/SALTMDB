# Design Document: Hybrid FTS5 + Semantic Search with RRF

**Status:** Design for review/implementation.
**Scope:** Adds vector-based semantic search alongside the existing FTS5/BM25
keyword search in `search_memory`, merged via Reciprocal Rank Fusion (RRF).

**Grounding note:** This document was written against the actual code on the
`refactor` branch as of this review (`src/saltmdb/db/schema.py`,
`src/saltmdb/domain/services/memory_service.py`,
`src/saltmdb/db/connection.py`, `pyproject.toml`). Line/function references
below point at real code, not an assumed or idealized version of it. Where
the current code diverges from earlier design discussions (e.g. `owner_id`
is still filter-enforced, `search_aliases` still lives in `metadata` JSON
rather than a dedicated table), this is noted explicitly rather than
silently assumed away — those are separate, pre-existing pieces of scope,
not part of this document's changes, and are called out only where they
interact with the search design below.

---

## 1. Objective

Keyword search (FTS5 + BM25) cannot bridge queries and memories that
describe the same fact with disjoint vocabulary (confirmed real pattern in
testing — e.g. a query phrased around a symptom failing to match a memory
stored around its technical diagnosis). Semantic (embedding-based) search
closes this gap. The two methods have complementary failure modes — FTS5 is
fast, precise, and free of any model dependency but purely lexical; semantic
search is vocabulary-independent but requires an embedding model and has
non-trivial infra cost — so this design **adds semantic search as a second,
parallel signal**, merged with FTS5 results via RRF, rather than replacing
FTS5.

**Explicitly not solved by this document:** duplicate detection, the
supersession pipeline, consolidation clustering. Those consume the same
embedding infrastructure (Section 3) but are separate services
(`memory_service.check_duplicate_memories`, `librarian_service.py`) with
their own call sites and are out of scope here except where Section 3's
infra choices affect them directly.

---

## 2. Current State (as of `refactor` branch)

### 2.1 What exists today

- `entities` table (`db/schema.py:27`) with `title`, `full_content`,
  `status`, `weight`, `owner_id`, `scope`, `context_id`, `project_id`,
  `metadata` (JSON), `is_core`.
- `entities_fts` (`db/schema.py:104`) — FTS5 virtual table, Porter
  tokenizer, columns `title`, `full_content`, `search_aliases` (the latter
  populated from `json_extract(metadata, '$.search_aliases')` via triggers
  at insert/update time — schema.py:150-197).
- `search_memory()` (`memory_service.py:231`) — builds a dynamic `WHERE`
  clause from filters (`owner_id`, `context_id`/`project_id`,
  `metadata_filter`, `tags_filter`, `is_core`), then either:
  - Runs an FTS5 `MATCH` query with BM25 ranking
    (`bm25(entities_fts, 10.0, 1.0, 5.0)`, weighting title 10x, full_content
    1x, search_aliases 5x) when `query_keywords` is provided, with an
    AND→OR fallback if the strict AND match returns zero rows
    (`memory_service.py:376-381`), **or**
  - Falls back to a plain filtered `SELECT` ordered by `is_core DESC,
    updated_at DESC` when no keywords are given.
  - Already supports `limit`, `cursor`-based pagination, `include_related`
    (pulls linked entities via `relations`), and an `explain_mode` for
    diagnosing why a query matched/didn't.
- `db/connection.py:19` — `get_connection()` opens a **new SQLite
  connection per call**, with WAL, `busy_timeout`, and other pragmas set
  each time. There is no persistent app-level connection pool; every
  service function that needs the DB calls `get_connection(db_path)`
  itself unless a connection is explicitly passed in
  (`db_connection` parameter pattern visible throughout `memory_service.py`).
- No embedding infrastructure exists yet. No `sqlite-vec`, no embedding
  model dependency in `pyproject.toml` (currently only `mcp>=0.1.0`).

### 2.2 Implications for this design, stated explicitly

- **The per-call connection pattern matters for where embedding generation
  and vector search get wired in.** Any new DB access this design adds
  should follow the same `db_connection`-or-`get_connection(db_path)`
  pattern already used everywhere else in `memory_service.py`, for
  consistency — not a new, different connection-handling convention.
- **`search_memory`'s existing signature and return shape must be
  preserved or additively extended, not replaced** — it's called from
  `mcp/tools.py` and likely other call sites; breaking its contract is out
  of scope for this document, which is additive only.
- **The current BM25 weighting (10.0 / 1.0 / 5.0 for
  title/full_content/search_aliases) is a tuned, existing decision.** This
  document does not change it; RRF merging (Section 5) is deliberately
  chosen partly *because* it doesn't require touching or re-deriving this
  existing tuning — RRF operates on rank order, not raw BM25 score
  magnitude, so the semantic layer can be added without needing to
  renormalize or reconcile it against this weighting.

---

## 3. Embedding Infrastructure

### 3.1 Dependencies

Add to `pyproject.toml`:
```toml
dependencies = [
    "mcp>=0.1.0",
    "sqlite-vec>=0.1.0",
    "sentence-transformers>=3.0.0",
]
```

**Explicit, non-optional caveats — do not treat this as "two small
dependencies":**
- `sentence-transformers` pulls in `torch` (CPU build, but still
  commonly 150–700MB depending on platform) plus `transformers`,
  `huggingface-hub`, `tokenizers`, `numpy`. Verify actual installed size
  in a clean venv before treating this as a minor addition — run
  `pip install sentence-transformers sqlite-vec` in an isolated
  environment and check `du -sh` on the result as part of implementation
  sign-off, don't estimate.
- On Termux/Android (aarch64-unknown-linux-android), `torch` and other
  compiled dependencies may not have prebuilt wheels and can require a
  native Rust/build toolchain (`pkg install rust`, `pkg install cmake`) to
  build from source, mirroring issues already hit with `pydantic-core`
  during MCP SDK installation on that platform. Document this in
  `INSTALL.md` if Termux is a supported dev target.
- This moves the project away from its current "pure stdlib SQLite,
  offline-first, lightweight" positioning (`pyproject.toml` description:
  "Local-First MCP Memory Server"). Update `README.md` to state this
  tradeoff plainly once implemented — do not let the install footprint
  change silently.

### 3.2 Model choice

**`BAAI/bge-small-en-v1.5`** — 33M parameters, 384-dimensional output,
small footprint (~130MB weights), fast CPU inference. Chosen over
`nomic-embed-text-v1.5` (137M params, ~4x larger) because SALTMDB's actual
stored content (titles, snippets, short-to-medium memory bodies) does not
need nomic's 8,192-token context advantage, which matters more for
embedding long documents unchunked. Revisit only if `full_content` values
routinely exceed a few thousand tokens (e.g. large consolidated memories
embedded whole without chunking).

### 3.3 Schema changes

Add a new module, `src/saltmdb/db/vector_schema.py` (kept separate from
`db/schema.py`'s relational schema for clarity, called from the same
`init_db()` entrypoint):

```python
def init_vector_schema(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension and create the embeddings virtual table."""
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS entity_embeddings USING vec0(
            entity_id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        );
    """)
```

Add an `embedding_status` column to `entities`, following the exact
migration pattern already used at `db/schema.py:47-51`:
```python
for col in ["embedding_status TEXT DEFAULT 'pending'"]:
    try:
        conn.execute(f"ALTER TABLE entities ADD COLUMN {col};")
    except sqlite3.OperationalError:
        pass
```
Values: `'pending'` (not yet embedded — the default for all rows, including
pre-existing ones after migration), `'ready'`, `'failed'`.

**Why a separate `entity_embeddings` table, not a column:** `sqlite-vec`'s
`vec0` virtual tables have different storage/indexing internals than a
normal column; this mirrors exactly how `entities_fts` already exists as a
separate table from `entities`, kept in sync rather than merged — same
established pattern in this codebase, not a new one.

### 3.4 Embedding generation — write path

New module `src/saltmdb/domain/services/embedding_service.py`:

```python
from sentence_transformers import SentenceTransformer
import threading

_model_lock = threading.Lock()
_model = None

def get_model() -> SentenceTransformer:
    """Lazily load the embedding model once per process."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # re-check inside lock
                _model = SentenceTransformer("BAAI/bge-small-en-v1.5")
    return _model

def embed_text(text: str) -> list[float]:
    model = get_model()
    return model.encode(text, normalize_embeddings=True).tolist()

def embed_entity_async(entity_id: str, title: str, full_content: str, db_path: str) -> None:
    """Runs in a background thread; generates and stores an embedding for one entity."""
    from saltmdb.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        text = f"{title}\n\n{full_content}"
        vector = embed_text(text)
        conn.execute(
            "INSERT OR REPLACE INTO entity_embeddings(entity_id, embedding) VALUES (?, ?)",
            (entity_id, sqlite_vec.serialize_float32(vector))
        )
        conn.execute(
            "UPDATE entities SET embedding_status = 'ready' WHERE id = ?",
            (entity_id,)
        )
        conn.commit()
    except Exception as e:
        conn.execute(
            "UPDATE entities SET embedding_status = 'failed' WHERE id = ?",
            (entity_id,)
        )
        conn.commit()
        logger.error("Embedding generation failed for %s: %s", entity_id, e)
    finally:
        conn.close()
```

**Call site — `memory_service.store_memory()`:** after the existing insert
logic completes and the function is about to return, spawn the embedding
job as a background thread (not a subprocess — the model stays loaded via
the module-level lazy singleton in 3.4, so a thread avoids repeated
model-load cost that a fresh subprocess-per-call would incur, unlike the
existing `trigger_librarian` subprocess pattern which is fine for a
short-lived, infrequent task but wrong for this higher-frequency one):

```python
import threading
threading.Thread(
    target=embedding_service.embed_entity_async,
    args=(entity_id, title, full_content, db_path),
    daemon=True
).start()
```

`store_memory()` returns immediately; embedding happens after, out of the
critical path. Consistent with existing `NOT NULL` validation already
enforced by `validate_memory_input()` (`memory_service.py:20`) — embeddings
are only ever generated for validated, non-empty content, since this runs
after that validation already passed.

**Backfill for pre-existing rows:** a one-time migration script
(`scratch/backfill_embeddings.py` or similar, run manually, not part of
`init_db()`) that selects all `entities` where `embedding_status = 'pending'`
and processes them in a loop — needed once, at rollout, for any data that
existed before this feature shipped.

### 3.5 Failure handling

If `embed_text()` raises (model load failure, OOM, malformed input):
`embedding_status = 'failed'`, logged via the existing `logger` pattern
already used in `memory_service.py:425`. Keyword search is entirely
unaffected — `search_memory`'s FTS5 path has no dependency on embedding
status. No automatic retry loop in this version; a `'failed'` row can be
manually or Librarian-swept later (out of scope for this document — note
as a follow-up, not a blocker).

---

## 4. Semantic Search — Read Path

### 4.1 Function: `semantic_search()`

New function in `memory_service.py` (or a new `search_service.py` if the
file is getting large — `memory_service.py` is already ~32K, worth
considering the split as part of this change, not deferred):

```python
def semantic_search(
    query: str,
    where_clauses: list[str],
    params: list,
    limit: int,
    db_connection,
) -> list[tuple[str, float]]:
    """Returns [(entity_id, distance), ...] ordered by ascending distance (closer = more similar)."""
    query_vector = embedding_service.embed_text(query)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    sql = f"""
        SELECT e.id, vec_distance_cosine(ee.embedding, ?) as distance
        FROM entity_embeddings ee
        JOIN entities e ON ee.entity_id = e.id
        WHERE e.embedding_status = 'ready' AND {where_sql}
        ORDER BY distance ASC
        LIMIT ?
    """
    exec_params = [sqlite_vec.serialize_float32(query_vector)] + params + [limit]
    rows = db_connection.execute(sql, exec_params).fetchall()
    return [(row[0], row[1]) for row in rows]
```

**Reuses the exact same `where_clauses`/`params` construction already built
in `search_memory()` (lines 263-328)** — owner/context/tag/metadata
filtering applies identically to both the FTS5 and semantic branches. This
is a deliberate design choice: filters should mean the same thing
regardless of which ranking method is used, and building the WHERE clause
once, then handing it to both search paths, guarantees that rather than
requiring it be kept in sync by hand across two separate implementations.

**Note on `entity_embeddings ee JOIN entities e`:** this means a memory
whose embedding is still `'pending'` (not yet generated) simply won't
appear in semantic results — no error, no special handling needed at the
call site, it just isn't in the join. This is the intended, accepted
behavior per Section 3.5 — no retry-signaling response field is added in
this version (see Section 6 for why this was deliberately simplified
relative to earlier design discussion).

### 4.2 Modifying `search_memory()` to run both searches

Current code (`memory_service.py:356-395`) branches on whether
`query_keywords` is provided at all. New logic: **when `query_keywords` is
provided, run FTS5 and semantic search in parallel**, then merge:

```python
import concurrent.futures

if sanitized_query:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fts_future = executor.submit(_run_fts_search, conn, sanitized_query, where_clauses, params, limit, offset)
        semantic_future = executor.submit(semantic_search, query_keywords, where_clauses, params, limit, conn)

        fts_rows = fts_future.result()
        semantic_rows = semantic_future.result()

    merged_ids = reciprocal_rank_fusion(fts_rows, semantic_rows, limit)
    # fetch full row data for merged_ids in original SQL-result shape, preserving
    # existing downstream code at lines 397-421 (result dict construction,
    # include_related handling) unchanged
else:
    # existing no-keyword filtered-list branch (memory_service.py:382-395) — unchanged
```

`_run_fts_search` is the existing FTS5 query logic (lines 357-381,
including the AND→OR fallback), extracted into its own function so it can
be run inside the thread pool without restructuring what it already does
correctly.

**Why `ThreadPoolExecutor` and not `asyncio`:** the rest of
`memory_service.py` is synchronous, uses blocking `sqlite3` calls
throughout, and the MCP tool layer (`mcp/tools.py`) is not shown to be
async-native in what's been reviewed. Introducing `asyncio` here would
require it to be threaded through the whole call stack to be worthwhile;
a thread pool for exactly two blocking calls (one SQLite FTS5 query, one
SQLite vector query + one embedding model inference call) is a much
smaller, self-contained change that fits the existing synchronous
codebase without a wider async migration. Revisit only if the MCP layer
is confirmed to already be async end-to-end.

### 4.3 Reciprocal Rank Fusion

```python
def reciprocal_rank_fusion(
    fts_results: list,
    semantic_results: list[tuple[str, float]],
    limit: int,
    k: int = 60,
) -> list[str]:
    """Merges two ranked result lists by rank position, not raw score.
    fts_results: sqlite3 Row objects from the existing FTS5 query (id at index 0).
    semantic_results: [(entity_id, distance), ...], already sorted ascending by distance.
    Returns a list of entity_ids in merged rank order, length <= limit.
    """
    scores: dict[str, float] = {}

    for rank, row in enumerate(fts_results):
        entity_id = row[0]
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)

    for rank, (entity_id, _distance) in enumerate(semantic_results):
        scores[entity_id] = scores.get(entity_id, 0.0) + 1.0 / (k + rank + 1)

    ranked = sorted(scores.items(), key=lambda item: -item[1])
    return [entity_id for entity_id, _ in ranked[:limit]]
```

**Why RRF specifically, not weighted score blending:** BM25 scores (via
`bm25(entities_fts, 10.0, 1.0, 5.0)`, an unbounded, weighting-dependent
scale) and cosine distance (0.0–2.0, or similarity 0.0–1.0 depending on
normalization) are not on comparable scales, and there is no principled
conversion between them without empirical tuning data this project does
not yet have. RRF avoids the problem entirely by merging on **rank
position**, not raw score value — no normalization or reconciliation of
the existing BM25 weighting (Section 2.2) is needed. `k=60` is the
standard default from the RRF literature; treat as a starting point, not
a final tuned value — **do not hand-tune `k` without real query/outcome
data**, per the project's existing "don't build for an unconfirmed gap"
principle. If RRF's ranking is later shown to be systematically wrong in
a specific, identifiable direction (e.g. FTS5 exact matches getting
buried under weak semantic matches), that is the trigger to revisit
weighted blending — not a preemptive concern to solve now.

### 4.4 Row-fetch after merge

After `reciprocal_rank_fusion()` returns an ordered list of entity IDs, a
single follow-up query fetches full row data for exactly those IDs, in
that order (`SELECT ... WHERE e.id IN (...)`, then re-sorted in Python to
match `merged_ids` order, since SQL's `IN` does not preserve list order).
This keeps the existing result-construction code at
`memory_service.py:397-421` (snippet extraction, `include_related`
handling, cursor field) entirely unchanged — it operates on the same row
shape it already expects, regardless of whether those rows came from a
pure-FTS5 query or a merged one.

---

## 5. Explicit Non-Goals / Deliberate Simplifications

Stated explicitly so they are not mistaken for oversights during review:

1. **No agent-facing retry/pending-embedding signaling in this version**
   (`pending_embeddings` count, `graph_exhausted`-style hints). An earlier
   design iteration considered this, but was superseded by the
   parallel-search approach specifically *because* it requires no
   agent-side judgment call at all — the merge happens server-side,
   unconditionally, every call. Do not reintroduce agent-driven retry
   logic without a specific new justification; it was deliberately
   dropped as unnecessary complexity once the parallel-merge design was
   adopted.
2. **No fallback if the embedding model fails to load at server startup**
   in this version — if `sentence_transformers.SentenceTransformer(...)`
   raises on first `get_model()` call, `embed_text()` propagates the
   exception, `embed_entity_async` catches it and marks `'failed'`
   (Section 3.5), and `semantic_search()` at query time simply finds no
   rows in `entity_embeddings` — keyword search continues to function.
   No explicit "embedding subsystem is down" alerting is added; consider
   as a follow-up if this proves to be a real operational issue.
3. **No re-ranking or cross-encoder step after RRF.** The merged list is
   the final result. A cross-encoder re-ranking stage was discussed as a
   possible future refinement but is out of scope — do not add without
   first confirming RRF's output quality is actually insufficient in
   practice.
4. **This document does not address duplicate detection or the
   consolidation-clustering/supersession pipeline.** Those consume
   `entity_embeddings` (Section 3) as shared infrastructure but are
   separate pieces of work with their own design needs (candidate
   funneling via tags → vector distance → small-model cross-check →
   agent confirmation event) not detailed here.

---

## 6. Testing Requirements

Per the project's existing gap in concurrency testing
(`scratch/test_db.py` has no multi-process/multi-thread coverage as of
this review) — **do not let this feature ship with the same gap**:

1. **Correctness:** unit test `reciprocal_rank_fusion()` directly with
   hand-constructed FTS/semantic result lists — verify merge order for
   known inputs (item in both lists ranks higher than item in only one;
   item absent from both lists doesn't appear).
2. **Embedding write-path race:** test that `store_memory()` returns
   before embedding generation completes (assert on timing, not just
   final state) and that a `search_memory()` call issued immediately
   after a `store_memory()` call does not error even though the
   embedding is still `'pending'` — it should simply return FTS5-only
   results for that item until the background thread finishes.
3. **Concurrent writes:** multiple threads calling `store_memory()`
   simultaneously should not corrupt `entity_embeddings` or leave
   `embedding_status` in an inconsistent state — extend
   `scratch/test_db.py` with a real multi-threaded test here, not just a
   sequential one (mirrors the existing, separately-tracked gap around
   `_system_locks` testing).
4. **Empty-embedding-table behavior:** `search_memory()` with keywords,
   before any embeddings have been backfilled/generated (fresh install or
   mid-migration), should return FTS5-only results with no errors —
   `semantic_search()` against an empty `entity_embeddings` table must
   return `[]`, not raise.

---

## 7. Rollout Sequencing

1. Add dependencies, verify actual install footprint (Section 3.1) on
   both primary dev targets (desktop and Termux, given both are in active
   use per project history) before merging.
2. Ship schema changes (`entity_embeddings`, `embedding_status`) — safe,
   additive, no behavior change yet since nothing reads/writes them.
3. Ship write-path embedding generation (Section 3.4) — memories start
   getting embedded on write; still no read-path change, so no user-facing
   behavior change yet. Verify via direct DB inspection that
   `embedding_status` transitions `pending → ready` correctly before
   proceeding.
4. Run the one-time backfill script against existing data.
5. Ship read-path changes (Sections 4.2–4.4) behind a config flag if the
   codebase has a pattern for that (check `config.py`); otherwise ship
   directly once 1–4 are confirmed working, since the fallback behavior
   (empty embeddings table → FTS5-only results) makes this safe to enable
   without a flag if preferred.
6. Update `README.md` and `INSTALL.md` per Section 3.1's transparency
   requirement.
