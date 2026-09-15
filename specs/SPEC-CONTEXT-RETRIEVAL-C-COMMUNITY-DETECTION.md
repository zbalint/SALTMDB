# SPEC-CONTEXT-RETRIEVAL-C-COMMUNITY-DETECTION

## 0. Status

**LOCKED**

**Scope**: may edit `pyproject.toml` (add exactly two new runtime dependencies, `leidenalg` and
`python-igraph`, see §2); may edit `uv.lock` (the mechanical, tool-regenerated output of running
`uv sync`/`uv lock` after §2's `pyproject.toml` change — commit whatever `uv` itself produces, no
hand-editing; see Amendment 2); may edit `src/saltmdb/config.py` (add exactly one new constant,
`COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`, see §3); may edit `src/saltmdb/db/schema.py` (add two
new relational tables, `communities` and `community_membership`, plus one new `_system_locks` seed
row, see §4); may edit `src/saltmdb/db/vector_schema.py` (add one new function,
`init_community_vector_schema`, creating the `community_embeddings` vec0 virtual table, see §5);
may edit `src/saltmdb/domain/services/relation_service.py` (a `coordinator=None` parameter plus a
fire-and-forget trigger call near the end of `store_relation` and of `invalidate_relation`, see §7
and Amendment 3); may edit `src/saltmdb/daemon/dispatch.py` (exactly two small, mechanical
additions threading `coordinator` through the existing `manage_relation` tool dispatch — no new
tool, no new surface, see §7.1 and Amendment 3); may create
`src/saltmdb/domain/services/community_detection_service.py` (new file, see §6)
and `tests/test_community_detection_service.py` (new file, see §8); may edit
`tests/test_relation_service.py` (test scenarios per §8 items 22-24 plus Amendment 3's own
additions, covering the two trigger-call additions §7 makes to `store_relation`/
`invalidate_relation` — no other change to this file; see Amendment 1); may edit
`tests/test_phase3_mcp_surface.py` (one new test scenario for §7.1's `dispatch.py` change, see
Amendment 3 — no other change to this file). Does not touch:
`src/saltmdb/domain/services/context_expansion_service.py`,
`conflict_set_service.py`, `context_budget_service.py`, `lineage_assembly_service.py`,
`retrieve_context_service.py` (Milestone C exposes nothing to `retrieve_context` at all — standing
constraint 17; Milestone C.5's own separate spec is the one that touches
`retrieve_context_service.py`), `src/saltmdb/mcp/tools.py`, `src/saltmdb/daemon/protocol.py` (no
new MCP tool surface, no wire-protocol change — constraint 17 remains intact; `dispatch.py`'s own
narrow exception is explained in §7.1/Amendment 3), and
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
throughout the domain-service layer. Also add, at module level alongside `logger` (not a local
import inside any function — see Amendment 2, and mirrors `cohesion_service.py:4`'s identical
module-level placement, required so `tests/test_community_detection_service.py` can
`patch("saltmdb.domain.services.community_detection_service.try_load_vector_extension", ...)`
exactly like `test_cohesion_service.py` already does for `cohesion_service`):
```python
from saltmdb.db.vector_schema import try_load_vector_extension
```

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
   pattern exactly. Then load the sqlite-vec extension onto **this specific connection** (extension
   loading is per-connection, not per-process — `init_community_vector_schema` loading it onto the
   connection `init_db` used tells you nothing about whether *this* connection, opened separately by
   this function or handed in by a caller, has it loaded too — see Amendment 2):
   ```python
   vector_extension_loaded = try_load_vector_extension(conn)
   if not vector_extension_loaded:
       logger.warning(
           "recompute_communities: sqlite-vec extension unavailable on this connection -- "
           "community structure (communities/community_membership) will still be computed and "
           "written normally, but community_embeddings is left untouched this cycle (no read, no "
           "clear, no write against it)."
       )
   ```
   `vector_extension_loaded` is read (never re-computed) by steps 5, 8, and 12 below.
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
   qualifying relation invalidated must not keep serving them), then return early. The
   `community_embeddings` clear is itself gated on `vector_extension_loaded` (Amendment 2) — a
   `DELETE FROM community_embeddings` against a vec0 virtual table requires the module registered
   on *this* connection the same as any other query against it, so this is not optional even for a
   bare `DELETE`:
   ```python
   if not edge_set:
       def _clear(c):
           c.execute("DELETE FROM community_membership")
           c.execute("DELETE FROM communities")
           if vector_extension_loaded:
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
   `pending`) is simply absent from this dict, handled per step 9's degrade-gracefully rule. The
   query itself is gated on `vector_extension_loaded` (Amendment 2): `entity_embeddings` is a vec0
   virtual table exactly like `community_embeddings`, so this SELECT needs the extension loaded on
   this connection too, not only the later writes — if it's unavailable, `raw_vector` is simply
   empty, which step 9's existing centroid logic already treats identically to "no member of this
   community has a usable embedding" (no new branch needed there):
   ```python
   import numpy as np

   raw_vector: dict[str, np.ndarray] = {}
   if vector_extension_loaded:
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
    choice, not a shadow-table swap). The `community_embeddings` delete+insert pair is gated on
    `vector_extension_loaded` (Amendment 2 — same reasoning as step 5: any statement referencing a
    vec0 virtual table needs the module registered on this connection, even a `DELETE`). Note this
    gate is almost always a no-op in practice when the extension IS loaded: if it loaded
    successfully, `embeddings_to_insert` may still legitimately be empty (e.g. every community
    happens to have no embedded member yet, step 10's existing per-community case) — that case
    still runs the `DELETE`/empty-`executemany` normally, exactly as before this amendment; only a
    **failed extension load** skips touching the table at all:
    ```python
    def _write(c):
        c.execute("DELETE FROM community_membership")
        c.execute("DELETE FROM communities")
        c.executemany(
            "INSERT INTO communities (id, representative_entity_id, member_count, level, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            communities_to_insert,
        )
        c.executemany(
            "INSERT INTO community_membership (entity_id, community_id, level) VALUES (?, ?, ?)",
            membership_to_insert,
        )
        if vector_extension_loaded:
            c.execute("DELETE FROM community_embeddings")
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
    When `vector_extension_loaded` is `False`, `embeddings_to_insert` is always empty (step 8 left
    `raw_vector` empty, so step 9's centroid logic gives every community `centroid_vec = None`), so
    `communities_missing_embedding` correctly equals `communities_created` in this case too — no
    change needed to this return dict's computation itself.

This function raises nothing itself for a resolvable/valid database state; a genuine `igraph`/
`leidenalg` failure (malformed graph construction, library-internal error) propagates uncaught,
matching this codebase's Coding Standards rule 15 (never swallow a real exception silently) and
A1/A4's own precedent of only catching the specific, named, expected failure modes.

### 6.2 `trigger_community_detection` — fire-and-forget cooldown-gated submit

```python
_COMMUNITY_DETECTION_TASK_NAME = "community_detection"


def trigger_community_detection(db_path: str | None = None, *, coordinator=None) -> None:
    """Fire-and-forget: schedules the cooldown check + recompute on the SAME single-worker
    _librarian_trigger_pool trigger_librarian uses (constraint 18's own named, accepted shared-pool
    tradeoff) -- never blocks the caller. `coordinator` branching mirrors
    `librarian_service.trigger_librarian` exactly (see Amendment 3) -- required for correctness
    under the real daemon's single-writer boundary, not optional polish."""
    if os.environ.get("SALTMDB_DISABLE_COMMUNITY_DETECTION") or os.environ.get("SALTMDB_TEST_MODE"):
        return
    db_path = db_path or get_db_path()
    from saltmdb.domain.services.librarian_service import _librarian_trigger_pool

    if coordinator is not None:
        _librarian_trigger_pool.submit(
            _run_community_detection_with_coordinator, db_path, coordinator
        )
    else:
        _librarian_trigger_pool.submit(_run_community_detection_pass_impl, db_path)


def _run_community_detection_with_coordinator(db_path: str, coordinator) -> str:
    """Mirrors `librarian_service.run_librarian_now`'s own coordinator branch (Amendment 3):
    runs on the trigger-pool's worker thread (the same thread `trigger_community_detection`
    above submitted onto), then hops onto the coordinator's own dedicated writer thread via
    `coordinator.submit`, where `connection.py`'s `_coordinator_connection` ContextVar is
    actually set for the duration of the call -- this hop is why a coordinator-aware branch
    exists at all: ContextVar values set on the coordinator's writer thread never propagate
    into a *different* ThreadPoolExecutor worker thread (this function's own caller's thread),
    so this function's job closure must explicitly receive the connection as an argument
    (`conn`, below) rather than relying on `get_connection()` to find it implicitly. `db_path`
    is accepted for signature symmetry with the no-coordinator branch but unused here --
    `coordinator.submit` supplies its own connection, already opened against the coordinator's
    own `db_path` at daemon startup."""
    return coordinator.submit(
        "community_detection_mutations",
        lambda conn: _run_community_detection_pass_on_connection(conn),
        priority="background",
    )


def _run_community_detection_pass_on_connection(conn) -> str:
    """Mirrors `librarian_service._run_maintenance_pass_on_connection`'s exact shape (Amendment
    3): the coordinator-path pass body, operating directly on the connection the coordinator's
    own job closure hands it -- never calling `get_connection()` itself, and never opening its
    own transaction (db_write_coordinator.py's `_execute_job` already wraps every submitted job
    in `write_transaction_retrying`, with `connection.py`'s `_coordinator_connection` ContextVar
    set for the whole call -- so `recompute_communities`'s own internal
    `write_transaction_retrying(conn, ...)` calls correctly detect they're already inside the
    coordinator's transaction (`connection.py:165-166`) and reuse it rather than opening a
    nested one). Precondition-check and cooldown-claim logic is intentionally duplicated from
    `_run_community_detection_pass_impl` below rather than shared/refactored -- mirrors
    `_run_maintenance_pass_on_connection`/`_run_maintenance_pass_impl`'s own established,
    already-shipped duplication exactly (see Amendment 3 for why this isn't a Coding-Standards-
    rule-4 violation: it matches existing precedent rather than inventing a new pattern)."""
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
    claim_now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        f"""
        UPDATE _system_locks
        SET last_run_at = ?
        WHERE task_name = '{_COMMUNITY_DETECTION_TASK_NAME}'
          AND (last_run_at IS NULL OR datetime(last_run_at) < datetime('now', '-{COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S} seconds'))
        """,
        (claim_now,),
    )
    if cur.rowcount != 1:
        return "Skipped: cooldown not elapsed."
    result = recompute_communities(db_connection=conn)
    return f"Community detection pass complete: {result}"
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

Three additions per function (§1 upstream-decision 1, extended by Amendment 3): a new
`coordinator=None` parameter, a fire-and-forget trigger call passing it through, and (Amendment 3)
the `coordinator` value itself — each mirroring exactly where `store_memory`/`log_event` already
accept `coordinator=None` and forward it into `trigger_librarian` (`memory_service/write.py:592`,
`event_service.py:21`) — right before the function's own final success return, gated the same way
`log_event` already gates its own `trigger_librarian` call on `_in_transaction`.

Add `coordinator=None` to `store_relation`'s signature, immediately after the existing
`db_path: str = None` parameter (current line 141) and before `_in_transaction: bool = False`:
```python
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    _in_transaction: bool = False,
    _allow_core_elaborates_on: bool = False,
) -> str:
```

Then, in `store_relation`, immediately before the existing `return result_msg` (current line 442,
right after `result_msg = write_transaction_retrying(conn, _write)` at line 441):

```python
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path, coordinator=coordinator)
        return result_msg
```

Add `coordinator=None` to `invalidate_relation`'s signature, immediately after the existing
`db_path: str = None` parameter (current line 462) and before `_in_transaction: bool = False`:
```python
    db_connection=None,
    db_path: str = None,
    coordinator=None,
    _in_transaction: bool = False,
) -> str:
```

Then, in `invalidate_relation`, immediately before its existing `return result_msg` (current line
534, the exact same shape):

```python
        if not _in_transaction:
            from saltmdb.domain.services.community_detection_service import (
                trigger_community_detection,
            )

            trigger_community_detection(db_path=db_path, coordinator=coordinator)
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
**not** touched (§1 upstream-decision 1's accepted, flagged gap) — none of them gain a
`coordinator` parameter either; only the two functions the trigger itself is wired into need one.

### 7.1 `src/saltmdb/daemon/dispatch.py` (Amendment 3 — required for the trigger to write
correctly under the real daemon's single-writer boundary; see Amendment 3 below for why)

Two small, mechanical additions, exactly mirroring how `store_memory`/`log_event` already receive
their own `coordinator` argument through this exact file:

In `MUTATING_TOOLS`'s coordinator-injection check inside `_dispatch_tool_inner` (current line 496),
add `"manage_relation"` to the existing two-tool set:
```python
        if tool in {"store_memory", "log_event", "manage_relation"}:
            kwargs = {**kwargs, "coordinator": coordinator}
```

In `_dispatch_manage_relation` (current lines 232-253), forward the now-available `coordinator`
kwarg into the two calls this spec's trigger is wired into — `store_relation` and
`invalidate_relation` only, **not** `bulk_store_relations` (mirrors §1 upstream-decision 1's own
bulk-path exclusion exactly — `bulk_store_relations` never gains a `coordinator` parameter, so it
has nothing to receive here regardless):
```python
    if kw.get("invalidate"):
        return relation_service.invalidate_relation(
            source_id=kw.get("source_id"),
            target_id=kw.get("target_id"),
            predicate=kw.get("predicate"),
            invalid_at=kw.get("invalid_at"),
            coordinator=kw.get("coordinator"),
        )
    return relation_service.store_relation(
        source_id=kw.get("source_id"),
        target_id=kw.get("target_id"),
        predicate=kw.get("predicate"),
        valid_at=kw.get("valid_at"),
        override_justification=kw.get("override_justification"),
        owner_id=kw.get("owner_id"),
        coordinator=kw.get("coordinator"),
    )
```
No other line of `dispatch.py` changes — no new tool, no new `retrieve_context` parameter, no
`protocol.py` change (constraint 17 is unaffected: this is purely internal coordinator-threading
plumbing for an *existing* tool's dispatch, not a new caller-facing surface).

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

Required test scenario added by Amendment 2, back in `tests/test_community_detection_service.py`
(topically belongs with `recompute_communities`'s own scenarios 1-16 above; numbered to continue
the file's existing sequence rather than renumbering the list):

25. **`sqlite-vec` extension unavailable on this connection**: patch
    `saltmdb.domain.services.community_detection_service.try_load_vector_extension` to
    `return_value=False` (mirrors `test_cohesion_service.py`'s own identical pattern for
    `cohesion_service.try_load_vector_extension`) against a fixture with real qualifying edges and
    real `entity_embeddings` rows for every member — assert `recompute_communities` still completes
    without raising; `communities`/`community_membership` are written normally (structural
    clustering unaffected — same `member_count`/membership as an unpatched run against the same
    fixture); no row exists in `community_embeddings` for any community afterward; and
    `communities_missing_embedding` in the return dict equals `communities_created`. Also exercise
    the step-5 short-circuit under the same patch: a fixture with zero qualifying edges but a
    **pre-existing** `community_embeddings` row from an earlier unpatched recompute — assert
    `community_membership`/`communities` are still cleared (`{"status": "no_edges",
    "communities_created": 0}`), but the pre-existing `community_embeddings` row is left untouched
    (not deleted) precisely because the extension is unavailable this cycle, proving the gate in
    step 5 is real and not merely a no-op that happens to look identical when nothing was there to
    clear.

Required test scenarios added by Amendment 3, back in `tests/test_community_detection_service.py`
(continuing the file's existing sequence):

26. **`trigger_community_detection` with `coordinator=None` takes the legacy path**: patch
    `_librarian_trigger_pool.submit` — assert it is called with `_run_community_detection_pass_impl`
    (not `_run_community_detection_with_coordinator`) when no `coordinator` is passed, proving the
    pre-Amendment-3 behavior is unchanged for every caller that omits the new parameter.
27. **`trigger_community_detection` with a real `coordinator` takes the coordinator path**: pass a
    minimal fake coordinator object (mirrors `tests/test_daemon_server.py`'s own
    `_ImmediateCoordinator` — copy the same `submit(self, name, operation, *, priority, wait=True)`
    shape locally in this test file rather than importing across test files, per this spec's own
    established fixture convention) — patch `_librarian_trigger_pool.submit` and assert it is
    called with `_run_community_detection_with_coordinator` and the coordinator object, not
    `_run_community_detection_pass_impl`.
28. **`_run_community_detection_pass_on_connection` end-to-end against a real connection**:
    construct a fixture with real qualifying edges and a real `_system_locks` row, call
    `_run_community_detection_pass_on_connection(conn)` directly (no coordinator object needed —
    this function takes a bare connection) — assert it returns "Community detection pass
    complete: ..." and that `communities`/`community_membership`/`community_embeddings` are
    populated identically to an equivalent `recompute_communities(db_connection=conn)` call, proving
    this function is a correct thin wrapper, not a divergent reimplementation. Also exercise its own
    empty-edge-set and cooldown-not-elapsed returns ("Skipped: no qualifying relation edges to
    cluster."/"Skipped: cooldown not elapsed."), mirroring scenarios 19/21's own assertions but
    against this function directly rather than `_run_community_detection_pass_impl`.

Required test scenarios added by Amendment 3, in `tests/test_relation_service.py` (alongside
scenarios 22-24 from Amendment 1):

29. **`store_relation` forwards its `coordinator` argument to the trigger call**: patch
    `trigger_community_detection` and call `store_relation` with a sentinel `coordinator` object —
    assert the patched trigger was called with that exact same sentinel as its own `coordinator`
    kwarg (not merely called at all, per scenario 22 — this scenario specifically proves the value
    is threaded through, not dropped or replaced with `None`).
30. **`invalidate_relation` forwards its `coordinator` argument to the trigger call**: same shape as
    scenario 29, for `invalidate_relation`'s own success path.

Required test scenario added by Amendment 3, in `tests/test_phase3_mcp_surface.py` (mirrors that
file's own existing `dispatch.MUTATING_TOOLS` membership-check convention, e.g. its
`self.assertIn("update_memory_metadata", dispatch.MUTATING_TOOLS)` pattern):

31. **`manage_relation` receives its `coordinator` kwarg through `_dispatch_tool_inner`, and
    `_dispatch_manage_relation` forwards it to `store_relation`/`invalidate_relation` but never to
    `bulk_store_relations`**: assert `"manage_relation"` is in the coordinator-injection set
    alongside `"store_memory"`/`"log_event"` (a direct set-membership or call-through check,
    whichever is more natural against `_dispatch_tool_inner`'s actual implementation shape); then,
    with `relation_service.store_relation`/`invalidate_relation` patched, dispatch a `manage_relation`
    call with a sentinel `coordinator` through `_dispatch_manage_relation` directly (non-bulk, both
    the create and invalidate branches) and assert the sentinel reaches the patched function's own
    `coordinator` kwarg; then dispatch a `relations=[...]` (bulk) call and assert
    `bulk_store_relations` — separately patched — is called with no `coordinator` kwarg at all
    (proving the bulk-exclusion from §1 upstream-decision 1 is real at the dispatch layer, not just
    in `relation_service.py`'s own signatures).

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
must exit 0, and every scenario in §8's first list (1-21, plus 25 added by Amendment 2 and 26-28
added by Amendment 3) must correspond to at least one passing test (a reviewer checks this by
name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/test_relation_service.py -v -k "community_detection"
```
must exit 0, covering §8's scenarios 22-24 and 29-30 (Amendment 3).

```bash
PYTHONPATH=src uv run pytest tests/test_phase3_mcp_surface.py -v -k "manage_relation or coordinator"
```
must exit 0, covering §8's scenario 31 (Amendment 3) — adjust the `-k` filter to whatever the
actual test method name(s) end up being if this exact filter doesn't match; the requirement is
that scenario 31 runs and passes as part of this command, not the literal filter string.

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite), **with one named, narrow exception added
by Amendment 3**: `tests/test_daemon_server.py::TestDaemonSignalShutdown::test_sigterm_triggers_clean_shutdown_without_deadlock`
is a known pre-existing, timing-sensitive/environment-specific flake on this machine, independently
established on a clean baseline *before this spec's own changes ever existed* (SALTMDB memory
`6f956bfb`, via `git stash`-and-rerun) and independently re-confirmed during this amendment's own
adjudication (5 consecutive isolated runs passed on both the clean `context-aware-search` baseline
and this spec's own worktree; a full `pytest tests/ -q` run on each passed 100% including this
test — its failure is real but nondeterministic, not reliably reproducible even with zero changes
from this spec applied). A failure in exactly this one test, and no other, does not block
acceptance. **Any other failing test anywhere else in the suite still fails this acceptance bar in
full** — this carve-out names one specific, already-flaky nodeid, not a general tolerance for
full-suite failures.

```bash
uv run ruff check src/saltmdb/domain/services/community_detection_service.py \
  src/saltmdb/db/schema.py src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py \
  src/saltmdb/daemon/dispatch.py \
  tests/test_community_detection_service.py tests/test_relation_service.py tests/test_phase3_mcp_surface.py && \
uv run ruff format --check src/saltmdb/domain/services/community_detection_service.py \
  src/saltmdb/db/schema.py src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py \
  src/saltmdb/daemon/dispatch.py \
  tests/test_community_detection_service.py tests/test_relation_service.py tests/test_phase3_mcp_surface.py && \
uv run mypy src/saltmdb/domain/services/community_detection_service.py src/saltmdb/db/schema.py \
  src/saltmdb/db/vector_schema.py src/saltmdb/domain/services/relation_service.py src/saltmdb/daemon/dispatch.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`). **Amendment 3
carve-out**: `ruff format --check` on `src/saltmdb/db/schema.py` and
`src/saltmdb/domain/services/relation_service.py` may reformat exactly two pre-existing,
unrelated blocks that already violate this repo's current `ruff format` output on the clean
baseline (confirmed independently — see Amendment 3): the string-concatenation lines inside
`_ensure_agent_sessions_table` (`schema.py`, currently ~line 210) and the `status = "duplicate"
if ... else "success"` line inside `bulk_store_relations` (`relation_service.py`, currently
~line 1883). Reformatting these two specific, already-broken, functionally-unrelated blocks to
satisfy `ruff format --check` is in scope and expected; no other reformatting of either file is
permitted, and no other file in this command's list may need reformatting for reasons unrelated
to this spec's own additions.

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

## Amendment 2 — `uv.lock` scope gap and a real vec0-extension-loading bug in §6.1

**Reported by OMP** (as forward notes on its second attempt, not a second formal `BLOCKED` report,
since it correctly took no action while Amendment 1's contradiction was still unresolved): (1)
`uv.lock` may need its own scope amendment; (2) fresh trigger-worker connections need vec0
extension handling before vector reads/writes. Two other notes OMP raised — trigger-call
duplicate/no-op return-placement semantics, and existing-relation-test async/test-mode isolation
— are **not** amended here: both are already explicitly addressed by existing spec text (§8
scenario 22's own "whichever the implementation actually does, the test must pin it down
explicitly" clause, and §8's existing `SALTMDB_TEST_MODE`/no-real-background-pool-submission
guidance) and require no scope or algorithm change, just implementation-time attention OMP already
has enough to act on.

**Both remaining items verified independently against the actual current tree before amending**
(no code written or run beyond reading — a text/precedent check, not a probe):

1. **`uv.lock` gap is real.** §2 adds two new runtime dependencies to `pyproject.toml`; `uv
   sync`/`uv lock` regenerates `uv.lock` as a direct, unavoidable mechanical consequence, and §0's
   file list never named it — exactly the `spec-writing` skill's own documented "mechanical
   step's output file never named in scope" failure shape (a lockfile regenerated by a package
   manager). This is the same root cause as Amendment 1 (a file genuinely touched by the spec's
   own instructions, never added to §0's affirmative list) — not a new category of gap.

2. **The vec0-extension-loading gap is real and would have broken the implementation at
   runtime**, confirmed by reading `src/saltmdb/db/connection.py` (does not load `sqlite_vec` for
   any connection it opens — extension loading is per-connection, not global) and
   `src/saltmdb/db/vector_schema.py`'s own `try_load_vector_extension` (built exactly for "a
   caller that opens their own ad-hoc connection... and needs the extension loaded before
   querying [a] vec0 virtual table," returning `False` instead of raising so callers degrade
   gracefully). `cohesion_service.get_fresh_entity_centroids` (`cohesion_service.py:83`) is the
   established precedent for this exact situation — call `try_load_vector_extension(conn)` once,
   gate every subsequent vec0-table read/write on its return value — with a matching test-mock
   precedent already in `tests/test_cohesion_service.py` (`patch(
   "saltmdb.domain.services.cohesion_service.try_load_vector_extension", return_value=False)`),
   which requires the import to be module-level, not local to a function, for `patch`'s dotted
   path to resolve.

   §6.1 as originally drafted opened its own connection (or received one from
   `_run_community_detection_pass_impl`, itself also freshly opened, per §6.3) and never loaded
   the extension on it at all, before: reading `entity_embeddings` (step 8, a vec0 table); writing
   `community_embeddings` (step 12, a vec0 table); and clearing `community_embeddings` in the
   empty-edge-set short-circuit (step 5, a vec0 table — a bare `DELETE` against a virtual table
   still requires its module registered on the connection executing it). All three would raise on
   a real, freshly-opened connection that never had `sqlite_vec.load()` called on it — this is not
   a hypothetical or an edge case, it is the connection `_run_community_detection_pass_impl`
   *always* opens for every triggered pass.

**Resolution**: §6's module-level imports gain `try_load_vector_extension` (mirrors
`cohesion_service.py:4`'s exact placement, for testability). §6.1 step 1 gains a
`try_load_vector_extension(conn)` call immediately after opening the connection, storing
`vector_extension_loaded` for steps 5/8/12 to read. Step 5's and step 12's `community_embeddings`
`DELETE`/`INSERT` statements are now gated on that flag (skipped entirely, not erroring, when
`False` — structural clustering into `communities`/`community_membership` proceeds unaffected
either way). Step 8's `entity_embeddings` `SELECT` is now gated the same way, degrading to an
empty `raw_vector` — which step 9's already-existing centroid logic already treats identically to
"no member has a usable embedding," requiring no new branch there. A new required test scenario,
25, is added to §8 (`recompute_communities`'s own scenario list) covering the degrade path
directly, mirroring `test_cohesion_service.py`'s own mock pattern. §10 Acceptance's scenario-count
line updated to include it.

No other section of this spec changes. This amendment does not alter §1's locked design decisions,
§7's `relation_service.py` changes, or Amendment 1's own resolution — it is purely a §6.1
algorithm-correctness fix plus the two mechanically-necessitated scope additions (`uv.lock`,
already-permitted `try_load_vector_extension` import) it depends on. OMP may resume implementation
immediately against the amended §0/§6.

## Amendment 3 — Full-suite flake, a pre-existing ruff-format contradiction, and a real daemon
write-boundary bug (all three verified independently, none merely trusted from OMP's report)

**Reported by OMP** as `BLOCKED — SPEC ADJUDICATION REQUIRED` on its next attempt, with fresh
acceptance evidence (focused suite 125 passed; full suite 1652 passed / 1 failed / 12 skipped),
citing three separate problems: (1) a full-suite test failure
(`TestDaemonSignalShutdown::test_sigterm_triggers_clean_shutdown_without_deadlock`, exit -15 vs.
expected 0) which OMP itself flagged against SALTMDB precedent `6f956bfb` as likely pre-existing;
(2) a "formatter/scope contradiction" — `ruff format --check` on the required file set only passes
if it reformats two lines OMP read the spec as forbidding it to touch; (3) a "daemon write-boundary
contradiction" — the mandated background worker opens a connection under the daemon boundary that
turns out to be query-only, then attempts the required `_system_locks`/community-table writes. OMP
again performed no unauthorized edits and left the worktree exactly as its own report described:
uncommitted, unmerged, only the (then-) nine §0-permitted paths touched.

**All three verified independently against the actual tree/history before amending** — none taken
on OMP's word alone, per this workspace's `spec-writing`/adjudication protocol:

1. **The daemon-shutdown test is genuinely flaky, not a regression this spec introduces.** Ran it
   5x in isolation on the clean `context-aware-search` baseline (5/5 passed) and 5x in isolation in
   this spec's own worktree (5/5 passed), then ran the *full* suite once on each: baseline
   `1655 passed, 0 failed`; worktree `1653 passed, 12 skipped, 0 failed` (the `12 skipped` exactly
   matches OMP's own report, but the `1 failed` did not reproduce either time). This is consistent
   with — not merely re-asserting — memory `6f956bfb`'s own independent `git stash`-confirmed
   finding that this exact test fails intermittently on a clean baseline with zero changes applied,
   on this machine, for reasons unrelated to any specific feature work. Resolved via a narrow,
   named §10 carve-out (below) rather than either ignoring the acceptance bar generally or forcing
   OMP into an unfixable retry loop against a test its own changes don't control.

2. **The ruff-format contradiction is real, and pre-exists this spec entirely.** Ran
   `uv run ruff format --check` against every file in the acceptance command's list, on the clean
   baseline, *before any Milestone C change exists*: `src/saltmdb/db/schema.py` and
   `src/saltmdb/domain/services/relation_service.py` **both already fail** — `schema.py` wants to
   collapse a 3-line string-concatenation inside `_ensure_agent_sessions_table` (current lines
   208-211, nowhere near this spec's own `_system_locks`/table-creation additions) to one line;
   `relation_service.py` wants to reformat a `status = "duplicate" if ... else "success"` ternary
   inside `bulk_store_relations` (current line ~1883, nowhere near `store_relation`/
   `invalidate_relation`) the same way. Confirmed OMP's own actual diff already contains this exact
   `bulk_store_relations` reformat, sitting alongside its two legitimate trigger-call additions —
   OMP correctly performed the reformat (since leaving it out would fail `ruff format --check`) but
   correctly flagged it as outside §0's literal "no other change to this file" framing, rather than
   silently overstepping. This is the same root cause as Amendments 1 and 2 (a real, mechanically-
   necessary change §0 never named) — not a new failure category.

3. **The daemon write-boundary contradiction is real and would have broken the trigger in every
   actual production deployment**, confirmed by reading `src/saltmdb/db/connection.py`,
   `src/saltmdb/daemon/db_write_coordinator.py`, `src/saltmdb/daemon/dispatch.py`, and
   `src/saltmdb/domain/services/librarian_service.py` directly (no code written or run beyond
   these reads and the verification runs in items 1-2 above). The mechanism: after daemon
   bootstrap, `connection.py`'s `enable_daemon_connection_boundary()` makes bare `get_connection()`
   calls from any thread other than the coordinator's own writer thread return a genuinely
   **read-only** connection (`open_read_connection`, `PRAGMA query_only=ON`) — `_daemon_boundary_
   enabled` is checked only after the `_coordinator_connection` ContextVar comes back empty, and
   that ContextVar is **thread-local**: it is set only for the duration of a closure the coordinator
   itself invokes on its own dedicated writer thread, and does **not** propagate into a *different*
   `ThreadPoolExecutor` worker thread (`connection.py:17-22,96-114`). §6.2/§6.3 as originally
   drafted submit `_run_community_detection_pass_impl` onto `_librarian_trigger_pool` — a separate
   thread pool from the coordinator's writer thread — where its own `get_connection(db_path)` call
   would therefore return a read-only connection in a real daemon, and every subsequent write
   (the `_system_locks` cooldown-claim `UPDATE`, and `recompute_communities`'s own table writes)
   would raise. Confirmed this is a **known, already-solved problem** in this exact codebase, not a
   novel design question: `librarian_service.trigger_librarian`/`run_librarian_now` already accept
   an explicit `coordinator=None` parameter and, when given one, hop from the trigger-pool thread
   onto the coordinator's own writer thread via `coordinator.submit(...)` (a *second*, explicit
   submission — the ContextVar's thread-locality is exactly why an implicit hand-off can't work);
   `store_memory`/`log_event` already accept and forward a `coordinator` parameter for exactly this
   reason (`memory_service/write.py:592`, `event_service.py:21`); and `dispatch.py`'s
   `MUTATING_TOOLS`/`_dispatch_tool_inner` (lines 477-499) already inject `coordinator` into
   `store_memory`/`log_event`'s kwargs specifically so they can forward it — `relation_service.py`'s
   `store_relation`/`invalidate_relation` never received the same treatment, and neither did
   `dispatch.py`'s `_dispatch_manage_relation` (confirmed: it forwards a fixed, explicit parameter
   list with no `coordinator` today, `dispatch.py:232-253`), so even injecting `coordinator` into
   `manage_relation`'s dispatch kwargs would have been silently dropped without also updating that
   function.

**Adjudication.** Item 1: narrow, named §10 carve-out (added above) — the acceptance bar's "no
regression" intent is preserved (any *other* failure still blocks), while a test this spec's own
changes provably don't control, and which fails nondeterministically even against a change-free
baseline, no longer wrongly gates this spec's acceptance. Item 2: widen §0 to explicitly permit
these two specific, pre-existing, functionally-unrelated reformats (added above) — mirrors
Amendments 1/2's own "widen scope to match a real mechanical necessity, never shrink the acceptance
bar to route around it" precedent exactly. Item 3: widen §0/§7 to add the `coordinator`-threading
plumbing (§6.2, §7, §7.1 above) — this is the largest of the three amendments by diff size, but is
**entirely mechanical and precedented**, mirroring `trigger_librarian`/`run_librarian_now`/
`store_memory`/`log_event`/`dispatch.py`'s own already-shipped pattern near line-for-line rather
than inventing a new design; every locked §1 decision (constraint 18's fire-and-forget/shared-pool
framing included) is preserved unchanged — the coordinator branch is an *additional* path alongside
the existing no-coordinator one, never a replacement for it, so every direct/test-mode caller that
never had a coordinator to begin with is unaffected.

New required test scenarios (26-31) added to §8 above, covering: both branches of
`trigger_community_detection`'s new `coordinator` parameter; `_run_community_detection_pass_on_
connection`'s own correctness against a real connection; `store_relation`/`invalidate_relation`
correctly forwarding (not dropping) their own `coordinator` argument; and `dispatch.py`'s own
injection-and-forwarding, including the explicit proof that `bulk_store_relations` never receives
one. §10 Acceptance extended with `dispatch.py` and `tests/test_phase3_mcp_surface.py` in the
lint/type/test commands.

No change to any §1 locked design decision or to Amendments 1/2's own resolutions. OMP may resume
implementation immediately against the amended §0/§6/§7/§7.1/§8/§10.
