# SPEC-CONTEXT-RETRIEVAL-C-COMMUNITY-DETECTION

## 0. Status

**LOCKED**

**Scope**: may edit `pyproject.toml` (add exactly two new runtime dependencies, `leidenalg` and
`python-igraph`, see §2); may edit `src/saltmdb/config.py` (add exactly one new constant,
`COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`, see §3); may edit `src/saltmdb/db/schema.py` (add two
new relational tables, `communities` and `community_membership`, plus one new `_system_locks` seed
row, see §4); may edit `src/saltmdb/db/vector_schema.py` (add one new function,
`init_community_vector_schema`, creating the `community_embeddings` vec0 virtual table, see §5);
may edit `src/saltmdb/domain/services/relation_service.py` (exactly two small additions — a
fire-and-forget trigger call near the end of `store_relation` and of `invalidate_relation`, see
§7); may create `src/saltmdb/domain/services/community_detection_service.py` (new file, see §6)
and `tests/test_community_detection_service.py` (new file, see §8); may edit
`tests/test_relation_service.py` (add exactly three new test scenarios per §8 items 22-24,
covering the two trigger-call additions §7 makes to `store_relation`/`invalidate_relation` — no
other change to this file; see Amendment 1). Does not touch:
`src/saltmdb/domain/services/context_expansion_service.py`,
`conflict_set_service.py`, `context_budget_service.py`, `lineage_assembly_service.py`,
`retrieve_context_service.py` (Milestone C exposes nothing to `retrieve_context` at all — standing
constraint 17; Milestone C.5's own separate spec is the one that touches
`retrieve_context_service.py`), `src/saltmdb/mcp/tools.py` / `src/saltmdb/daemon/dispatch.py` /
`src/saltmdb/daemon/protocol.py` (no new MCP tool surface — constraint 17), and
`src/saltmdb/domain/services/librarian_service.py` itself (its existing `_librarian_trigger_pool`
is imported and reused as-is — no signature change, no new wrapper — per constraint 18's own named,
accepted "shared pool" tradeoff).

**Pre-lock gate completed against the current tree** (spec-writing skill): every section below was
drafted in Why → mechanical → tests → Out of scope → Acceptance order before this Status section
was finalized. Grepped `communities`, `community_membership`, `community_embeddings`,
`community_detection_service`, `recompute_communities`, `trigger_community_detection`,
`COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`, `leidenalg`, `igraph` across the tree: zero prior
occurrences of any of them, confirming none collide with existing code, tables, or stale test
literals. `store_relation`/`invalidate_relation` were re-read in full before drafting §7: neither
currently calls `trigger_librarian` or any other background hook — relation-graph-changing writes
fire *no* existing maintenance trigger today (`trigger_librarian` is wired only into
`store_memory` and `log_event`, confirmed by inspecting every call site of
`_librarian_trigger_pool`/`trigger_librarian`). This is a genuine, previously-unflagged gap in the
codebase that wayfinder ticket `2afcb0f2` (clustering trigger, standing constraint 18) never
explicitly named — it locked *how* the new trigger reuses the Librarian's pool/cooldown shape, not
*which write call sites* fire it. §7 below closes that gap: it is the one design decision this
spec makes that was left open upstream (see §1, "Locked design decisions").

## 1. Why

This is Milestone C ("community/graph-index infrastructure") of the
`wayfinder:saltmdb:graph-aware-context-retrieval` roadmap, greenlit 2026-09-14 (wayfinder ticket
"Should Milestone C be greenlit?", event `b1c4e8b6`) after zbalint overturned Claude's own initial
"lean against urgency" recommendation on a generalization argument: this corpus's health (few
zero-edge memories) is an artifact of Claude's own strict linking discipline as its primary writer,
not a property that generalizes to SALTMDB's other actual and intended writers (`antigravity`,
`omp`, future smaller/weaker models) — see memory `b83d66a6` for the full argument. Milestone C
then went through the same seven-ticket grilling round Milestone A's G2-G8 received; every ticket
is now resolved (standing constraints 16-23 on the roadmap map, `d582e06d`). This spec implements
constraints 17, 18, 19, 20, 21, and half of 23 (the spec-lock-before-calibration ordering) — the
Leiden clustering engine and its persisted storage. Constraint 16 (orphan-to-community assignment,
Milestone C.5) is a **separate**, later spec — see `SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`
— which depends on this spec's schema and centroid data but is not part of this implementation.

**Locked upstream design decisions this spec implements as-is** (restated only to the depth needed
to implement; full reasoning lives in the cited constraints/tickets, not repeated here):

1. **Library: `leidenalg` + `python-igraph`, not `graspologic`** (constraint via ticket "library &
   Python-version choice," event `c7b5a16b`) — feature completeness (native CPM/RBConfiguration
   quality functions, though this spec uses none of them — see decision 4 below) and
   license-compatibility with SALTMDB's own AGPL-3.0-only license were the deciding factors, not
   packaging friction (both libraries ship prebuilt wheels). No version pins were locked upstream;
   this spec pins them now (§2).
2. **No caller-facing surface** (constraint 17, ticket "integration shape," event `42e106fb`) —
   Milestone C ships Leiden clustering + storage + a purely internal query surface. No new MCP
   tool, no new `retrieve_context` parameter or `metadata.strategy` value. The "internal query
   surface" this constraint anticipates is simply the persisted tables themselves, readable by any
   future in-process caller (Milestone C.5 first) via plain SQL — this spec does not need to build
   a bespoke query function beyond `recompute_communities` itself, since a `SELECT` against
   `community_membership`/`community_embeddings` *is* the internal query surface.
3. **Trigger: reuse the write-triggered + cooldown-throttle pattern verbatim, same pool, new
   `task_name`** (constraint 18, ticket "clustering trigger and freshness/maintenance," event
   `20b4c507`) — same `_librarian_trigger_pool`/`_system_locks` atomic-claim shape as the still-live
   `trigger_librarian`, under `task_name='community_detection'`. **The shared pool is the literal
   same `ThreadPoolExecutor` instance** (`librarian_service._librarian_trigger_pool`), not a second
   independent pool — this is the ticket's own named, explicitly-accepted tradeoff ("a slow
   clustering pass could queue behind the pool's other duties... accepted as a non-issue at today's
   corpus scale"), not a detail this spec is free to change. Always fully recomputes from scratch
   over the whole relation graph on every triggered run — no incremental/delta updates.
4. **Flat (single-level) communities; no hierarchy** (constraint 20, ticket "hierarchy/resolution,"
   event `f27056ff`) — no CPM resolution-parameter sweep, no recursive re-run of Leiden on induced
   subgraphs. This is why the partition type used below (§6.2) is the parameter-free
   `leidenalg.ModularityVertexPartition`, not `CPMVertexPartition`/`RBConfigurationVertexPartition`
   (both of which *require* a resolution parameter this milestone has no locked value for and no
   use for, since there is nothing to sweep).
5. **Representative-member and community-embedding**: within-community PageRank centrality
   combined with cosine-distance medoid for the representative; the PageRank-weighted centroid as
   the community's own embedding index vector (constraint 19, ticket "representative-member and
   community-embedding lock," event `25b80bda`). PageRank is the sole centrality measure (not
   eigenvector, not raw degree). Ties break first by closest cosine distance to the centroid, then
   by deterministic lowest-entity-id ordering. A singleton community trivially self-assigns (no
   centrality computed); 2-3 member communities get no special-cased rule. No representative/
   centroid stability mechanism across recomputes — identity churn is accepted as low-stakes and
   unengineered.
6. **Storage: three relational-shaped tables, one of them a vec0 virtual table** (constraint 21,
   ticket "storage/schema and migration strategy," event `4c20b91f`) — `communities`,
   `community_membership`, and `community_embeddings`. A `level INTEGER NOT NULL DEFAULT 0` column
   is reserved on both relational tables now, pre-provisioning an additive (not migrating) path for
   a possible future Milestone D hierarchy retrofit — this spec never writes anything other than
   `0` into it. No bitemporal columns. A triggered recompute is one wrapped
   `BEGIN IMMEDIATE...COMMIT` transaction (delete-then-reinsert across all three tables), verified
   safe under this codebase's actual `PRAGMA journal_mode=WAL` setting (`connection.py`) for a
   concurrent reader (Milestone C.5's own future live centroid lookups). `community_id` is not
   stable across recomputes — every trigger mints fresh UUIDs (`uuid.uuid4()`, matching every other
   id-generation call site in this codebase — `relation_service.py:237`,
   `memory_service/write.py:242`, etc.).
7. **Spec-first, calibrate-after** (constraint 23, ticket "spec-writing sequencing," event
   `7d401c13`) — this spec ships with one literal named `PLACEHOLDER` constant
   (`COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`, §3); OMP implements against it as a real, concrete,
   working value; the eventual Milestone C/C.5 benchmark run (constraint 22, ticket `787ebf0c`)
   patches it later via a direct `config.py` edit, mirroring Milestone B's own precedent
   (`4c0c77bd`) — no OMP re-handoff for that later edit. **Per forward note `e74d4355`'s own
   concern, resolved here explicitly**: this spec's own acceptance bar (§9) is pure structural
   correctness against the constant's *symbol*, never its numeric value — every test that exercises
   the cooldown imports `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S` and asserts behavior relative to
   it (mirrors real elapsed-vs-not-elapsed timing), never a hardcoded literal. Constraint 22's own
   separate recall-lift acceptance-bar numeric value is not part of this spec's acceptance at all —
   it gates only the future standalone benchmark ticket, which cannot run until this implementation
   already exists (structurally impossible to sequence the other way, as `7d401c13`'s own
   resolution notes).

**Locked design decisions this spec makes that were left open upstream** (mirroring
`SPEC-CONTEXT-RETRIEVAL-A1-EXPANSION-ENGINE.md` §1's own precedent for this category — grounded
against the actual codebase and the actual ticket text, not assumed):

1. **Trigger call sites: `store_relation` and `invalidate_relation`, not `store_memory`/
   `log_event`.** No ticket ever named which write call site fires the new hook — constraint 18
   only locked the pool/cooldown *shape*. Community structure is a function of the relation graph,
   not of raw entity creation (a freshly `store_memory`'d entity has no edges yet — recomputing
   communities the instant it's created can find nothing new to cluster). The two functions that
   actually mutate `relations` rows (`store_relation` — new edge; `invalidate_relation` — edge
   removed) are the correct, minimal, precisely-scoped points to fire this trigger from. This spec
   does **not** wire it into `bulk_store_relations`, `commit_consolidation`, or
   `bulk_commit_consolidation` — those call `store_relation`/the consolidation-internal relation
   writes with `_in_transaction=True` inside their own already-open transaction, and none of them
   currently call `trigger_librarian` either (confirmed: zero occurrences in `relation_service.py`
   today). Leaving this batch-write path unwired is an accepted, explicitly-flagged gap, not a
   silent omission: a community-structure change made only through consolidation stays undetected
   until the next ordinary `store_relation`/`invalidate_relation` call anywhere in the corpus fires
   the trigger — a staleness bound, not a correctness bug, consistent with G1's live-usage-first,
   "getting it wrong here is low-stakes and reversible" posture already applied to comparable
   low-stakes numeric choices elsewhere on this roadmap (e.g. `LINEAGE_HISTORICAL_CAP`). If live
   usage later shows consolidation-only community drift is a real problem, wiring
   `bulk_store_relations`/`commit_consolidation` too is a pure, independent, low-risk addition —
   not attempted here.
2. **Graph construction: every relation predicate counts, collapsed to one undirected, unweighted
   simple graph.** No ticket restricts Milestone C's clustering graph to Milestone A's G3
   allowlist (`elaborates_on`/`depends_on`/`corrects`/`derived_from`/`resolves`) — that allowlist is
   an explicitly Milestone-A-scoped retrieval-inclusion policy (constraint 8: "predicate-based
   *context expansion*"), a structurally different concern from "which nodes belong in the same
   community," and constraint 17 already draws a hard line between Milestone A's retrieval
   mechanics and Milestone C's infrastructure. The destination's own framing —
   "Leiden clustering over **explicit relation edges only**" — draws its contrast against the *old,
   rejected* similarity-threshold-edge approach (`3deae748`'s chaining bug), not against any
   predicate subset; the original Leiden research (`82eae539`) treated "the relation graph" as one
   undifferentiated structure throughout. Concretely: **every** currently-valid `relations` row
   between two non-archived entities — including `contradicts` and the reserved lifecycle
   predicates (`supersedes`/`consolidated_from`/`revises`) — contributes one graph edge. Multiple
   relations between the same two entities (different predicates, either direction) collapse to a
   single edge; direction and predicate identity are both discarded for clustering purposes (Leiden
   partitions an undirected graph; "which predicate" and "which direction" have no defined meaning
   for "do these two memories belong in the same topical cluster"). No edge weighting is
   introduced — mirrors G3's own binary-not-weighted philosophy, applied here by analogy in the
   absence of any locked reason to weight.
3. **Zero-edge entities never enter the clustering graph as vertices at all — not even as
   Leiden-trivial singletons.** This is the load-bearing distinction between Milestone C's own
   "singleton community" (constraint 19: a *connected* node that Leiden's own modularity
   optimization still assigns its own community) and a true zero-edge orphan (Milestone C.5's
   entire reason for existing, per the scope-confirmation ticket `3115a801`: "pure graph-community
   detection cannot address the zero-edge failure case"). If every zero-edge entity were inserted
   into the graph as an isolated vertex, Leiden would trivially assign each one its own singleton
   community — meaning every orphan would already "belong" to a community, and Milestone C.5 would
   have nothing left to do. This spec's node set is therefore defined as exactly the set of
   entities that appear as an endpoint of at least one qualifying edge (§6.1) — never the full
   `entities` table.
4. **Minimum-work gate: skip the Leiden call entirely (but still clear stale tables) when the
   qualifying edge set is empty.** Mirrors `_run_maintenance_pass_impl`'s own `raw_count < 2`
   early-skip shape (check cheap precondition *before* claiming the cooldown, so a corpus that
   hasn't grown enough yet never burns a cooldown window on a no-op). Unlike Librarian's skip (which
   leaves prior state untouched because there is no prior state to invalidate), an empty edge set
   here still means "there are currently zero communities" — a corpus that had communities before
   and has since had every relation invalidated must not keep serving stale rows forever. See §6.1
   step 5.

## 2. `pyproject.toml`

Add to the existing `dependencies` list (after `"uuid6>=2024.7.10,<2026.0.0"`), matching this
project's existing `>=X,<Y` pinning convention exactly:

```toml
    "uuid6>=2024.7.10,<2026.0.0",
    "leidenalg>=0.10.0,<0.11.0",
    "python-igraph>=0.11.0,<0.12.0"
```

No change to `requires-python` — the environment fact captured in memory `a1e2ec2c` (actual ceiling
≤3.12.5 today, `pyproject.toml`'s own `>=3.10` floor is unbounded and misleading on this point) is
not this spec's concern to fix; both libraries ship prebuilt wheels well within that ceiling
(verified during the library-choice ticket, `c7b5a16b`).

## 3. `src/saltmdb/config.py`

Insert immediately after the existing `LIBRARIAN_TRIGGER_COOLDOWN_S` block (current lines
237-239), before the blank line and the `# Pairwise cohesion gate` comment:

```python
# Milestone C (wayfinder ticket "clustering trigger and freshness/maintenance", standing
# constraint 18, memory 20b4c507) -- community_detection_service's write-triggered recompute
# cooldown, same shape as LIBRARIAN_TRIGGER_COOLDOWN_S above (same shared _librarian_trigger_pool,
# same _system_locks atomic-claim pattern, new task_name='community_detection'). PLACEHOLDER:
# seeded at LIBRARIAN_TRIGGER_COOLDOWN_S's own order of magnitude per constraint 22's explicit
# calibration mechanic ("seeded at the existing Librarian task's own cooldown order of magnitude,
# validated by a named probe for transient mis-assignment during the cooldown window") -- not yet
# benchmarked against SALTMDB's own corpus. Do not remove the placeholder framing when tuning
# this; replace this comment with the benchmark citation once a real value is locked from the
# Milestone C/C.5 benchmark run (wayfinder ticket 787ebf0c), matching the treatment already given
# to CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS before Milestone B calibrated it.
COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S = 300
```

No other constant is added by this spec. (The orphan-community similarity threshold, constraint
16's own separate `PLACEHOLDER`, belongs to the Milestone C.5 spec — do not add it here
speculatively.)

## 4. `src/saltmdb/db/schema.py`

Insert immediately after the existing `_system_locks` block's `INSERT OR IGNORE` statement (current
lines 829-832, the `'librarian_consolidation'` seed row), before the `# 6b. Viewer Sessions Table`
comment at line 834:

```python
        conn.execute(
            "INSERT OR IGNORE INTO _system_locks (task_name, locked_at, locked_by_pid, last_run_at) "
            "VALUES ('community_detection', NULL, NULL, NULL);"
        )

        # Milestone C (wayfinder standing constraint 21, memory 4c20b91f) -- Leiden community
        # detection over the explicit relation graph. Three tables, one of them (community_embeddings,
        # in vector_schema.py) a vec0 virtual table mirroring the entity_embeddings/
        # entity_chunk_embeddings/retrieval_embeddings family. `level INTEGER NOT NULL DEFAULT 0` is
        # reserved on both relational tables now so a future Milestone D hierarchy retrofit is
        # additive, not a migration -- this milestone (flat communities only, constraint 20) never
        # writes anything but 0 into it. No bitemporal columns: constraint 18's full-recompute-every-
        # trigger policy makes row PRESENCE itself mean "member as of last recompute" -- no locked
        # query path ever needs point-in-time community history. A triggered recompute DELETEs and
        # re-INSERTs the full contents of all three tables inside one wrapped transaction (see
        # community_detection_service.recompute_communities) rather than a shadow-table swap --
        # verified safe under this database's actual PRAGMA journal_mode=WAL (connection.py) for a
        # concurrent reader mid-recompute. community_id is NOT stable across recomputes -- every
        # trigger mints fresh UUIDs (mirrors constraint 19's own accepted representative/centroid
        # identity-churn precedent).
        conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id TEXT PRIMARY KEY,
            representative_entity_id TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (representative_entity_id) REFERENCES entities(id) ON DELETE CASCADE
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS community_membership (
            entity_id TEXT PRIMARY KEY,
            community_id TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
        );
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_community_membership_community_id "
            "ON community_membership(community_id)"
        )
```

`community_membership.entity_id` as the primary key (not a composite with `level`) is deliberate,
not an oversight: flat communities (constraint 20) mean at most one row per entity today. A future
Milestone D hierarchy retrofit that needs multiple rows per entity (one per level) would need to
change this table's primary key at that time — out of scope here, since constraint 20 explicitly
forecloses building hierarchy support ahead of Milestone D's own greenlight.

## 5. `src/saltmdb/db/vector_schema.py`

Add a new function, sibling to `init_vector_schema`/`init_retrieval_vector_schema`, immediately
after `init_retrieval_vector_schema` (current lines 57-77):

```python
def init_community_vector_schema(conn: sqlite3.Connection) -> None:
    """Create the community_embeddings vec0 virtual table (Milestone C, wayfinder standing
    constraint 21). One row per community, keyed by community_id -- mirrors entity_embeddings'
    exact shape (a single PRIMARY KEY id column plus one FLOAT[384] embedding column, no
    auxiliary columns), since a community's embedding, like an entity's, is a single deterministic
    index vector (the PageRank-weighted centroid, see community_detection_service.py), not
    multiple per-community vectors the way entity_chunk_embeddings holds multiple per-entity rows.

    Loads the sqlite_vec extension onto this connection itself, matching every other vec0 call
    site in this codebase (init_vector_schema, init_entity_chunk_vector_schema,
    init_retrieval_vector_schema) that self-loads defensively rather than assume a prior call
    already attached the extension to this specific connection object. Loading twice on the same
    connection is a harmless no-op.
    """
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS community_embeddings USING vec0(
            community_id TEXT PRIMARY KEY,
            embedding FLOAT[384]
        );
    """)
```

In `src/saltmdb/db/schema.py`'s `init_db`, the existing import (current lines 764-769) already
names four symbols, not three — `init_vector_schema`, `init_entity_chunk_vector_schema`,
`init_retrieval_vector_schema`, and `migrate_entity_chunk_embeddings_schema` (the last one called
separately, outside the try/except below, per its own "must NOT be swallowed" comment at lines
771-781 — leave that call and its surrounding comment completely untouched). Add
`init_community_vector_schema` as a fifth name to that same import:

```python
        from saltmdb.db.vector_schema import (
            init_vector_schema,
            init_entity_chunk_vector_schema,
            init_retrieval_vector_schema,
            init_community_vector_schema,
            migrate_entity_chunk_embeddings_schema,
        )
```

Then add the new call **inside** the existing `try/except Exception` block (current lines 784-789)
that already wraps the other three `init_*_vector_schema` calls in this codebase's established
"vector features are best-effort, degrade gracefully" posture — placing it outside that block
would be a real behavioral regression (an exception from `init_community_vector_schema` would then
abort `init_db()` entirely instead of degrading, contradicting the exact pattern this addition is
supposed to mirror):

```python
        try:
            init_vector_schema(conn)
            init_entity_chunk_vector_schema(conn)
            init_retrieval_vector_schema(conn)
            init_community_vector_schema(conn)
        except Exception as e:
            logger.warning("Vector schema init deferred/failed: %s", e)
```

A `community_embeddings` row is deleted whenever its owning community is deleted at recompute time
(handled explicitly by `recompute_communities`'s own delete step, §6.3 — vec0 virtual tables do not
support `ON DELETE CASCADE` foreign keys, so this is application-level, not a schema constraint).
This spec does **not** add `community_embeddings` to `schema.py`'s existing archival-cleanup sweep
(the `for table in ("entity_embeddings", "entity_chunk_embeddings", "retrieval_embeddings")` loop
at lines 114-122) — that loop cleans up *entity*-keyed vector rows left behind by an archived
entity; `community_embeddings` is keyed by `community_id`, an entirely different id space with no
such orphaning failure mode (a community row and its embedding row are always deleted together, in
the same transaction, by `recompute_communities` itself).

## 6. New file: `src/saltmdb/domain/services/community_detection_service.py`

A new domain-service module, sibling to `context_expansion_service.py`/`cohesion_service.py`,
following this codebase's existing conventions: module-level `logger = logging.getLogger(__name__)`,
the same `db_connection=None, db_path: str | None = None` open-or-reuse-connection pattern used
throughout the domain-service layer.

### 6.1 `recompute_communities` — the full Leiden pass

```python
def recompute_communities(
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

**Callable directly, bypassing the cooldown throttle entirely** — mirrors `run_librarian_now`'s
`force=True` path: the throttle lives in `trigger_community_detection`'s pool-worker wrapper
(§6.4), not in this function. A test, a future manual CLI/RPC entry point, or `run_librarian_now`
itself could call this directly with no cooldown concern.

**Algorithm** (implement exactly this sequence):

1. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it) — mirror A1/A2/A3/A4's `should_close`
   pattern exactly.
2. Compute `now = datetime.now(UTC).isoformat()` — the single point-in-time every bitemporal check
   below evaluates against (no caller-supplied `point_in_time`, per §1 upstream-decision
   framing — constraint 21 explicitly rejects point-in-time community history).
3. Fetch the qualifying edge set in one query — every relation between two currently-active,
   non-archived entities, using the exact same four-part bitemporal predicate
   `_dependency_cte_sql` already uses (verified against `relation_service.py:567-570`), with no
   predicate filter at all (§1 upstream-decision 2 — every predicate counts):
   ```python
   rows = conn.execute(
       """
       SELECT r.source_id, r.target_id
       FROM relations r
       JOIN entities e1 ON r.source_id = e1.id
       JOIN entities e2 ON r.target_id = e2.id
       WHERE e1.status != 'archived' AND e2.status != 'archived'
         AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
         AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
         AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
         AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
       """,
       (now, now, now, now),
   ).fetchall()
   ```
4. Collapse to a deduplicated undirected edge set and derive the node set from it (§1
   upstream-decision 3 — zero-edge entities are never nodes here):
   ```python
   edge_set: set[frozenset[str]] = set()
   for source_id, target_id in rows:
       if source_id != target_id:  # defensive; store_relation already forbids self-relations
           edge_set.add(frozenset({source_id, target_id}))
   node_ids: list[str] = sorted({n for pair in edge_set for n in pair})
   ```
5. **Empty-edge-set short-circuit** (§1 upstream-decision 4): if `edge_set` is empty, still clear
   any stale prior state (a corpus that previously had communities but has since had every
   qualifying relation invalidated must not keep serving them), then return early:
   ```python
   if not edge_set:
       def _clear(c):
           c.execute("DELETE FROM community_membership")
           c.execute("DELETE FROM communities")
           c.execute("DELETE FROM community_embeddings")
       write_transaction_retrying(conn, _clear)
       return {"status": "no_edges", "communities_created": 0}
   ```
6. Build the igraph graph. igraph indexes vertices by contiguous integer position, so build an
   explicit `entity_id -> index` map from the sorted `node_ids` list (sorting makes vertex index
   assignment deterministic across runs given the same edge set — required for the deterministic
   lowest-entity-id tie-break in step 9 to be reproducible, and for tests to assert on it):
   ```python
   import igraph as ig

   index_of = {entity_id: i for i, entity_id in enumerate(node_ids)}
   edges = [tuple(sorted(index_of[n] for n in pair)) for pair in edge_set]
   graph = ig.Graph(n=len(node_ids), edges=edges, directed=False)
   ```
7. Run Leiden with the parameter-free modularity quality function (§1 upstream-decision, restated
   from constraint 20 — no resolution parameter exists or is needed for flat communities):
   ```python
   import leidenalg

   partition = leidenalg.find_partition(graph, leidenalg.ModularityVertexPartition)
   ```
   `partition` is a list-like grouping of vertex indices; each group is one community.
8. Batch-fetch every node's `entity_embeddings` vector in one query (mirrors A2/A4's own batched
   entity-materialization pattern) — an entity with no embedding row yet (e.g. embedding still
   `pending`) is simply absent from this dict, handled per step 9's degrade-gracefully rule:
   ```python
   import numpy as np

   placeholders = ",".join("?" for _ in node_ids)
   embedding_rows = conn.execute(
       f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
       node_ids,
   ).fetchall()
   raw_vector = {
       entity_id: np.frombuffer(blob, dtype=np.float32) for entity_id, blob in embedding_rows
   }
   ```
9. For each community (each group of vertex indices from `partition`), compute its representative,
   centroid, and member count. Implement this exactly:
   ```python
   def _normalize(vec: np.ndarray) -> np.ndarray:
       norm = np.linalg.norm(vec)
       return vec / norm if norm > 0 else vec

   communities_to_insert = []
   membership_to_insert = []
   embeddings_to_insert = []

   for group in partition:
       member_ids = sorted(node_ids[i] for i in group)  # deterministic order
       if len(member_ids) == 1:
           # Singleton: trivially self-assigns, no centrality computed (constraint 19).
           representative_id = member_ids[0]
           centroid_source = {representative_id: raw_vector[representative_id]} if (
               representative_id in raw_vector
           ) else {}
           pagerank_by_id = {representative_id: 1.0}
       else:
           subgraph = graph.subgraph(group)
           pagerank_scores = subgraph.pagerank(directed=False)
           # subgraph.vs preserves the original vertex order of `group` as passed to .subgraph()
           pagerank_by_id = {
               node_ids[group[i]]: pagerank_scores[i] for i in range(len(group))
           }
           max_score = max(pagerank_by_id.values())
           top_ids = sorted(
               eid for eid, score in pagerank_by_id.items() if score == max_score
           )
           # Tie-break by cosine distance to centroid happens AFTER the centroid is computed
           # below (the centroid needs pagerank_by_id first) -- see the two-pass note beneath
           # this block.
           representative_id = None  # resolved below
           centroid_source = {
               eid: raw_vector[eid] for eid in member_ids if eid in raw_vector
           }

       member_count = len(member_ids)

       if not centroid_source:
           centroid_vec = None  # no member has a usable embedding yet -- see step 10
       elif len(centroid_source) == 1 and len(member_ids) == 1:
           centroid_vec = _normalize(next(iter(centroid_source.values())))
       else:
           total_weight = sum(pagerank_by_id[eid] for eid in centroid_source)
           weighted_sum = np.zeros(384, dtype=np.float64)
           for eid, vec in centroid_source.items():
               weight = pagerank_by_id[eid] / total_weight if total_weight > 0 else (
                   1.0 / len(centroid_source)
               )
               weighted_sum += weight * _normalize(vec.astype(np.float64))
           centroid_vec = _normalize(weighted_sum)

       if len(member_ids) > 1:
           if len(top_ids) == 1:
               representative_id = top_ids[0]
           elif centroid_vec is not None:
               def _cosine_sim(eid: str) -> float:
                   if eid not in raw_vector:
                       return -1.0  # no embedding -- never wins a medoid tie-break
                   return float(np.dot(_normalize(raw_vector[eid].astype(np.float64)), centroid_vec))

               best_sim = max(_cosine_sim(eid) for eid in top_ids)
               closest = sorted(
                   eid for eid in top_ids if _cosine_sim(eid) == best_sim
               )
               representative_id = closest[0]  # lowest-entity-id final tie-break
           else:
               representative_id = sorted(top_ids)[0]  # no centroid at all -- id tie-break only
   ```
   **Note on the two-pass structure above**: PageRank ties are resolved by cosine distance to the
   *already-computed* centroid, then by lowest id — exactly constraint 19's locked order
   ("ties break first by closest cosine distance to the centroid, then by deterministic
   lowest-entity-id ordering"). This requires computing `centroid_vec` before resolving
   `representative_id` for any non-singleton community with more than one top-PageRank member,
   which the code above does by deferring `representative_id`'s assignment to after `centroid_vec`
   is known.
10. **Missing-embedding degrade rule** (defensive, mirrors `get_fresh_entity_centroids`'s own
    graceful-degradation posture — never hard-fail the whole recompute for one unembedded row): a
    community whose `centroid_vec` came out `None` (no member has a usable embedding yet, e.g. a
    just-created entity whose async embedding job hasn't completed) still gets its `communities`
    and `community_membership` rows written normally; it simply gets **no**
    `community_embeddings` row at all for this recompute cycle (not a zero vector, not a
    placeholder — genuinely absent, so a future consumer's `SELECT` against
    `community_embeddings` naturally returns nothing for it rather than a misleading fake value).
    `representative_id` in this case is resolved by PageRank + lowest-id only (no cosine tie-break
    possible), per the `else: representative_id = sorted(top_ids)[0]` branch above.
11. Collect the three tables' new rows:
    ```python
       community_id = str(uuid.uuid4())
       communities_to_insert.append((community_id, representative_id, member_count, 0, now))
       for entity_id in member_ids:
           membership_to_insert.append((entity_id, community_id, 0))
       if centroid_vec is not None:
           embeddings_to_insert.append((community_id, sqlite_vec.serialize_float32(centroid_vec.astype(np.float32).tolist())))
    ```
12. Write everything inside one wrapped transaction (constraint 21's explicit delete-then-reinsert
    choice, not a shadow-table swap):
    ```python
    def _write(c):
        c.execute("DELETE FROM community_membership")
        c.execute("DELETE FROM communities")
        c.execute("DELETE FROM community_embeddings")
        c.executemany(
            "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            communities_to_insert,
        )
        c.executemany(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, ?)",
            membership_to_insert,
        )
        c.executemany(
            "INSERT INTO community_embeddings (community_id, embedding) VALUES (?, ?)",
            embeddings_to_insert,
        )

    write_transaction_retrying(conn, _write)
    return {
        "status": "recomputed",
        "communities_created": len(communities_to_insert),
        "nodes_clustered": len(node_ids),
        "communities_missing_embedding": len(communities_to_insert) - len(embeddings_to_insert),
    }
    ```

This function raises nothing itself for a resolvable/valid database state; a genuine `igraph`/
`leidenalg` failure (malformed graph construction, library-internal error) propagates uncaught,
matching this codebase's Coding Standards rule 15 (never swallow a real exception silently) and
A1/A4's own precedent of only catching the specific, named, expected failure modes.

### 6.2 `trigger_community_detection` — fire-and-forget cooldown-gated submit

```python
_COMMUNITY_DETECTION_TASK_NAME = "community_detection"


def trigger_community_detection(db_path: str | None = None) -> None:
    """Fire-and-forget: schedules the cooldown check + recompute on the SAME single-worker
    _librarian_trigger_pool trigger_librarian uses (constraint 18's own named, accepted shared-pool
    tradeoff) -- never blocks the caller."""
    if os.environ.get("SALTMDB_DISABLE_COMMUNITY_DETECTION") or os.environ.get("SALTMDB_TEST_MODE"):
        return
    db_path = db_path or get_db_path()
    from saltmdb.domain.services.librarian_service import _librarian_trigger_pool

    _librarian_trigger_pool.submit(_run_community_detection_pass_impl, db_path)
```

`SALTMDB_DISABLE_COMMUNITY_DETECTION` is a new, dedicated env var mirroring
`SALTMDB_DISABLE_LIBRARIAN`'s own precedent — deliberately not overloading
`SALTMDB_DISABLE_LIBRARIAN` itself, since Milestone C is a structurally separate subsystem that
merely shares Librarian's execution pool, not a Librarian duty. `SALTMDB_TEST_MODE` is checked too
(the existing shared "no background async work during tests" convention `trigger_librarian` already
uses) so this new trigger never fires unexpectedly during the existing test suite.

### 6.3 `_run_community_detection_pass_impl` — the pool-worker body (cooldown claim + pass)

```python
def _run_community_detection_pass_impl(db_path: str) -> str:
    """Mirrors librarian_service._run_maintenance_pass_impl's exact shape: cheap precondition
    check (skip without claiming the cooldown), atomic cooldown-claim UPDATE, then the real pass --
    but the precondition here is 'no qualifying edges exist yet' (this pass's own §6.1 step 3/4
    query), not raw-entity count, since community structure depends on edges, not entity count."""
    try:
        conn = get_connection(db_path)
    except Exception as e:
        logger.debug("Could not open connection for community detection pass: %s", e)
        return f"Skipped: could not open database connection ({e})."
    try:
        now = datetime.now(UTC).isoformat()
        edge_count = conn.execute(
            """
            SELECT COUNT(*) FROM relations r
            JOIN entities e1 ON r.source_id = e1.id
            JOIN entities e2 ON r.target_id = e2.id
            WHERE e1.status != 'archived' AND e2.status != 'archived'
              AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
              AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
              AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
              AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
            """,
            (now, now, now, now),
        ).fetchone()[0]
        if edge_count == 0:
            return "Skipped: no qualifying relation edges to cluster."

        def _claim_cooldown(c):
            claim_now = datetime.now(UTC).isoformat()
            cur = c.execute(
                f"""
                UPDATE _system_locks
                SET last_run_at = ?
                WHERE task_name = '{_COMMUNITY_DETECTION_TASK_NAME}'
                  AND (last_run_at IS NULL OR datetime(last_run_at) < datetime('now', '-{COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S} seconds'))
                """,
                (claim_now,),
            )
            return cur.rowcount == 1

        if not write_transaction_retrying(conn, _claim_cooldown):
            return "Skipped: cooldown not elapsed."

        result = recompute_communities(db_connection=conn)
        return f"Community detection pass complete: {result}"
    except Exception as e:
        logger.warning("Community detection pass failed: %s", e)
        return f"Failed: {e}"
    finally:
        close_connection(conn)
```

This mirrors `_run_maintenance_pass_impl`'s exact structure (open connection defensively, cheap
precondition check before claiming, atomic single-UPDATE cooldown claim inside
`write_transaction_retrying`, run the real pass, close connection in `finally`) — the one deliberate
difference (§1 upstream-decision 4) is that the precondition is "any qualifying edges exist" rather
than "raw entity count ≥ 2", since the two subsystems have genuinely different trigger conditions.

## 7. `src/saltmdb/domain/services/relation_service.py`

Two small additions (§1 upstream-decision 1), each mirroring exactly where `store_memory`/
`log_event` already call `trigger_librarian` — right before the function's own final success
return, gated the same way `log_event` already gates its own `trigger_librarian` call on
`_in_transaction`.

In `store_relation`, immediately before the existing `return result_msg` (current line 442, right
after `result_msg = write_transaction_retrying(conn, _write)` at line 441):

```python
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path)
        return result_msg
```

In `invalidate_relation`, immediately before its existing `return result_msg` (current line 534,
the exact same shape):

```python
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path)
        return result_msg
```

The import is local (inside the `if`), not module-level, mirroring `store_memory`'s own
`from saltmdb.domain.services.librarian_service import trigger_librarian` local-import
convention — avoids a module-level circular-import risk between `relation_service.py` and the new
`community_detection_service.py` (which itself imports from `relation_service.py`'s sibling
modules only, not from `relation_service.py` directly, but the local-import convention is kept
consistent regardless).

No other line of `store_relation`/`invalidate_relation` changes. `bulk_store_relations`,
`commit_consolidation`, `consolidate_memories`, and `bulk_commit_consolidation` are explicitly
**not** touched (§1 upstream-decision 1's accepted, flagged gap).

## 8. `tests/test_community_detection_service.py` (new file)

Follow `tests/test_relation_service.py`'s exact fixture conventions: `init_db` + real SQLite (no
mocks for the database or for `igraph`/`leidenalg` — this module's whole value is the real
graph-partitioning algorithm), the `_memory_id`/`_axis_vector` helper pair (copy them, do not
import across test files, per established precedent), `store_relation`/`store_memory` to build real
fixture graphs, and direct `INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)`
(via `sqlite_vec.serialize_float32`) to give fixture entities deterministic, controllable vectors —
mirroring `tests/test_archived_embedding_status.py`/`tests/test_cross_owner_dedup.py`'s own direct
raw-insert convention, bypassing the async embedding pipeline entirely so tests are synchronous and
deterministic. Set `SALTMDB_TEST_MODE=1` in every test's environment (or patch
`trigger_community_detection` to a no-op) so `store_relation`/`invalidate_relation` calls in this
file's own setup never race a real background pool submission against the test's own direct
`recompute_communities` call.

Required test scenarios for `recompute_communities` (write test-first, red before green, per this
workspace's `tdd` skill and Coding Standards rule 19 — this list is the acceptance bar's content,
not exhaustive implementation guidance OMP is free to skip):

1. **Empty graph**: zero relations anywhere → `recompute_communities` returns
   `{"status": "no_edges", "communities_created": 0}`, and `communities`/`community_membership`/
   `community_embeddings` are all empty afterward.
2. **Two connected entities form one community**: A `depends_on` B (only edge in the corpus) →
   exactly one `communities` row (`member_count == 2`), one `community_membership` row per entity
   pointing at it, both entities' embeddings contribute to the centroid.
3. **Zero-edge entity never gets a community row**: a third entity C with no relations at all
   coexists with scenario 2's A/B pair → C has no `community_membership` row and is not counted in
   `nodes_clustered`.
4. **Every predicate counts, including `contradicts` and reserved lifecycle predicates**
   (§1 upstream-decision 2): construct three separate corpora (or three independent
   fixture pairs within one test), one connected via `related_to`, one via `contradicts`, one via
   a `supersedes` edge (construct the same way `test_relation_service.py`'s lineage tests do) —
   assert all three pairs each form their own community, proving no predicate is silently excluded
   from the clustering graph the way G3's allowlist excludes them from context expansion.
5. **Multi-predicate/multi-edge pair collapses to one graph edge, not a weighted multi-edge**: A and
   B connected by both `depends_on` AND `related_to` — still exactly one community of size 2 (not
   a crash, not a double-counted edge affecting PageRank/modularity in an observably different way
   than a single edge would).
6. **Disconnected components form separate communities**: A-B connected, C-D connected, no edge
   between the two pairs → two distinct `communities` rows, each with `member_count == 2`, disjoint
   membership.
7. **Larger connected community with distinguishable PageRank**: construct a small hub-and-spoke
   graph (one central entity connected to 3+ others, those others not connected to each other) big
   enough that the hub's PageRank score is unambiguously higher than any spoke's — assert
   `representative_entity_id` is the hub, not a spoke (proves PageRank centrality, not naive
   first/lowest-id selection, actually drives representative choice when scores are NOT tied).
8. **PageRank tie broken by cosine distance to centroid**: construct a community where two members
   have identical (or symmetric, guaranteed-tied) PageRank scores but distinguishable embeddings
   (e.g. via `_axis_vector`) — assert the member whose embedding is closer to the computed
   `community_embeddings` centroid wins the representative slot, not simply the lower id.
9. **Final tie-break is deterministic lowest-entity-id**: construct a community where PageRank AND
   cosine-distance-to-centroid are both genuinely tied (e.g. identical embeddings via the same
   `_axis_vector` call for both members) — assert the lexicographically lower entity id wins, and
   assert this is stable across repeated `recompute_communities` calls against the same fixture.
10. **Singleton community**: assert `recompute_communities`' own singleton code path (§6.1 step 9's
    `len(member_ids) == 1` branch) produces `member_count == 1`, `representative_entity_id` equal
    to that one member, and a `community_embeddings` row equal to that member's own (normalized)
    embedding directly, not a PageRank computation artifact. **Verified empirically before locking
    this spec** (three independent constructions probed directly against the real installed
    `leidenalg`/`igraph` libraries — a leaf-off-a-triangle, a leaf off a dense 6-node complete
    graph, and a bridge node connecting three separate triangles): `leidenalg.ModularityVertexPartition`
    reliably prefers grouping even a weakly-attached, single-edge node with its one neighbor's
    community over leaving it fully isolated — a genuine algorithm-produced singleton (for a node
    with degree ≥ 1, as opposed to a fully disconnected component, which is a different case
    already covered by scenario 6) did not occur in any of the three probed constructions and
    should not be assumed constructible on demand. **The only reliable way to exercise this
    scenario is a fully disconnected single-node "community"** — i.e., construct the graph so this
    one node's only edge partner is itself excluded from consideration is impossible by definition
    (a node needs at least one edge to be a graph vertex at all, per §1 upstream-decision 3) — so in
    practice, exercise this scenario as: a genuinely disconnected node pair (mirrors scenario 6's
    "disconnected components" shape) where you additionally assert the SIZE-1 branch specifically
    by picking a fixture where partition naturally yields a size-1 group (retry the specific
    triad/bridge/dense-hub shapes above, or any other construction, and print/log the actual
    partition to confirm before asserting on it — do not assume any single hand-picked graph
    reliably reproduces a singleton without first confirming it against the real library, exactly
    as this spec's own pre-lock check did). If no construction reliably produces a genuine
    singleton within reasonable effort, the acceptable substitute is directly unit-testing the
    singleton code branch in isolation (call the internal per-community logic with a hand-built
    `partition`-shaped single-element group, bypassing `leidenalg.find_partition` entirely for this
    one assertion) — note in a comment that this substitution was made and why, per this
    scenario's own empirical finding above.
11. **Missing embedding for one member degrades gracefully, not fatally**: a community of 2 where
    one member has no `entity_embeddings` row at all — assert the recompute still completes, both
    `community_membership` rows exist, `community_embeddings` reflects only the member with a real
    vector (not a zero-vector, not a crash), and that member (with real embedding) is a valid
    medoid tie-break candidate while the embedding-less one never wins a cosine-distance
    comparison.
12. **All members missing embeddings**: a community where every member has no `entity_embeddings`
    row — assert `communities`/`community_membership` rows still exist (structural clustering
    result) but no `community_embeddings` row is created for that community id, and
    `communities_missing_embedding` in the return dict reflects this.
13. **Full recompute clears prior state, not just adds to it**: run `recompute_communities` once
    against a fixture, then invalidate the only relation (via `invalidate_relation`) and run it
    again — assert the previously-populated `communities`/`community_membership`/
    `community_embeddings` rows are gone (not stale), per the empty-edge-set short-circuit's own
    explicit clear-then-return behavior (scenario 1's assertion, re-exercised here after a
    non-empty prior state).
14. **`community_id` is not stable across recomputes**: run `recompute_communities` twice against
    the identical unchanged fixture (no relation change between calls) — assert the community
    covering the same members gets a *different* `id` the second time (per constraint 21's
    explicit no-stability guarantee), while `representative_entity_id`/`member_count` and the
    community's actual membership set remain identical.
15. **Archived entity excluded from clustering even with a live edge to a non-archived one**: A
    (archived) — `depends_on` → B (not archived) — assert neither A nor B gets a
    `community_membership` row from this edge (the edge itself is excluded by the
    `status != 'archived'` join predicate on both endpoints), even though B might separately be
    part of another community via a different, non-archived-endpoint edge.
16. **Bitemporally-invalid relation excluded**: a relation row with `valid_to` in the past (already
    closed) or `invalid_at` in the past — assert it contributes no edge to the clustering graph,
    mirroring the same predicate `_dependency_cte_sql` already enforces for traversal.

Required test scenarios for the trigger/cooldown machinery (`trigger_community_detection`,
`_run_community_detection_pass_impl`):

17. **`SALTMDB_TEST_MODE` suppresses the trigger**: with the env var set, `trigger_community_detection`
    returns immediately without submitting to the pool (assert via a patched/mocked
    `_librarian_trigger_pool.submit` never being called, mirroring how `test_relation_service.py`
    or similar already verifies `trigger_librarian`'s own equivalent suppression, if such a test
    exists — otherwise construct this directly against the real env var).
18. **`SALTMDB_DISABLE_COMMUNITY_DETECTION` suppresses the trigger independently of
    `SALTMDB_DISABLE_LIBRARIAN`**: set only the new var (not the Librarian one) — assert the
    submission is still suppressed, proving the two are genuinely independent toggles.
19. **Cooldown claim collapses concurrent callers to one winner**: call
    `_run_community_detection_pass_impl` twice in immediate succession against the same fixture
    (real qualifying edges present, real `_system_locks` row) — assert the second call's own
    `_claim_cooldown` UPDATE affects zero rows (`cur.rowcount == 0`), returning "Skipped: cooldown
    not elapsed." (reference `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`'s symbol in the assertion
    framing — e.g. by asserting the claim succeeds again only after manually rewinding
    `_system_locks.last_run_at` past that many seconds — never hardcode `300` in the test, per
    forward-note `e74d4355`'s own carve-out).
20. **Cooldown claim succeeds again after the window elapses**: manually set
    `_system_locks.last_run_at` to a timestamp older than `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`
    seconds ago (imported, not hardcoded) — assert the next `_run_community_detection_pass_impl`
    call successfully claims and runs.
21. **No qualifying edges skips before claiming the cooldown**: an empty-relations fixture — assert
    `_run_community_detection_pass_impl` returns "Skipped: no qualifying relation edges to
    cluster." AND that `_system_locks.last_run_at` for `'community_detection'` remains unchanged
    (the cooldown slot was never claimed, exactly mirroring Librarian's own
    check-before-claim ordering — a corpus that later grows edges must still be able to fire on
    its very first qualifying trigger, not find the slot already burned by an earlier no-op).

Cross-check before finalizing the test file: every locked decision in §1 (both upstream and
locked-here) must correspond to at least one scenario above — upstream decisions 3-6 underlie
scenarios 2, 4, 7-10, 13-14; decisions 1-4 of the locked-here section correspond to scenarios 17-18
(trigger call sites — exercised at the `relation_service.py` integration level, see below),
2 (scenario 4), 3 (scenario 3), and 4/§6.1-step-5 (scenarios 1, 13, 21) respectively.

Add, in `tests/test_relation_service.py` (existing file — this is the one small addition to an
existing test file this spec makes, since the trigger call sites live in `relation_service.py`
itself, not in the new module):

22. **`store_relation` fires the community-detection trigger on a real new edge**: patch
    `saltmdb.domain.services.community_detection_service.trigger_community_detection` and assert it
    is called exactly once after a successful `store_relation` call (not on the
    "already exists (no-op)" duplicate-edge path — or, if the implementation calls it
    unconditionally on both paths mirroring `store_memory`'s own unconditional-after-success
    convention, assert that instead; whichever the implementation actually does, the test must
    pin it down explicitly rather than leaving it unasserted).
23. **`invalidate_relation` fires the community-detection trigger on a real invalidation**: same
    shape as scenario 22, for `invalidate_relation`'s own success path.
24. **`_in_transaction=True` suppresses the trigger call** for both `store_relation` and
    `invalidate_relation`: call each with `_in_transaction=True` inside a real open transaction on
    the connection — assert the patched `trigger_community_detection` is never called, mirroring
    `log_event`'s own existing `_in_transaction` gate precedent.

## 9. Out of scope

- Milestone C.5's orphan-to-community assignment (the query-time cosine-comparison against
  `community_embeddings`, its own `PLACEHOLDER` similarity threshold, and its `retrieve_context`
  integration) — a separate, later spec,
  `SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`, which depends on this spec's schema but is not
  implemented here.
- Any `retrieve_context` parameter, MCP tool, or `metadata.strategy` value exposing communities to
  callers — foreclosed by constraint 17 for the whole of Milestone C.
- Hierarchy: CPM/RBConfiguration resolution-parameter sweeps, recursive re-run of Leiden on induced
  subgraphs, or any second `level` value beyond the reserved-but-unused `0` — foreclosed by
  constraint 20 until Milestone D is separately greenlit.
- Incremental/delta clustering updates, or any lazy/on-demand computation at query time — foreclosed
  by constraint 18; every triggered run is a full recompute from scratch.
- Wiring the new trigger into `bulk_store_relations`, `commit_consolidation`, or
  `bulk_commit_consolidation` — an accepted, explicitly-flagged gap (§1 upstream-decision 1), not
  attempted here.
- Any per-member centrality/medoid-distance persistence — constraint 21 explicitly forecloses this
  (throwaway compute-time values only, used solely to pick `representative_entity_id`).
- Recalibrating `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`'s numeric value — deferred to the
  Milestone C/C.5 benchmark run (constraint 22), not this implementation's concern.
- Any representative/centroid stability or hysteresis mechanism across recomputes — constraint 19
  explicitly accepts identity churn as unengineered.
- Any new owner/scope-based ACL filtering on which entities/edges participate in clustering —
  communities are computed over the single global relation graph, matching constraint 18's own
  "the whole relation graph" framing; no per-owner or per-scope partitioning is introduced.
- A manual/CLI-exposed "run community detection now" entry point analogous to
  `run_librarian_now`/`--librarian` — `recompute_communities` is directly callable by any future
  in-process caller (including a future such entry point), but building that entry point itself is
  not this spec's job.

## 10. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_community_detection_service.py -v
```
must exit 0, and every scenario in §8's first list (1-21) must correspond to at least one passing
test (a reviewer checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/test_relation_service.py -v -k "community_detection"
```
must exit 0, covering §8's scenarios 22-24.

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite).

```bash
uv run ruff check src/saltmdb/domain/services/community_detection_service.py \
  src/saltmdb/db/schema.py src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py \
  tests/test_community_detection_service.py tests/test_relation_service.py && \
uv run ruff format --check src/saltmdb/domain/services/community_detection_service.py \
  src/saltmdb/db/schema.py src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py \
  tests/test_community_detection_service.py tests/test_relation_service.py && \
uv run mypy src/saltmdb/domain/services/community_detection_service.py src/saltmdb/db/schema.py \
  src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`).

**Explicit acceptance carve-out (per §1 upstream-decision 7 / forward-note `e74d4355`)**: this
spec's acceptance is pure structural/behavioral correctness against
`COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`'s *symbol* — no test may hardcode its numeric value
(`300`), and no part of this acceptance bar depends on constraint 22's not-yet-determined
recall-lift threshold, which gates only the future, separate benchmark ticket run.

## Amendment 1 — §0 Scope omitted `tests/test_relation_service.py`

**Reported by OMP as `BLOCKED — SPEC ADJUDICATION REQUIRED`**: §0 Scope's affirmative file list
never named `tests/test_relation_service.py`, while §8 (scenarios 22-24) explicitly requires
adding three new test scenarios to that existing file, and §10 Acceptance's second command
(`pytest tests/test_relation_service.py -v -k "community_detection"`) already exercised it too.
OMP correctly declined to guess past the contradiction and performed no tracked-file edits.

**Verified against the actual locked text** (not just OMP's claim): confirmed both the §0 omission
and the §8/§10 requirement are real, exactly as reported — no code was written or run to check
this, the contradiction is visible directly in the spec's own prose.

**Adjudication: widen §0, not shrink §8/§10.** §8 itself already frames this addition as
deliberate and singular ("this is the one small addition to an existing test file this spec
makes, since the trigger call sites live in `relation_service.py` itself, not in the new module"),
and §10's acceptance command already assumed it — §0's own file list is the one section that
never caught up to that intent when the spec was drafted, not a sign the test coverage itself was
wrong or should be dropped. Removing scenarios 22-24 (option 2) would silently drop all direct
test coverage for §7's two trigger-call additions, the very code this amendment's own conflict is
about — rejected.

**Resolution**: §0 Scope amended above to add `tests/test_relation_service.py` as a permitted
edit, scoped to exactly the three scenarios §8 already specifies (items 22-24) — no other change
to that file, no relaxation of any other acceptance criterion. No other section of this spec
changes. OMP may resume implementation immediately against the amended §0.
