# SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT

## 0. Status

**LOCKED**

**Depends on** `SPEC-CONTEXT-RETRIEVAL-C-COMMUNITY-DETECTION.md` being implemented, reviewed, and
merged into `context-aware-search` first — this spec reads the `community_membership` and
`community_embeddings` tables Milestone C creates and populates. **Do not branch this spec's
worktree until that merge has happened**; branching early would hand OMP a tree with no
`community_membership`/`community_embeddings` tables to query against.

**Scope**: may edit `src/saltmdb/config.py` (add exactly one new constant,
`COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`, and one new constant,
`CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP`, see §2); may edit
`src/saltmdb/domain/services/context_budget_service.py` (extend `pack_context_budget`'s existing
signature with one new optional parameter, `orphan_community_entity_ids`, and its existing return
shape with one new field, `orphan_community_reserve_tokens_used` — no other change, see §4); may
edit `src/saltmdb/domain/services/retrieve_context_service.py` (wire the new orphan-assignment step
into `assemble_retrieve_context`'s existing pipeline, see §5); may create
`src/saltmdb/domain/services/orphan_community_service.py` (new file, see §3) and
`tests/test_orphan_community_service.py` (new file, see §6). Does not touch:
`community_detection_service.py`, `schema.py`, `vector_schema.py` (this spec only *reads*
Milestone C's tables — it creates and writes nothing new to them),
`context_expansion_service.py`, `conflict_set_service.py`, `lineage_assembly_service.py` (their own
existing logic and contracts are unchanged; this spec's new step slots between
`assemble_conflict_sets` and `pack_context_budget` in the orchestrator's existing call sequence,
touching neither of those two functions' own internals), and any MCP/dispatch surface — Milestone
C.5, like Milestone C, introduces no new tool and no new `retrieve_context` parameter (constraint
17 is scoped to Milestone C's own infrastructure surface, but nothing in constraint 16 asks for a
new parameter either — the only observable surface change is a new `inclusion` value and new
`metadata` sub-fields on the tool's already-existing output).

**Pre-lock gate completed against the current tree**: `assemble_retrieve_context`,
`pack_context_budget`, and `assemble_conflict_sets` were re-read in full before drafting §§3-5.
Grepped `orphan_community`, `find_orphan_community_matches`, `orphan_community_service`,
`COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`, `CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP` across the tree:
zero prior occurrences, confirming none collide with existing code or stale test literals.

## 1. Why

This is Milestone C.5 ("orphan-to-community assignment") of the
`wayfinder:saltmdb:graph-aware-context-retrieval` roadmap — split out from Milestone C proper
(wayfinder ticket "Milestone C.5 split," event `be2595d8`) because pure Leiden clustering over
explicit edges cannot, by construction, help a **zero-edge** memory (the exact failure mode,
`92a3e17d`, that drove Milestone C's own greenlight in the first place — a same-topic memory with
no explicit graph edge to its topic-mates, only recoverable today by plain FTS/vector relevance
luck). This spec implements standing constraint 16 (ticket "Milestone C.5 orphan-to-community
assignment mechanics," event `1c15e9ac`) in full, plus half of constraint 23 (spec-lock-before-
calibration ordering, mirroring the sibling Milestone C spec's own treatment of that same
constraint).

**Locked upstream design decisions this spec implements as-is** (constraint 16's own five-part
design, restated only to the depth needed to implement):

1. **Compute timing: live, at query time, never persisted.** A zero-edge primary hit's
   `entity_embeddings` row is cosine-compared against every current `community_embeddings` row
   inline, during `assemble_retrieve_context`. No new table, no caching, no background job. Before
   Milestone C has ever run once (an empty `community_embeddings` table — a fresh install, or one
   where the trigger hasn't fired yet), this mechanism silently no-ops: zero communities to compare
   against means zero orphan-community matches, never an error.
2. **Budget/fan-out: its own additive reserve, mirroring constraint 12 (G8)'s conflict-set-reserve
   precedent exactly** — never drawn from the ordinary expansion pool (`CONTEXT_EXPANSION_TOP_K_
   RELATIONSHIPS`'s cap) or the ordinary token budget (`CONTEXT_BUDGET_DEFAULT_TOKENS`'s pack). An
   orphan-community match is always included once admitted under its own cap (§3), regardless of
   how full the primary/expansion budget already is — exactly like a `conflict_only` member.
3. **Shape**: a new `inclusion: "orphan_community"` value; `retrieval_provenance:
   [{"reason": "orphan_community_assignment", "orphan_entity_id": <str>, "community_id": <str>,
   "similarity": <float>}]` (mirrors `conflict_only`'s own `{reason, conflict_set_id}` provenance
   pattern — one dict, not a list of alternatives, since an orphan-community match is admitted via
   exactly one orphan anchor even if it was theoretically reachable from more than one, see §3 step
   7). The cap (`CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP`) is a **flat constant**, not scaled by the
   number of orphan hits or primary hits — orphan-community is a sparse, force-include mechanism
   like G7's contradicts reserve, not G4's proportional default-case fan-out. Truncation under the
   cap is **per-member**, not G7's whole-set-or-nothing: `orphan_community_truncated`/
   `orphan_community_dropped_count`.
4. **Ranking: embedding-similarity to the specific orphan itself, not to the community centroid.**
   Once an orphan hit's best-matching community is found (by centroid similarity, §3 step 5), that
   community's *other* members are ranked, for admission purposes, by their own cosine similarity
   to the orphan's own embedding — not by their similarity to the community centroid and not by
   within-community PageRank centrality (constraint 19's centrality measure is for *representative
   selection*, an entirely different job, at recompute time; this is a *retrieval-ranking* decision,
   at query time).
5. **Threshold: a new named `PLACEHOLDER` constant** (`COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`,
   §2) gating which community (if any) an orphan is assigned to at all — deferred to the Milestone
   C/C.5 benchmark run (constraint 22, ticket `787ebf0c`), which per that ticket's own locked
   calibration mechanic sweeps this threshold to maximize recall-lift without false-positive noise.
   **Per forward-note `e74d4355`'s own concern**, restated here exactly as in the sibling Milestone
   C spec: this spec's own acceptance bar (§7) is pure structural correctness against the
   constant's *symbol*, never a specific numeric expectation — every test referencing it imports
   `COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD` and asserts behavior relative to it (e.g. "a similarity
   engineered to be clearly above/below the threshold behaves as expected"), never hardcodes a bare
   float literal as if it were the locked value.

**Locked design decisions this spec makes that were left open upstream** (mirroring the sibling
Milestone C spec's own §1 precedent for this category):

1. **"Zero-edge orphan" is operationalized as "absent from `community_membership`."** Constraint 16
   never states the exact query; the scope-confirmation ticket (`3115a801`) locks "strict
   zero-edge only" as the *definition*, and Milestone C's own spec constructs
   `community_membership` to contain a row for exactly the set of entities with at least one
   currently-valid qualifying relation edge (see that spec's §1 upstream-decision 3). A primary
   hit with no `community_membership` row is therefore, by construction, a true zero-edge orphan —
   reusing this existing table avoids re-deriving Milestone C's own bitemporal edge-existence
   predicate a second time in a different module, and inherits Milestone C's own accepted
   staleness bound (a hit that gained an edge moments ago may still read as "orphan" until the next
   cooldown-gated recompute catches up — the same write-triggered-with-cooldown freshness tradeoff
   Milestone C already signed off on, not a new one this spec introduces).
2. **Expansion candidates are never orphan-hit anchors — only primary hits are.** Constraint 16's
   own wording is explicit ("a zero-edge **primary hit's** `entity_embeddings` row"), and this is
   also structurally forced: any entity `expand_context_candidates` (Milestone A slice A1) ever
   surfaces as an expansion candidate was, by that function's own contract, reached via a graph
   edge from a primary hit — it therefore has at least one edge and can never be a zero-edge
   orphan. This spec's public function accordingly takes only `primary_hits` as its anchor input,
   never `expansion_result`.
3. **A candidate community member already surfaced elsewhere in the same `retrieve_context` call
   (as `primary`, `expansion`, or `conflict_only`) is never re-admitted as `orphan_community`.**
   Mirrors `assemble_conflict_sets`'s own already-established convention of checking candidate ids
   against `primary_hit_ids`/`expansion_candidate_ids` before tagging inclusion — extended here to
   also exclude `conflict_only` ids, since by this point in the pipeline (§5) those are already
   known. This is required for `memories[]`'s own "one entry per distinct entity_id" invariant
   (A4's spec §1 decision 10 relies on exactly this kind of non-overlap guarantee holding by
   construction).
4. **If the same community member is reachable from two different orphan-hit anchors in the same
   call, it is admitted at most once, credited to whichever anchor gives it the higher similarity
   score.** Two different orphan primary hits can legitimately both resolve to the same
   best-matching community (constraint 16 does not say communities are exclusively claimed by one
   orphan). Since ranking is "similarity to the specific orphan" (a per-anchor-pair score, not a
   single global property of the member), the same member can have two different scores depending
   on which orphan is asking — this spec keeps only the higher-scoring `(member, anchor)` pairing
   and drops the other, so the member's single `retrieval_provenance` entry (§1 upstream-decision
   3) always names its best (not merely its first-found) anchor.
5. **Global cap, global ranking — not per-orphan-hit.** Constraint 16 explicitly frames the cap as
   flat, "not G4's per-primary-hit scaling," which this spec reads as applying to the *combined*
   candidate pool across every orphan hit in the call, not `cap` slots per orphan hit (the latter
   reading would let a single `retrieve_context` call with many orphan hits blow past any sane
   token/context budget). All admitted-or-dropped candidates across every orphan anchor are pooled
   into one ranked list before the cap is applied (§3 step 8).

## 2. `src/saltmdb/config.py`

Insert immediately after the existing `CONTEXT_EXPANSION_CONTRADICTS_CAP` block (current lines
287-296), before the `# Rework Phase 6` comment:

```python
# Milestone C.5 (wayfinder ticket "orphan-to-community assignment mechanics," standing constraint
# 16, memory 1c15e9ac) -- retrieve_context's orphan-community reserve: bounds how many net-new
# (orphan_community) entities a call may pull in across ALL zero-edge orphan primary hits combined,
# additive to (never drawn from) CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's fan-out cap and
# CONTEXT_EXPANSION_CONTRADICTS_CAP's conflict reserve. A FLAT constant, mirroring
# CONTEXT_EXPANSION_CONTRADICTS_CAP's own shape exactly (not scaled by orphan-hit or primary-hit
# count, unlike CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS) -- orphan-community is a sparse
# force-include mechanism, not G4's always-on default case. Fixed now, NOT a Milestone-C/C.5-
# benchmark placeholder (unlike COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD below) -- mirrors
# LINEAGE_HISTORICAL_CAP's own "low-stakes and reversible, do not defer" precedent: getting this
# number wrong costs at most a few extra/fewer context entries, recoverable by re-tuning without
# calibration evidence. Seeded at the same value as CONTEXT_EXPANSION_CONTRADICTS_CAP's own current
# value, since constraint 16 explicitly mirrors that constant's shape.
CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP = 5

# Milestone C.5 (wayfinder ticket "orphan-to-community assignment mechanics," standing constraint
# 16, memory 1c15e9ac) -- the minimum cosine similarity between a zero-edge orphan primary hit's
# own entity_embeddings vector and a community's PageRank-weighted centroid (community_embeddings)
# required to assign that orphan to that community at all. Below this threshold, the orphan gets no
# orphan_community assignment for this call, full stop -- never force-assigned to its
# nearest-however-distant community. PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus
# -- seeded at the same order of magnitude as this codebase's other cosine-similarity thresholds
# gating a comparably-scoped "does this specific thing belong with that specific thing" judgment
# (RELATION_GATE_MIN_SIMILARITY_THRESHOLD=0.6505, COHESION_MIN_PAIRWISE_THRESHOLD=0.6547) -- not
# derived from any benchmark of its own. Recalibrated by the Milestone C/C.5 benchmark run
# (wayfinder ticket 787ebf0c) via a sweep maximizing that ticket's own recall-lift metric without
# false-positive noise. Do not remove the placeholder framing when tuning this; replace this
# comment with the benchmark citation once a real value is locked.
COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD = 0.65
```

## 3. New file: `src/saltmdb/domain/services/orphan_community_service.py`

A new domain-service module, sibling to `conflict_set_service.py`, following the same conventions:
module-level `logger = logging.getLogger(__name__)`, the same `db_connection=None, db_path: str |
None = None` open-or-reuse-connection pattern.

### 3.1 `find_orphan_community_matches` — the slice's one public function

```python
def find_orphan_community_matches(
    primary_hits: list[dict[str, Any]],
    excluded_entity_ids: set[str],
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

**Input contract**: `primary_hits` is the same `list[{"id": str, "score": float}]` every other
context-retrieval service in this pipeline receives — only `"id"` is read here. `excluded_entity_ids`
is the caller-computed union of every entity id already surfaced elsewhere in this same
`retrieve_context` call (primary hit ids, expansion candidate ids, and `conflict_only` member ids —
§5 shows exactly how the caller builds this) — a candidate community member in this set is never
admitted (§1 decision 3). An empty `primary_hits` list returns the all-empty shape below without
opening a connection.

**Algorithm** (implement exactly this sequence):

1. If `primary_hits` is empty, return the zero-result shape from step 9 immediately (no connection
   opened, mirroring `assemble_conflict_sets`'s own empty-input early return).
2. Open one connection (`db_connection` if given, else `get_connection(db_path or get_db_path())`,
   closing it at the end only if this function opened it).
3. Find which primary hits are true zero-edge orphans (§1 decision 1):
   ```python
   primary_hit_ids = [hit["id"] for hit in primary_hits]
   placeholders = ",".join("?" for _ in primary_hit_ids)
   member_rows = conn.execute(
       f"SELECT entity_id FROM community_membership WHERE entity_id IN ({placeholders})",
       primary_hit_ids,
   ).fetchall()
   has_membership = {row[0] for row in member_rows}
   orphan_ids = [hit_id for hit_id in primary_hit_ids if hit_id not in has_membership]
   ```
   If `orphan_ids` is empty, return the zero-result shape (step 9) — every primary hit already
   belongs to a community (or there are no communities at all yet, handled identically here: no
   orphans to check means nothing to do either way).
4. Fetch every current community centroid in one query. If this returns zero rows (Milestone C
   hasn't run yet, or produced no communities with embeddings), return the zero-result shape (§1
   upstream-decision 1's explicit silent-no-op case):
   ```python
   import numpy as np
   from saltmdb.db.vector_schema import try_load_vector_extension

   if not try_load_vector_extension(conn):
       logger.warning(
           "find_orphan_community_matches: sqlite-vec extension unavailable, skipping orphan "
           "community assignment for this call"
       )
       return <zero-result shape>
   centroid_rows = conn.execute(
       "SELECT community_id, embedding FROM community_embeddings"
   ).fetchall()
   if not centroid_rows:
       return <zero-result shape>
   centroids = {
       community_id: _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
       for community_id, blob in centroid_rows
   }
   ```
   `_normalize` is the same L2-normalize-or-return-as-is-if-zero-vector helper defined locally in
   this module (a two-line function; not imported from `cohesion_service.py`'s own `_centroid`
   internals, which computes a different thing — an unweighted multi-vector mean — not a
   single-vector normalization; there is nothing to reuse there beyond the same well-known
   normalization formula, which is too small and too different in context to justify importing a
   private helper across modules).
5. Fetch `entity_embeddings` for every orphan id, in one query; an orphan with no embedding row yet
   is dropped silently (defensive — mirrors every other embedding-touching service in this
   pipeline's graceful-degradation posture):
   ```python
   placeholders = ",".join("?" for _ in orphan_ids)
   orphan_rows = conn.execute(
       f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
       orphan_ids,
   ).fetchall()
   orphan_vectors = {
       entity_id: _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
       for entity_id, blob in orphan_rows
   }
   ```
6. For each orphan with a usable vector, find its single best-matching community by cosine
   similarity to the centroid, gated by the threshold:
   ```python
   from saltmdb.config import COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD

   orphan_assignment: dict[str, str] = {}  # orphan_entity_id -> community_id
   for orphan_id, vec in orphan_vectors.items():
       best_community_id, best_sim = None, -1.0
       for community_id, centroid in centroids.items():
           sim = float(np.dot(vec, centroid))
           if sim > best_sim:
               best_community_id, best_sim = community_id, sim
       if best_community_id is not None and best_sim >= COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD:
           orphan_assignment[orphan_id] = best_community_id
   if not orphan_assignment:
       return <zero-result shape>
   ```
   A tie between two communities at the same best similarity score is broken by lowest
   `community_id` (`min()` over the tied ids) — a low-stakes, purely-cosmetic determinism choice
   with no locked precedent to follow, since `community_id` is not stable across recomputes anyway
   (Milestone C constraint 21) and no test should depend on which of two literally-tied communities
   wins.
7. For each assigned `(orphan_id, community_id)` pair, fetch that community's other members and
   rank them by similarity to *that orphan's own* vector (§1 upstream-decision 4 — a per-anchor
   score, not a shared community-level score):
   ```python
   candidate_rows: list[tuple[str, str, str, float]] = []  # (member_id, orphan_id, community_id, sim)
   for orphan_id, community_id in orphan_assignment.items():
       member_rows = conn.execute(
           "SELECT entity_id FROM community_membership WHERE community_id = ?",
           (community_id,),
       ).fetchall()
       member_ids = [
           row[0] for row in member_rows
           if row[0] != orphan_id and row[0] not in excluded_entity_ids
       ]
       if not member_ids:
           continue
       placeholders = ",".join("?" for _ in member_ids)
       embedding_rows = conn.execute(
           f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({placeholders})",
           member_ids,
       ).fetchall()
       orphan_vec = orphan_vectors[orphan_id]
       for member_id, blob in embedding_rows:
           member_vec = _normalize(np.frombuffer(blob, dtype=np.float32).astype(np.float64))
           sim = float(np.dot(orphan_vec, member_vec))
           candidate_rows.append((member_id, orphan_id, community_id, sim))
   ```
   A community member with no `entity_embeddings` row of its own is silently absent from
   `candidate_rows` (same graceful-degradation posture as step 5) — it simply can never be admitted
   this call, not a crash.
8. **Dedupe by member id, keeping only the best-scoring `(orphan, community)` pairing per member**
   (§1 upstream-decision 4), then rank and cap globally (§1 upstream-decision 5), not per orphan:
   ```python
   from saltmdb.config import CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP

   best_by_member: dict[str, tuple[str, str, float]] = {}  # member_id -> (orphan_id, community_id, sim)
   for member_id, orphan_id, community_id, sim in candidate_rows:
       current = best_by_member.get(member_id)
       if current is None or sim > current[2]:
           best_by_member[member_id] = (orphan_id, community_id, sim)

   ranked = sorted(
       best_by_member.items(),
       key=lambda item: (-item[1][2], item[0]),  # similarity desc, then entity_id asc
   )
   eligible_count = len(ranked)
   admitted = ranked[:CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP]
   dropped_count = max(0, eligible_count - CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP)
   ```
9. Fetch `title` for every admitted member in one batched query (mirrors A2's own batched
   entity-materialization pattern; `memory_type` is deliberately NOT fetched here — the caller,
   `assemble_retrieve_context`, already has its own batched `memory_type_by_id` query covering
   every non-primary inclusion, extended in §5 to include these ids too, rather than duplicating
   that query here).

**Output contract** (exact keys, exact types):

```python
{
    "orphan_community_matches": [
        {
            "entity_id": str,
            "title": str,
            "similarity": float,
            "orphan_entity_id": str,
            "community_id": str,
        },
        ...  # ranked highest-similarity first, already capped, per step 8
    ],
    "orphan_community_cap": {
        "cap": int,           # CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP
        "eligible_count": int,
        "truncated": bool,
        "dropped_count": int,
    },
}
```

**The zero-result shape**, returned at every early-exit point above:
```python
{
    "orphan_community_matches": [],
    "orphan_community_cap": {
        "cap": CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP,
        "eligible_count": 0,
        "truncated": False,
        "dropped_count": 0,
    },
}
```
`cap` reflects the real configured constant even in the zero-result shape (mirrors
`assemble_conflict_sets`'s own identical convention for `contradicts_cap`), never a bare `0`.

This function raises nothing itself for a resolvable/valid input; a failed `sqlite-vec` extension
load degrades to the zero-result shape (step 4) rather than raising, matching this codebase's
existing convention (`get_fresh_entity_centroids`'s own extension-load-failure handling) for this
exact, expected, non-catastrophic failure mode.

## 4. `src/saltmdb/domain/services/context_budget_service.py`

Extend `pack_context_budget`'s existing signature with one new optional parameter (Coding
Standards rule 4 — extend rather than duplicate token-accounting logic that already exists in this
function), defaulting to `None` so every existing caller (including Milestone A's own already-shipped
tests) is unaffected:

```python
def pack_context_budget(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    conflict_sets_result: dict[str, Any],
    *,
    budget_tokens: int | None = None,
    orphan_community_entity_ids: set[str] | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

Changes to the function body, precisely scoped (every other line of the existing algorithm is
unchanged — see the sibling Milestone A spec's own algorithm for the parts not mentioned here):

1. In the id-set-building step (existing step 2), add:
   ```python
   orphan_community_ids: set[str] = orphan_community_entity_ids or set()
   ```
   and include it in the existing early-return-if-all-empty check and in `all_ids`'s union:
   ```python
   all_ids = set(primary_ids) | set(expansion_ids) | conflict_only_ids | orphan_community_ids
   ```
2. In the token-counting step (existing step 5), no change — `orphan_community_ids`'s members are
   already covered by iterating `all_ids`, which now includes them.
3. After the existing `conflict_reserve_tokens_used` computation (existing step 7), add:
   ```python
   orphan_community_reserve_tokens_used = sum(
       token_counts[entity_id] for entity_id in orphan_community_ids
   )
   ```
4. In the returned `budget` dict (existing step 8), add one new field:
   ```python
   "orphan_community_reserve_tokens_used": orphan_community_reserve_tokens_used,
   ```
   alongside the existing `conflict_reserve_tokens_used` field — both are additive reserves, never
   checked against `effective_budget`, per §1 upstream-decision 2 (mirroring constraint 12's own
   already-locked precedent this function already implements for the conflict reserve).
5. The step-2 early-return zero-result shape (all input sets empty) gains the same new field, set
   to `0`, for shape consistency with the non-empty-input return.

No other line of `pack_context_budget` changes. This is a strictly additive extension: any existing
caller that omits `orphan_community_entity_ids` gets `orphan_community_ids = set()`, an unchanged
`all_ids`, and `orphan_community_reserve_tokens_used == 0` — byte-for-byte the same behavior as
before this spec, for every call site that doesn't yet know this parameter exists.

## 5. `src/saltmdb/domain/services/retrieve_context_service.py`

Wire the new step into `assemble_retrieve_context`'s existing pipeline, between the existing
`assemble_conflict_sets` call and the existing `pack_context_budget` call (current lines 98-110):

```python
        conflict_sets_result = assemble_conflict_sets(
            expansion_result,
            primary_hits,
            point_in_time=pit,
            db_connection=conn,
        )

        expansion_candidate_ids = {
            candidate["entity_id"] for candidate in expansion_result["expansion_candidates"]
        }
        conflict_only_ids = {
            member["id"]
            for conflict_set in conflict_sets_result["conflict_sets"]
            for member in conflict_set["members"]
            if member["inclusion"] == "conflict_only"
        }
        excluded_for_orphan_lookup = (
            set(primary_hit_ids_set) | expansion_candidate_ids | conflict_only_ids
        )
        orphan_result = find_orphan_community_matches(
            primary_hits,
            excluded_for_orphan_lookup,
            db_connection=conn,
        )
        orphan_community_entity_ids = {
            match["entity_id"] for match in orphan_result["orphan_community_matches"]
        }

        budget_result = pack_context_budget(
            expansion_result,
            primary_hits,
            conflict_sets_result,
            budget_tokens=budget_tokens,
            orphan_community_entity_ids=orphan_community_entity_ids,
            db_connection=conn,
        )
```

`primary_hit_ids_set` is a new small local (`{hit["id"] for hit in primary_hits}`) introduced
alongside the existing `primary_hits`/`primary_meta`/`original_rank` locals built earlier in the
function (current lines 77-91) — a one-line addition there, not a new pipeline stage.

Add the new import alongside the existing five (current lines 10-17):
```python
from saltmdb.domain.services.orphan_community_service import find_orphan_community_matches
```

**Final assembly changes** (the `memories`-building loop, current lines 170-219): after the
existing `conflict_only_entity_ids` loop (current lines 204-219), add a fourth loop:

```python
        orphan_title_by_id = {
            match["entity_id"]: match["title"]
            for match in orphan_result["orphan_community_matches"]
        }
        orphan_provenance_by_id = {
            match["entity_id"]: {
                "reason": "orphan_community_assignment",
                "orphan_entity_id": match["orphan_entity_id"],
                "community_id": match["community_id"],
                "similarity": match["similarity"],
            }
            for match in orphan_result["orphan_community_matches"]
        }
        for entity_id in orphan_community_entity_ids:
            memories.append(
                {
                    "entity_id": entity_id,
                    "title": orphan_title_by_id[entity_id],
                    "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                    "inclusion": "orphan_community",
                    "retrieval_provenance": [orphan_provenance_by_id[entity_id]],
                }
            )
```

Extend the existing `needs_memory_type` set (current lines 151-153) to include these ids too, so
the existing single batched `memory_type` query (current lines 154-161) covers them without a
second query:
```python
        needs_memory_type = (
            set(final_expansion_ids)
            | set(budget_result["conflict_only_entity_ids"])
            | orphan_community_entity_ids
        )
```

**Metadata changes** (current lines 234-252): extend the existing `fan_out`/`budget` sub-dicts,
each with one new nested field, mirroring the existing `conflict_reserve` nesting exactly:

```python
        metadata = {
            "strategy": "local",
            "fan_out": {
                **expansion_result["fan_out"],
                "conflict_reserve": conflict_sets_result["contradicts_cap"],
                "orphan_community_reserve": orphan_result["orphan_community_cap"],
            },
            "budget": {
                "unit": budget_result["budget"]["unit"],
                "limit": budget_result["budget"]["limit"],
                "used": budget_result["budget"]["used"] + recovered_tokens_used,
                "primary_truncated": len(true_primary_dropped) > 0,
                "primary_dropped_count": len(true_primary_dropped),
                "expansion_truncated": len(true_expansion_dropped) > 0,
                "expansion_dropped_count": len(true_expansion_dropped),
                "conflict_reserve_tokens_used": (
                    budget_result["budget"]["conflict_reserve_tokens_used"] + recovered_tokens_used
                ),
                "orphan_community_reserve_tokens_used": budget_result["budget"][
                    "orphan_community_reserve_tokens_used"
                ],
            },
        }
```

No other line of `assemble_retrieve_context` changes. `surfaced_ids`/`lineage_result`'s existing
pruning step (current lines 220-223) needs no change — it already prunes `lineage_result` to
whatever ends up in `memories`, and `memories` now includes the orphan-community entries by
construction of the loop above running before that pruning step executes (confirm placement: the
new loop must be inserted before, not after, the existing `surfaced_ids = {memory["entity_id"] for
memory in memories}` line, exactly where it's written above — between the `conflict_only`
loop and the `edges = expansion_result["in_network_edges"]` line).

## 6. `tests/test_orphan_community_service.py` (new file)

Follow `tests/test_conflict_set_service.py`'s exact fixture conventions: `init_db` + real SQLite,
the `_memory_id`/`_axis_vector` helper pair (copy, do not import across files), `store_memory` for
fixture entities, `store_relation` to build community-membership fixtures (or, more directly, raw
`INSERT INTO communities/community_membership/community_embeddings` — since this module only ever
*reads* those tables, direct fixture inserts are simpler and more targeted than running a real
`recompute_communities` pass for every test; use whichever is clearer per scenario, but prefer
direct inserts for tests that only need a specific membership/centroid shape, reserving a real
`recompute_communities` call for scenarios that specifically want to verify true end-to-end
integration). Direct `INSERT INTO entity_embeddings` via `sqlite_vec.serialize_float32`, same
convention as the sibling Milestone C spec's own test file.

Required test scenarios (write test-first, red before green, per this workspace's `tdd` skill and
Coding Standards rule 19):

1. **Empty `primary_hits`**: `find_orphan_community_matches([], set())` returns the zero-result
   shape without opening a connection.
2. **No orphans among primary hits**: every primary hit already has a `community_membership` row —
   assert the zero-result shape (`orphan_community_matches == []`), and that no
   `entity_embeddings`/`community_embeddings` queries were even needed to reach that conclusion (a
   query-count assertion is optional; the behavioral result is the required assertion).
3. **No communities exist yet** (empty `community_embeddings`): a genuine orphan primary hit, but
   Milestone C has never run — assert the zero-result shape, proving §1 upstream-decision 1's
   "silently no-ops before Milestone C ships" contract.
4. **Orphan assigned when similarity clears the threshold**: an orphan hit whose embedding is
   engineered (via `_axis_vector`-style controlled vectors) to be clearly above
   `COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD` (imported, never hardcoded — e.g. construct the orphan's
   vector as a small perturbation of the community centroid's own known vector, verified to exceed
   the real threshold) against one community's centroid — assert it gets assigned, and that
   community's other members become admitted candidates.
5. **Orphan NOT assigned when best similarity is below the threshold**: same shape as scenario 4
   but with the orphan's vector engineered to be clearly below the threshold (e.g. an orthogonal
   `_axis_vector`) — assert `orphan_community_matches == []` for that orphan, even though a
   community with a nonzero (but sub-threshold) similarity technically existed.
6. **Ranking is similarity-to-the-orphan, not similarity-to-the-centroid**: a community with two
   members, one closer to the community centroid but farther from the specific orphan's own vector,
   the other farther from the centroid but closer to the orphan — assert the latter ranks first in
   `orphan_community_matches` (proves §1 upstream-decision 4 / constraint-16-decision-4 is actually
   implemented, not silently defaulted to centroid-similarity).
7. **Excluded ids are never admitted**: a community member that would otherwise rank first is
   passed in `excluded_entity_ids` — assert it never appears in `orphan_community_matches`, and
   that a lower-ranked, non-excluded member is admitted in its place (not merely skipped and
   leaving a gap).
8. **Global cap across multiple orphan hits, not per-orphan**: construct two distinct orphan primary
   hits, each mapping to a different community with enough candidate members that their *combined*
   candidate pool exceeds `CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP` (imported, never hardcoded) even
   though *neither individually* would — assert the admitted set is capped at the constant's value
   globally, and `dropped_count`/`truncated` reflect the combined pool, not either orphan's own
   sub-pool.
9. **Same member reachable from two orphan anchors, deduped to the higher-scoring pairing**:
   construct two orphan hits that both resolve to the same community, with one shared candidate
   member whose similarity differs measurably against each orphan's own vector — assert it appears
   exactly once in `orphan_community_matches`, with `orphan_entity_id` naming whichever anchor gave
   it the higher score, not the other.
10. **A member with no `entity_embeddings` row is silently skipped, not fatal**: a community member
    that would otherwise be a candidate has no embedding row at all — assert the call completes
    without error and that member never appears in `orphan_community_matches`.
11. **Tie-break at the community-selection step is deterministic**: an orphan equidistant (exactly
    tied cosine similarity, e.g. via identical `_axis_vector`-derived centroids) between two
    communities — assert the lower `community_id` wins, and this is stable across repeated calls
    against the same fixture.
12. **`pack_context_budget`'s new parameter is additive and backward-compatible**: call
    `pack_context_budget` exactly as every existing Milestone-A test already does, omitting
    `orphan_community_entity_ids` entirely — assert `budget["orphan_community_reserve_tokens_used"]
    == 0` and every other field is byte-identical to what the existing (pre-this-spec) test suite
    already asserts (re-run — do not rewrite — a representative sample of `test_context_budget_service
    .py`'s own existing scenarios to confirm zero regression, per §7's acceptance command).
13. **`pack_context_budget` correctly reserves orphan-community tokens outside the ordinary
    budget**: pass a real `orphan_community_entity_ids` set with large-content members and a
    `budget_tokens` too small to admit even one primary/expansion candidate — assert those ids'
    tokens are reflected in `orphan_community_reserve_tokens_used` and never counted against
    `budget["used"]`, mirroring the existing `conflict_reserve_tokens_used` scenario in
    `test_context_budget_service.py`.
14. **End-to-end integration via `assemble_retrieve_context`**: a real corpus with one well-linked
    cluster (via `store_relation`, then a real `recompute_communities` call to populate the tables
    for real — this is the one scenario in this file that should exercise Milestone C's own
    engine directly rather than raw fixture inserts, to prove the two milestones' contracts
    actually compose) and one genuinely zero-edge memory whose embedding is engineered to match
    that cluster's centroid — assert the final `retrieve_context` result includes an
    `inclusion: "orphan_community"` entry for at least one cluster member, with the exact
    `retrieval_provenance` shape locked in §1 upstream-decision 3, and that
    `metadata.fan_out.orphan_community_reserve`/`metadata.budget.orphan_community_reserve_tokens_used`
    are both populated and consistent with the admitted entry.

## 7. Out of scope

- Anything in `community_detection_service.py`, `schema.py`, or `vector_schema.py` — this spec only
  reads Milestone C's already-populated tables, never writes to them.
- Any new MCP tool, `retrieve_context` parameter, or `metadata.strategy` value — the only observable
  surface change is a new `inclusion` value and new `metadata` sub-fields on the tool's existing
  output shape.
- Recalibrating `COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`'s numeric value — deferred to the Milestone
  C/C.5 benchmark run (constraint 22), not this implementation's concern. This spec's acceptance
  bar (§8) never depends on that numeric value being "correct" in any calibrated sense.
- Widening the strict-zero-edge orphan definition to include low-but-nonzero-degree entities (a
  1-2-edge case) — constraint 22 explicitly names this as a future validation-only probe in the
  eventual benchmark run, not a change to this spec's own scope boundary (`3115a801`).
- Persisting orphan-community assignments anywhere, or any freshness/caching mechanism for them —
  foreclosed by §1 upstream-decision 1; every call recomputes from scratch.
- Ranking admitted members by community centrality (PageRank) instead of similarity-to-the-orphan —
  explicitly foreclosed by §1 upstream-decision 4.
- Any change to `expand_context_candidates`, `assemble_conflict_sets`, or `assemble_lineage`'s own
  internals — this spec's new step consumes their outputs as data only (mirrors every existing
  Milestone A slice's own convention).

## 8. Acceptance

```bash
PYTHONPATH=src uv run pytest tests/test_orphan_community_service.py -v
```
must exit 0, and every scenario in §6 must correspond to at least one passing test (a reviewer
checks this by name, not just by exit code).

```bash
PYTHONPATH=src uv run pytest tests/test_context_budget_service.py tests/test_retrieve_context_service.py -v
```
(or whatever the actual existing Milestone-A integration test file for `assemble_retrieve_context`
is named in the tree at implementation time — confirm the real filename first) must exit 0, proving
zero regression to `pack_context_budget`'s existing contract and `assemble_retrieve_context`'s
existing behavior for calls that predate this spec.

```bash
PYTHONPATH=src uv run pytest tests/ -q
```
must also exit 0 (no regression to the existing suite).

```bash
uv run ruff check src/saltmdb/domain/services/orphan_community_service.py \
  src/saltmdb/domain/services/context_budget_service.py \
  src/saltmdb/domain/services/retrieve_context_service.py \
  tests/test_orphan_community_service.py && \
uv run ruff format --check src/saltmdb/domain/services/orphan_community_service.py \
  src/saltmdb/domain/services/context_budget_service.py \
  src/saltmdb/domain/services/retrieve_context_service.py \
  tests/test_orphan_community_service.py && \
uv run mypy src/saltmdb/domain/services/orphan_community_service.py \
  src/saltmdb/domain/services/context_budget_service.py \
  src/saltmdb/domain/services/retrieve_context_service.py
```
must exit 0, matching this repo's documented lint/type gate (`CONTRIBUTING.md`).

**Explicit acceptance carve-out (per §1 upstream-decision 5 / forward-note `e74d4355`)**: this
spec's acceptance is pure structural/behavioral correctness against
`COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD`'s *symbol* — no test may hardcode its numeric value
(`0.65`), and no part of this acceptance bar depends on constraint 22's not-yet-determined
recall-lift threshold.
