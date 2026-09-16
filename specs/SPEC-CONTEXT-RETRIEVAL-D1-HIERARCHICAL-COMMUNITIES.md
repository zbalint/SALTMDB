# SPEC-CONTEXT-RETRIEVAL-D1-HIERARCHICAL-COMMUNITIES

## 0. Status

**LOCKED**

**Depends on** Milestone C.5 (`SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`) being implemented,
reviewed, merged into `context-aware-search`, and live-deployed — already true as of this spec's
drafting (memory `50eae206` / `8e7b1204`). This spec reads and extends the `communities` /
`community_membership` / `community_embeddings` tables Milestone C created.

**Scope**: may edit `src/saltmdb/config.py` (add exactly two new constants,
`COMMUNITY_HIERARCHY_SIZE_THRESHOLD` and `COMMUNITY_HIERARCHY_MAX_DEPTH`, see §2); may edit
`src/saltmdb/db/schema.py` (add exactly one new nullable column, `parent_community_id`, to the
existing `communities` table via the existing `_add_column_if_missing` additive-migration helper,
see §3); may edit `src/saltmdb/domain/services/community_detection_service.py` (replace
`recompute_communities`'s flat per-group loop with a recursive helper, add one new public helper
function `fetch_leaf_community_centroids`, see §4); may edit
`src/saltmdb/domain/services/orphan_community_service.py` (replace its own direct
`community_embeddings` query with a call to the new `fetch_leaf_community_centroids` helper — one
query-construction site changes, nothing else in that file, see §5); may edit
`tests/test_community_detection_service.py` and `tests/test_orphan_community_service.py` (new
scenarios only, append-only, see §§6-7). **Does not touch**: `retrieve_context_service.py`,
`context_budget_service.py`, `conflict_set_service.py`, `context_expansion_service.py`,
`lineage_assembly_service.py`, `mcp/tools.py`, `daemon/dispatch.py`, `daemon/protocol.py` — this
spec is Milestone D's own internal hierarchy-infrastructure half only (constraint 25); the
caller-facing `strategy: "global"` retrieval mode (constraints 24, 26, 27, 28) is a separate,
sequenced-after spec (`SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL.md`) that depends on this one
being merged first, mirroring the C → C.5 dependency precedent exactly (`SPEC-CONTEXT-RETRIEVAL-
C5-ORPHAN-ASSIGNMENT.md`'s own §0). `vector_schema.py`'s `community_embeddings` table definition
is unchanged — every level's centroid (leaf or non-leaf) is still just one row keyed by
`community_id`, no new column needed there.

**Pre-lock gate completed against the current tree**: `recompute_communities`,
`_compute_community_group`, `_normalize_community_vector`, and the two trigger-runner functions
were re-read in full (`community_detection_service.py`, all 353 lines) before drafting §4.
`orphan_community_service.py` was re-read in full (184 lines) before drafting §5 — confirmed its
downstream member-ranking logic (its own lines 111-133) already operates only on
`community_membership` rows for one already-chosen `community_id`, which after this spec always
means leaf-level rows by construction (§4's own design decision 2), so that logic needs **no**
change — only the upstream centroid-candidate query (its own lines 70-72) does. Grepped
`COMMUNITY_HIERARCHY_SIZE_THRESHOLD`, `COMMUNITY_HIERARCHY_MAX_DEPTH`, `parent_community_id`,
`fetch_leaf_community_centroids` across the whole tree: zero prior occurrences. Grepped
`INSERT INTO communities` and `FROM communities` across `src/` and `tests/`: the only production
INSERT site is `community_detection_service.py`'s own `_write` (this spec's own edit target); the
only test-side raw INSERT site (`tests/test_orphan_community_service.py:96`) omits
`parent_community_id` entirely, which is safe and requires no fixture edit — the new column is
nullable with no explicit default needed, and an omitted nullable column already means exactly
what an existing (pre-hierarchy) leaf-with-no-parent community should mean: `NULL`. Verified via a
throwaway SQLite probe (`python3` in-memory, not part of this repo) that (a) a bulk
`DELETE FROM communities` with `PRAGMA foreign_keys=ON` succeeds on a self-referencing
`parent_community_id REFERENCES communities(id) ON DELETE CASCADE` column regardless of row
deletion order, and (b) `ALTER TABLE communities ADD COLUMN parent_community_id TEXT REFERENCES
communities(id) ON DELETE CASCADE` (the exact `_add_column_if_missing` shape) succeeds and enforces
correctly on an already-populated table — both were live behavior questions this spec's own
correctness depends on, not assumed.

## 1. Why

This is the hierarchy half of Milestone D ("global/community retrieval") of the
`wayfinder:saltmdb:graph-aware-context-retrieval` roadmap, greenlit 2026-09-16 (memory `9adcd8dc`).
A live probe run during that greenlighting session (memory `13549b72`) found flat Leiden
communities are bimodal on this corpus: 27% of communities (41+ members) hold ~65-70% of all
clustered content and are topically heterogeneous — a real `retrieve_context` query landed in an
82-member community where 73/82 members (89%) were unrelated noise. Naively returning "the whole
matched community" (Milestone D's original one-line sketch) would flood callers with noise on the
majority of this corpus's actual content. zbalint's own framing, verbatim: "if we do it we should
do it the right way" — rejecting a flat-with-size-gating v1 in favor of building the hierarchy the
original Leiden research memo (`82eae539`) already prescribed as the fix for exactly this failure
mode: recursive re-run of Leiden on the induced subgraph of any oversized community. This reopens
and reverses standing constraint 20 (memory `f7754901`, which deferred hierarchy to Milestone D
"only if/when D needs it" — that condition is now empirically met, not hypothetical).

This spec implements standing constraint 25 (ticket `9f165899`, resolved 2026-09-16, event
`94579e0f`) in full, plus the one project-wide side effect resolving open gap `14c261d9` requires
of already-shipped Milestone C.5 code (§5 below) — C.5's live orphan-to-community centroid lookup
was written and locked before hierarchy had any concrete shape, under an implicit assumption that
"every row in `community_embeddings` is a valid match target." Once this spec starts writing
non-leaf community rows into that same table, C.5's existing query would silently start matching
zero-edge orphans against coarser, less-precise parent centroids unless it is updated in the same
change — this is not new scope creep, it is fixing a regression this spec's own schema change would
otherwise introduce into already-shipped, already-live code.

**Locked upstream design decisions this spec implements as-is** (constraint 25's own design,
restated only to the depth needed to implement):

1. **Mechanism: recursive re-run of Leiden on each oversized community's own induced subgraph** —
   not a CPM resolution-parameter sweep across the whole graph. The existing top-level pass
   (`leidenalg.find_partition(graph, ModularityVertexPartition)` over the whole relation graph) is
   unchanged and still runs first, producing level-0 communities exactly as today.
2. **Trigger: a pure member-count size threshold** — not a compound size+heterogeneity signal. A
   community with `member_count > COMMUNITY_HIERARCHY_SIZE_THRESHOLD` is a candidate to recurse;
   one below or at the threshold never recurses, regardless of internal heterogeneity.
3. **Recursion: iterative, into any still-oversized child, capped at a new named max-depth
   constant.** A child produced by recursing into an oversized parent is itself checked against
   the same threshold and, if still oversized and the depth cap has not been reached, recursed into
   again. There is no requirement that a recursive Leiden pass make monotonic size progress on any
   one community — see §4's worked example for why this is an accepted, intentional consequence of
   a pure depth cap rather than a bug to special-case.
4. **Representative/centroid computed independently at every level that exists after a recompute**
   — constraint 19's existing PageRank + cosine-medoid mechanism (`_compute_community_group`, §4)
   runs unmodified once per community row, at every level, not only at the finest level. This
   closes self-critique gap `316c7043` (reopened by `9adcd8dc` after constraint 20 previously
   closed it "by mootness" under flat-only framing).
5. **Schema: a new nullable `parent_community_id` self-FK column on `communities`**, refining
   constraint 21's schema (which had already reserved `level` for this, but not this column). A
   `NULL` parent means a level-0 (root-pass) community. This spec explicitly does **not** decide
   which level(s) a caller-facing retrieval mode surfaces — that is Milestone D's other,
   sequenced-after spec's job (`SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL.md`, implementing
   constraint 26). What this spec does fix, because it is a direct, unavoidable consequence of
   this spec's own schema change and not a D2 concern: any *existing* code that already queries
   "every row in `community_embeddings`" as if every row were equally a valid match target (only
   `orphan_community_service.py` does this today) must be updated to mean "every **leaf**
   community" instead — see §5.

**Locked design decisions this spec makes that were left open upstream** (mirroring the sibling
Milestone C/C.5 specs' own §1 precedent for this category):

1. **`community_membership` stores only the leaf (finest) assignment per entity — never more than
   one row per entity, and never a coarser-level row for an entity whose community was further
   split.** `community_membership.entity_id` is already a `PRIMARY KEY` (Milestone C's own schema,
   constraint 21) — an entity can only ever have one membership row. When a level-N community is
   recursed into and split into level-(N+1) children, its own members' membership rows are written
   at level (N+1) against their new child community, **not** at level N against the
   now-non-leaf parent. The parent's own `communities` row still exists (so `parent_community_id`
   chains remain walkable upward from any leaf), it simply has no `community_membership` rows
   pointing at it directly — its members are only reachable transitively via its children. This is
   the only membership model consistent with the existing single-row-per-entity schema; storing a
   second, coarser row per entity would require either a schema change to
   `community_membership`'s primary key (out of scope, and not requested by constraint 25) or a
   redundant, un-normalized duplicate-row design constraint 25 never asks for.
2. **A community row is a "leaf" if and only if no other `communities` row names it via
   `parent_community_id`** — never a `level = MAX(level)` comparison. This formally resolves open
   gap `14c261d9` project-wide (it was originally raised against C.5's own "finest-level
   attachment" wording, but the underlying per-branch-recursion fact it names is a property of
   *this spec's own* recursion shape, not of any one consumer): different branches of the hierarchy
   tree bottom out at different depths (one branch may never exceed the threshold and stays a leaf
   at level 0; another may recurse all the way to the depth cap), so there is no single "finest
   level" number that is finest everywhere in the tree at once. A `level = MAX(level)` filter would
   both silently exclude true leaves sitting in shallower branches and could, in a future recursion
   shape, admit a non-leaf row that happens to share the globally-deepest level. `WHERE NOT EXISTS
   (SELECT 1 FROM communities child WHERE child.parent_community_id = communities.id)` is the only
   filter that is correct by construction regardless of how uneven the tree's branches are.
3. **The recursive step reuses `_compute_community_group` completely unmodified** at every level —
   no new representative-selection or centroid-computation logic is written. The existing function
   already takes a group of graph-local indices, the graph they index into, and a `node_ids` list,
   and returns member ids / representative id / centroid vector generically; calling it again on a
   freshly-built induced subgraph at level N+1 is a direct, intended reuse of its existing generic
   contract, not a new algorithm.
4. **Recursion determinism**: a partition group's member indices are sorted before being passed to
   `igraph`'s `.subgraph()` call and before being used to build the child level's own local
   `node_ids` list — mirrors the existing top-level code's own `node_ids: list[str] = sorted(...)`
   precedent for determinism, extended to every recursion level rather than only the top one.
5. **`community_id` is minted fresh (`uuid.uuid4()`) at every level, exactly as today** — constraint
   21's existing "not stable across recomputes" acceptance already covers every level's rows; this
   spec introduces no new stability requirement for any level.

## 2. `src/saltmdb/config.py`

Insert immediately after the existing `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S` block (current line
249), before the blank line preceding the `# Pairwise cohesion gate` comment (current line 251):

```python

# Milestone D (wayfinder ticket "Milestone D hierarchy mechanics," standing constraint 25, memory
# 94579e0f) -- community_detection_service's hierarchical sub-clustering trigger: a community whose
# member_count exceeds this value is a candidate for recursive re-run of Leiden on its own induced
# subgraph (see recompute_communities/_process_partition_group). A community at or below this value
# never recurses, regardless of internal heterogeneity -- a pure size trigger, not a compound
# size+heterogeneity signal, per constraint 25's own explicit locked choice. PLACEHOLDER: not yet
# benchmarked against SALTMDB's own corpus -- seeded above the live-corpus median (~10, per probe
# 13549b72) and comfortably below the live-corpus's own confirmed "definitely too large, definitely
# heterogeneous" tier (41+ members, same probe), so a community this size or smaller is expected to
# already be plausibly topic-coherent without recursion, not derived from any benchmark of its own.
# Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28)
# via its own concrete quantitative sweep against the live corpus. Do not remove the placeholder
# framing when tuning this; replace this comment with the benchmark citation once a real value is
# locked.
COMMUNITY_HIERARCHY_SIZE_THRESHOLD = 20

# Milestone D (wayfinder ticket "Milestone D hierarchy mechanics," standing constraint 25, memory
# 94579e0f) -- the maximum `communities.level` a recursive sub-clustering pass may ever produce. A
# still-oversized community at this level never recurses further, regardless of its own
# member_count -- capped iterative recursion, not unbounded, per constraint 25's own explicit locked
# choice. Level 0 (the original whole-graph pass) always counts against this cap: a value of 2 means
# levels 0, 1, and 2 may exist, and a level-2 community never produces a level-3 child. PLACEHOLDER:
# not yet benchmarked against SALTMDB's own corpus -- seeded at a small value since the live-corpus
# cost probe (memory acd52d4a) measured only one recursion level's worth of overhead (~3.3% on top
# of the full-graph pass); a deeper cap multiplies that cost per additional level and has not itself
# been measured. Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing
# constraint 28) via its own concrete quantitative sweep against the live corpus. Do not remove the
# placeholder framing when tuning this; replace this comment with the benchmark citation once a real
# value is locked.
COMMUNITY_HIERARCHY_MAX_DEPTH = 2
```

## 3. `src/saltmdb/db/schema.py`

Immediately after the existing `communities` table's `CREATE TABLE IF NOT EXISTS` statement (current
lines 853-862), before the `community_membership` table's own `CREATE TABLE IF NOT EXISTS` (current
line 864), insert:

```python
        # Milestone D (wayfinder standing constraint 25, memory 94579e0f) -- self-referencing FK
        # supporting hierarchical sub-clustering: NULL for a level-0 (root full-graph pass)
        # community, or the id of the community this row was produced by recursively re-running
        # Leiden on. A row with no other row naming it here is a LEAF community -- the correct,
        # construction-guaranteed definition project-wide (see community_detection_service.py's
        # fetch_leaf_community_centroids and open gap 14c261d9's own resolution); never derive
        # "leaf" from a level-number comparison instead. Additive migration via the existing
        # _add_column_if_missing helper (mirrors how `level` itself was reserved ahead of need by
        # constraint 21) -- nullable, so every already-populated pre-hierarchy communities row
        # (parent_community_id implicitly NULL) already means exactly what a leaf-with-no-parent
        # community should mean, no backfill required.
        _add_column_if_missing(
            conn,
            "communities",
            "parent_community_id TEXT REFERENCES communities(id) ON DELETE CASCADE",
        )
```

No other schema file changes. `vector_schema.py`'s `init_community_vector_schema` (the
`community_embeddings` vec0 table) is untouched — it is still exactly one row per `community_id`
regardless of level, which this spec's §4 continues to populate for every level, leaf or not.

## 4. `src/saltmdb/domain/services/community_detection_service.py`

### 4.1 New import

Add `COMMUNITY_HIERARCHY_MAX_DEPTH` and `COMMUNITY_HIERARCHY_SIZE_THRESHOLD` to the existing
`from saltmdb.config import ...` line (current line 9):

```python
from saltmdb.config import (
    COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S,
    COMMUNITY_HIERARCHY_MAX_DEPTH,
    COMMUNITY_HIERARCHY_SIZE_THRESHOLD,
    get_db_path,
)
```

### 4.2 New function `_process_partition_group` (replaces the current inline per-group loop body)

Insert this new module-level private function immediately before `recompute_communities` (i.e.,
immediately after `_normalize_community_vector`, current line 90). It reuses `_compute_community_group`
unmodified and is the one place recursion happens:

```python
def _process_partition_group(  # noqa: PLR0913
    group_indices: list[int],
    graph,
    node_ids: list[str],
    raw_vector: dict[str, "np.ndarray"],
    level: int,
    parent_community_id: str | None,
    now: str,
    communities_to_insert: list[tuple],
    membership_to_insert: list[tuple],
    embeddings_to_insert: list[tuple],
) -> None:
    """Insert one community row for this partition group at `level`, then either recurse into a
    fresh Leiden pass over its own induced subgraph (if oversized and under the depth cap) or emit
    leaf-level community_membership rows for its members. Every community row this function ever
    creates -- leaf or not -- gets its own representative/centroid via the existing,
    completely-unmodified _compute_community_group (constraint 25's own "independently at every
    level" requirement)."""
    sorted_group = sorted(group_indices)
    member_ids, representative_id, centroid_vec = _compute_community_group(
        sorted_group, graph, node_ids, raw_vector
    )
    member_count = len(member_ids)
    community_id = str(uuid.uuid4())
    communities_to_insert.append(
        (community_id, representative_id, member_count, level, now, parent_community_id)
    )
    if centroid_vec is not None:
        import sqlite_vec

        embeddings_to_insert.append(
            (
                community_id,
                sqlite_vec.serialize_float32(centroid_vec.astype(np.float32).tolist()),
            )
        )

    if member_count > COMMUNITY_HIERARCHY_SIZE_THRESHOLD and level < COMMUNITY_HIERARCHY_MAX_DEPTH:
        import leidenalg

        child_graph = graph.subgraph(sorted_group)
        child_node_ids = [node_ids[i] for i in sorted_group]
        child_partition = leidenalg.find_partition(child_graph, leidenalg.ModularityVertexPartition)
        for child_group in child_partition:
            _process_partition_group(
                list(child_group),
                child_graph,
                child_node_ids,
                raw_vector,
                level + 1,
                community_id,
                now,
                communities_to_insert,
                membership_to_insert,
                embeddings_to_insert,
            )
    else:
        for entity_id in member_ids:
            membership_to_insert.append((entity_id, community_id, level))
```

`import numpy as np` already happens earlier in `recompute_communities` (current line 159) before
the loop this replaces runs; `_process_partition_group`'s own type hint on `raw_vector` uses a
string-quoted `"np.ndarray"` annotation for the same reason `_compute_community_group` itself has no
top-of-file `numpy` import — `numpy` is imported lazily inside `recompute_communities`, not at
module scope.

**Worked example (satisfies this file's own pre-lock-gate step 8 — concrete instance walked through
the actual locked rule)**: `COMMUNITY_HIERARCHY_SIZE_THRESHOLD = 20`,
`COMMUNITY_HIERARCHY_MAX_DEPTH = 2`. A level-0 community with 82 members (this corpus's own real
probe case, memory `13549b72`) has `member_count(82) > threshold(20)` and `level(0) <
max_depth(2)`, so it recurses: `leidenalg.find_partition` on its induced subgraph yields, say,
sub-groups of sizes `[30, 25, 15, 12]`. The 15- and 12-member children are `<= 20`, so each becomes
a level-1 leaf immediately (their members get `community_membership` rows at level 1 against their
own new community_id). The 30- and 25-member children are still `> 20` and `level(1) < max_depth(2)`,
so each recurses again. Suppose the 30-member child's own recursive pass yields `[22, 8]` at level
2: the 8-member grandchild is `<= 20` and becomes a level-2 leaf; the 22-member grandchild is still
`> 20`, but `level(2) < max_depth(2)` is **false** — it becomes a level-2 leaf anyway, despite still
being oversized, because the depth cap governs regardless of size. This is the accepted, intentional
behavior constraint 25's "capped iterative recursion" locks — not a bug for a future session to
special-case with a "make progress or stop early" rule; termination is guaranteed by the depth cap
alone, independent of whether any given recursive Leiden pass actually reduces heterogeneity.

### 4.3 New function `fetch_leaf_community_centroids`

Insert immediately after `recompute_communities` (current line 224), before
`trigger_community_detection` (current line 227):

```python
def fetch_leaf_community_centroids(conn: sqlite3.Connection) -> list[tuple[str, bytes]]:
    """Community centroids for LEAF communities only -- a community row with no other row naming
    it via parent_community_id (never a `level = MAX(level)` comparison, which per-branch recursion
    (this file's own _process_partition_group) makes unsafe: different branches of the hierarchy
    tree can bottom out at different depths, so there is no single "finest level" number that is
    finest everywhere in the tree at once).

    Returns the exact same (community_id, embedding_blob) row shape a raw
    `SELECT community_id, embedding FROM community_embeddings` already returns, so an existing call
    site can replace its own raw query with a call to this function with no further change to how
    it consumes the result. Before hierarchy has ever produced a single non-leaf row (a fresh
    install, or a corpus whose communities have never exceeded COMMUNITY_HIERARCHY_SIZE_THRESHOLD),
    every community is trivially a leaf and this returns the same rows the old unfiltered query
    would have.
    """
    return conn.execute(
        """
        SELECT ce.community_id, ce.embedding
        FROM community_embeddings ce
        JOIN communities c ON c.id = ce.community_id
        WHERE NOT EXISTS (
            SELECT 1 FROM communities child WHERE child.parent_community_id = c.id
        )
        """
    ).fetchall()
```

### 4.4 `recompute_communities` — replace the per-group loop, extend the INSERT

Replace the current per-group loop (lines 173-194):

```python
        communities_to_insert = []
        membership_to_insert = []
        embeddings_to_insert = []

        for group in partition:
            member_ids, representative_id, centroid_vec = _compute_community_group(
                group, graph, node_ids, raw_vector
            )
            member_count = len(member_ids)
            community_id = str(uuid.uuid4())
            communities_to_insert.append((community_id, representative_id, member_count, 0, now))
            for entity_id in member_ids:
                membership_to_insert.append((entity_id, community_id, 0))
            if centroid_vec is not None:
                import sqlite_vec

                embeddings_to_insert.append(
                    (
                        community_id,
                        sqlite_vec.serialize_float32(centroid_vec.astype(np.float32).tolist()),
                    )
                )
```

with:

```python
        communities_to_insert: list[tuple] = []
        membership_to_insert: list[tuple] = []
        embeddings_to_insert: list[tuple] = []

        for group in partition:
            _process_partition_group(
                list(group),
                graph,
                node_ids,
                raw_vector,
                0,
                None,
                now,
                communities_to_insert,
                membership_to_insert,
                embeddings_to_insert,
            )
```

Then, in the same function's inner `_write` closure (current lines 196-214), extend the INSERT
column list and each tuple by one column (`communities_to_insert` tuples are now 6-element, not
5-element, per §4.2's own append shape above):

```python
            c.executemany(
                "INSERT INTO communities "
                "(id, representative_entity_id, member_count, level, created_at, parent_community_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                communities_to_insert,
            )
```

The `community_membership` and `community_embeddings` INSERT statements in `_write` are unchanged —
their own column shapes were never touched by this spec (`membership_to_insert`/`embeddings_to_insert`
tuples keep their existing 3-element/2-element shapes; only which rows end up in them changes, per
§4.2's leaf-only-membership behavior).

The function's own return dict (`"communities_created"`, `"nodes_clustered"`,
`"communities_missing_embedding"`) is unchanged in shape and continues to count every row across
every level (leaf and non-leaf combined) — a natural generalization of what it already counted, not
a new decision requiring its own PLACEHOLDER or acceptance criterion.

## 5. `src/saltmdb/domain/services/orphan_community_service.py`

Replace the current direct query (lines 70-72):

```python
        centroid_rows = conn.execute(
            "SELECT community_id, embedding FROM community_embeddings"
        ).fetchall()
```

with:

```python
        from saltmdb.domain.services.community_detection_service import (
            fetch_leaf_community_centroids,
        )

        centroid_rows = fetch_leaf_community_centroids(conn)
```

No other line in this file changes. Every downstream consumer of `centroid_rows` (the
`_normalize`-then-cosine-similarity loop building `centroids`, current lines 75-78) is unchanged —
it already treats each row as an equally-valid match target, which after this one-query swap is
finally true again (leaf-only) rather than silently including non-leaf rows once D1 populates them.
The function's own downstream member-ranking logic (current lines 111-133, ranking a *chosen*
community's own `community_membership` rows by similarity to the orphan) needs no change at all:
per §1's own design decision 1, `community_membership` only ever contains leaf-level rows by
construction, so that code was already correct and stays correct without modification.

## 6. `tests/test_community_detection_service.py` — new scenarios

Append new scenarios continuing this file's own `test_scenario_NN` numbering (currently ends at 28)
starting at 29. Required coverage, each as its own `test_scenario_NN_<description>` method following
this file's existing fixture conventions (`self.conn`, `store_memory`/`store_relation` helpers
already defined in the file):

- **29 — oversized community recurses into real children.** Build a graph with one connected
  component whose member count exceeds a monkey-patched small `COMMUNITY_HIERARCHY_SIZE_THRESHOLD`
  (e.g. patch to 3) and enough internal edge structure for Leiden to find more than one sub-group on
  the induced subgraph. Assert: the level-0 `communities` row for this component has
  `parent_community_id IS NULL`; at least one level-1 `communities` row exists with
  `parent_community_id` equal to the level-0 row's id; `community_membership` contains rows at level
  1 for this component's entities, and **zero** rows at level 0 for them (leaf-only membership,
  §1 decision 1).
- **30 — child below threshold does not recurse further.** A level-0 community just above threshold
  splits into children each below threshold; assert no level-2 `communities` rows exist for this
  branch and each level-1 child's own row has no other row naming it via `parent_community_id`
  (i.e. each is correctly a leaf).
- **31 — recursion is capped at max depth even while still oversized.** Monkey-patch both
  `COMMUNITY_HIERARCHY_SIZE_THRESHOLD` (small) and `COMMUNITY_HIERARCHY_MAX_DEPTH` (e.g. 1) so a
  component's structure would keep exceeding threshold past the cap; assert the deepest
  `communities` row produced for this branch has `level == COMMUNITY_HIERARCHY_MAX_DEPTH` and no
  row at `level + 1` exists for it, even though its own `member_count` is still `>` the threshold.
- **32 — representative/centroid computed independently at every level.** For a component that
  recurses at least once, assert both the level-0 (non-leaf) row and its level-1 children's rows
  each have their own `representative_entity_id` and their own `community_embeddings` row (i.e. the
  non-leaf parent's own centroid is real and present, not merely inherited/copied from a child).
- **33 — flat (non-recursing) behavior is unchanged.** A component at or below the real
  (non-patched) `COMMUNITY_HIERARCHY_SIZE_THRESHOLD` produces exactly the same `communities` /
  `community_membership` / `community_embeddings` rows as before this spec (regression guard against
  §4's refactor of the previously-inline loop into `_process_partition_group`).
- **34 — full recompute clears every level, not just level 0.** After a recompute that produced
  levels 0-2 for some component, trigger a second recompute against a graph with no qualifying
  edges (the existing `_clear` no-edges path); assert `communities`, `community_membership`, and
  `community_embeddings` are all empty afterward, regardless of how many levels the prior recompute
  produced (regression guard on the existing `_clear`/`_write` full-wipe-and-rebuild contract,
  constraint 21, now exercised with non-trivial depth for the first time).

## 7. `tests/test_orphan_community_service.py` — new scenario

Append one new scenario continuing this file's own numbering (currently ends at 14), scenario 15:

- **15 — orphan assignment matches against leaf centroids only, never a non-leaf parent's.** Using
  this file's existing raw-fixture pattern (its own line 96's direct
  `INSERT INTO communities (...)`), construct one level-0 community row with a real centroid and one
  level-1 child row (via an explicit `parent_community_id` set to the level-0 row's id) that also
  has its own, *different*, real centroid — engineer the level-0 (non-leaf) centroid to be the
  closer match to a synthetic orphan's own embedding, while the level-1 (leaf) centroid is farther
  but still above `COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`. Assert `find_orphan_community_matches`
  assigns the orphan to the level-1 (leaf) community, never the level-0 (non-leaf) one — proving the
  leaf-filter (§5) is actually load-bearing and not merely present-but-unreached by this test suite.

## 8. Out of scope

- Any change to which level(s) a caller-facing retrieval mode surfaces, or to any MCP/dispatch
  surface — Milestone D's `strategy: "global"` retrieval mode (constraints 24/26/27/28) is
  `SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL.md`'s job, sequenced strictly after this spec merges.
- Any change to `retrieve_context_service.py`, `context_budget_service.py`,
  `context_expansion_service.py`, `conflict_set_service.py`, or `lineage_assembly_service.py` — none
  of Milestone D's hierarchy mechanics touch the local-mode pipeline at all.
- A CPM resolution-parameter sweep, a heterogeneity-based recursion trigger, an unbounded recursion
  depth, or finest-level-only representative computation — all explicitly foreclosed by constraint
  25 itself (§1 above).
- Backfilling `parent_community_id` for any already-existing `communities` row — the next triggered
  recompute (constraint 18's existing write-triggered-plus-cooldown mechanism, unchanged) rebuilds
  every row from scratch anyway (constraint 21's existing full-wipe-and-rebuild contract), so no
  migration/backfill script is needed or in scope.
- Any change to `COMMUNITY_DETECTION_TRIGGER_COOLDOWN_S`, the write-trigger mechanism itself
  (`trigger_community_detection` and its two runner functions), or the coordinator/no-coordinator
  branching — all unchanged by this spec; recursion happens entirely inside the existing
  `recompute_communities` call, on the existing trigger cadence.
- Renumbering or otherwise touching any existing test scenario in either edited test file — both
  files are append-only under this spec's scope (§0).

## 9. Acceptance

```bash
cd /home/zbalint/workspace/SALTMDB-context-retrieval-d1
uv run pytest tests/test_community_detection_service.py tests/test_orphan_community_service.py -v
uv run pytest -q
```

Both commands must exit 0. The targeted run must show at least scenarios 29-34 present and passing
in `test_community_detection_service.py` and scenario 15 present and passing in
`test_orphan_community_service.py`, with every pre-existing scenario in both files still passing
unmodified (this spec's scope is append-only for both test files, §0/§8). The full suite must show
zero regressions elsewhere. Additionally:

```bash
rg -n "parent_community_id" src/saltmdb/db/schema.py src/saltmdb/domain/services/community_detection_service.py src/saltmdb/domain/services/orphan_community_service.py
```

must show the column defined once (schema.py), threaded through the INSERT in
`community_detection_service.py`, and referenced only inside `fetch_leaf_community_centroids`'s own
query — never re-implemented as a second, divergent leaf-filter query anywhere else in the tree.

## Amendment 1 — §4.2 `_process_partition_group` needs its own local `numpy` import (OMP BLOCKED, adjudicated)

### Contradiction (as reported by OMP, SALTMDB event `dacac008-a29e-4525-a4c3-a4af774c7436`)

§4.2 prescribes a new module-level function `_process_partition_group` whose body calls
`centroid_vec.astype(np.float32)` at runtime, inside its `if centroid_vec is not None:` block. §4.2's
own explanatory paragraph immediately following that code block claimed this was already covered by
`recompute_communities`'s existing `import numpy as np` (current line 159). **That claim is false**: a
Python `import` statement executed inside one function body only binds the imported name in *that
function's own local scope* — it never creates a module-global binding. `_process_partition_group` is
a sibling top-level function, not a nested closure of `recompute_communities`, so it has no access to
`recompute_communities`'s local `np` binding at all. Re-reading the actual current file
(`community_detection_service.py`, all 353 lines, re-verified against the tree as part of this
amendment, not merely re-read from the earlier pre-lock pass) confirms every *other* numpy use in this
file already follows the opposite, correct pattern: `_compute_community_group` (line 24),
`_normalize_community_vector` (line 87), and `recompute_communities` itself (line 159) each do their
own **local, per-function** `import numpy as np` as the first statement that needs it. §4.2 was the
only place in this spec that used `np` without following that same established convention. This is not
an edge case: it is a guaranteed `NameError` the first time `_process_partition_group` processes any
partition group whose centroid is not `None` — i.e. on every non-trivial `recompute_communities` call
once implemented. OMP correctly stopped rather than guessing past it.

### Fix

This amendment supersedes §4.2's original code block and the paragraph immediately following it (the
one that began "`import numpy as np` already happens earlier in `recompute_communities`..."). Both are
replaced, in place, by the following:

1. **`_process_partition_group`'s body** gets its own local `import numpy as np` as its first
   statement, immediately after the docstring — mirroring the exact convention this file's three
   existing numpy-using functions already established, not a new pattern:

```python
def _process_partition_group(  # noqa: PLR0913
    group_indices: list[int],
    graph,
    node_ids: list[str],
    raw_vector: dict[str, "np.ndarray"],
    level: int,
    parent_community_id: str | None,
    now: str,
    communities_to_insert: list[tuple],
    membership_to_insert: list[tuple],
    embeddings_to_insert: list[tuple],
) -> None:
    """Insert one community row for this partition group at `level`, then either recurse into a
    fresh Leiden pass over its own induced subgraph (if oversized and under the depth cap) or emit
    leaf-level community_membership rows for its members. Every community row this function ever
    creates -- leaf or not -- gets its own representative/centroid via the existing,
    completely-unmodified _compute_community_group (constraint 25's own "independently at every
    level" requirement)."""
    import numpy as np

    sorted_group = sorted(group_indices)
    member_ids, representative_id, centroid_vec = _compute_community_group(
        sorted_group, graph, node_ids, raw_vector
    )
    member_count = len(member_ids)
    community_id = str(uuid.uuid4())
    communities_to_insert.append(
        (community_id, representative_id, member_count, level, now, parent_community_id)
    )
    if centroid_vec is not None:
        import sqlite_vec

        embeddings_to_insert.append(
            (
                community_id,
                sqlite_vec.serialize_float32(centroid_vec.astype(np.float32).tolist()),
            )
        )

    if member_count > COMMUNITY_HIERARCHY_SIZE_THRESHOLD and level < COMMUNITY_HIERARCHY_MAX_DEPTH:
        import leidenalg

        child_graph = graph.subgraph(sorted_group)
        child_node_ids = [node_ids[i] for i in sorted_group]
        child_partition = leidenalg.find_partition(child_graph, leidenalg.ModularityVertexPartition)
        for child_group in child_partition:
            _process_partition_group(
                list(child_group),
                child_graph,
                child_node_ids,
                raw_vector,
                level + 1,
                community_id,
                now,
                communities_to_insert,
                membership_to_insert,
                embeddings_to_insert,
            )
    else:
        for entity_id in member_ids:
            membership_to_insert.append((entity_id, community_id, level))
```

   (Identical to the original §4.2 code block except for the added `import numpy as np` line and the
   blank line after it, immediately following the docstring.)

2. **The explanatory paragraph immediately after the code block is replaced** with:

   `_process_partition_group` imports `numpy` locally, as its own first statement, exactly like this
   file's other three numpy-using functions (`_compute_community_group`, `_normalize_community_vector`,
   `recompute_communities` itself) — never at module scope. A local import inside `recompute_communities`
   does not bind `np` for a separate top-level function; `_process_partition_group` needs, and now has,
   its own. `_process_partition_group`'s type hint on `raw_vector` still uses a string-quoted
   `"np.ndarray"` annotation, for the same reason every other `np`-typed signature in this file does:
   the annotation is evaluated only if something calls `typing.get_type_hints()` on it (nothing in this
   codebase does), so the string form never actually requires `np` to be bound at *def* time — only the
   function *body*'s runtime `np.float32` use requires the local import above.

No other section of this spec is affected: `fetch_leaf_community_centroids` (§4.3) and
`recompute_communities`'s own edits (§4.4) never reference `np` directly and are unchanged by this
amendment.

### Scope

No change to §0's file-edit scope. This amendment stays entirely inside the same file
(`community_detection_service.py`) and the same function §4.2 already named in scope.

### Independent audit of the rest of the spec (no other contradictions found)

While adjudicating this block, the rest of the spec was re-verified against the current tree — not
just re-read from the original drafting pass:

- Every cited line number was checked against the actual current file: §2 (`config.py:249/251`), §3
  (`schema.py:854/865`), §4.1 (`community_detection_service.py:9`), §5
  (`orphan_community_service.py:70-72`), and §7 (`test_orphan_community_service.py`, whose scenarios
  end at 14, so a new scenario 15 is correctly the next number) — all match exactly.
- §4.2's worked example (an 82-member level-0 community → `[30, 25, 15, 12]` at level 1 → `[22, 8]`
  from the 30-member child at level 2) was walked through the corrected code above and remains
  internally consistent with §1's locked design decisions 2-5 and with the corrected function's own
  `member_count > threshold and level < max_depth` recursion condition.
- Parameter order was checked positionally, not just by name, between every call site and the
  corrected function's own signature: the top-level call in §4.4 and the recursive self-call inside
  §4.2 both match `_process_partition_group`'s ten-parameter order exactly.
- No other bug, missing import, or internal contradiction was found anywhere else in the spec.
