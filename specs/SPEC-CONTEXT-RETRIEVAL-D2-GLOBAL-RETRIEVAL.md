# SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL

## 0. Status

**LOCKED**

**Depends on** `SPEC-CONTEXT-RETRIEVAL-D1-HIERARCHICAL-COMMUNITIES.md` being implemented, reviewed,
and merged into `context-aware-search` first — this spec calls D1's new
`fetch_leaf_community_centroids` helper and relies on `communities.parent_community_id` existing.
**Do not branch this spec's worktree until that merge has happened** — mirrors
`SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`'s own identical dependency note on Milestone C.

**Scope**: may edit `src/saltmdb/config.py` (add exactly three new constants,
`CONTEXT_GLOBAL_TOP_K_COMMUNITIES`, `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP`,
`CONTEXT_GLOBAL_MEMBER_POOL_CAP`, see §2); may create
`src/saltmdb/domain/services/community_retrieval_service.py` (new file, see §3); may edit
`src/saltmdb/domain/services/context_budget_service.py` (extend `pack_context_budget`'s existing
signature with two new optional parameters and its existing return shape with new fields — no
other change, see §4); may edit `src/saltmdb/domain/services/retrieve_context_service.py` (add a
new `strategy` parameter and a new global-mode branch to `assemble_retrieve_context`, see §5); may
edit `src/saltmdb/mcp/tools.py` (add a new `strategy` parameter to the `retrieve_context` tool and
extend its docstring, see §6); may edit `src/saltmdb/daemon/dispatch.py` (add one new
`_optional_strategy` helper, mirroring the existing `_optional_mode`, and thread `strategy` through
`_dispatch_retrieve_context`, see §7); may create
`tests/test_community_retrieval_service.py` (new file, see §8); may edit
`tests/test_context_budget_service.py`, `tests/test_retrieve_context_service.py`, and
`tests/test_retrieve_context_wiring.py` (new scenarios, plus a small number of *named* existing
whole-shape/whole-signature assertion sites that this spec's own additive parameter necessarily
changes — enumerated exactly in §9, no other line in any of these three files may change). **Does
not touch**: `community_detection_service.py`, `orphan_community_service.py`, `schema.py`,
`vector_schema.py` (this spec only *reads* D1's tables/helper — it creates and writes nothing new
to `communities`/`community_membership`/`community_embeddings`), `context_expansion_service.py`,
`conflict_set_service.py`, `lineage_assembly_service.py` (their own existing logic and local-mode
contracts are completely unchanged; this spec's global-mode branch never calls any of them), and
`daemon/protocol.py` (`retrieve_context` is already registered as a read-only tool; adding a
parameter to an already-registered tool changes nothing there).

**Pre-lock gate completed against the current tree**: `assemble_retrieve_context`,
`pack_context_budget`, `orphan_community_service.find_orphan_community_matches`, and
`embedding_service.embed_text` were all re-read in full before drafting §§3-5.
`mcp/tools.py`'s `retrieve_context` and `daemon/dispatch.py`'s `_optional_mode`/
`_dispatch_retrieve_context` were re-read in full before drafting §§6-7. Grepped
`CONTEXT_GLOBAL_TOP_K_COMMUNITIES`, `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP`,
`CONTEXT_GLOBAL_MEMBER_POOL_CAP`, `community_retrieval_service`, `seed_and_rank_communities`,
`_optional_strategy`, `community_representative`, `community_member` across the whole tree: zero
prior occurrences of the new symbol/constant/module names; `"community_member"`/
`"community_representative"` as bare strings are likewise unused (no stale-literal collision with
any existing `inclusion` value). **Three existing whole-shape/whole-signature test assertions were
traced and found to break under this spec's own additive parameter, exactly the class of collateral
breakage the pre-lock gate's step 2/3 exists to catch before OMP does** — see §9's own explicit
enumeration, mirroring `SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`'s own "Amendment 1" episode
for this exact failure shape, caught here before locking rather than after.

## 1. Why

This is the caller-facing retrieval half of Milestone D ("global/community retrieval") of the
`wayfinder:saltmdb:graph-aware-context-retrieval` roadmap, greenlit 2026-09-16 (memory `9adcd8dc`).
Where `SPEC-CONTEXT-RETRIEVAL-D1-HIERARCHICAL-COMMUNITIES.md` builds the internal hierarchy
infrastructure (constraint 25), this spec builds the thing a caller actually invokes: a new
`strategy: "global"` value on the existing `retrieve_context` MCP tool, implementing standing
constraints 24 (integration shape), 26 (retrieval/synthesis mechanics), and 27 (primary-search
seeding) in full.

Two live probes from the same greenlighting session motivate this spec's two central mechanics.
First (memory `13549b72`/`6b9c866e`), flat communities are bimodal on this corpus and naive "return
the whole matched community" would flood a caller with noise — D1's hierarchy exists so this spec
can synthesize from small, coherent leaf communities instead of large, heterogeneous ones. Second
(memory `e957aa78`), `retrieve_context`'s existing entity-level primary search runs a deliberate,
calibrated strict relevance-abstention gate (`b9b75764`) that reproducibly returns near-nothing for
long, natural-language, conversational queries — exactly D's own expected query shape ("what do we
know about X as a whole"). zbalint's own words, verbatim: "there is no value in the retrieve_context
if it does not retrieves the context." This spec's seeding mechanism (§3, constraint 27) sidesteps
that gate entirely rather than attempting to recalibrate a threshold that already broke once for
this exact query shape.

**Locked upstream design decisions this spec implements as-is** (constraints 24/26/27's own design,
restated only to the depth needed to implement):

1. **Integration shape (constraint 24)**: a new `strategy: "global"` value on the *existing*
   `retrieve_context` MCP tool — not a new standalone tool. D's v1 request signature adds exactly
   one new parameter, `strategy`; it does **not** add a caller-supplied `entity_ids`/`community_id`
   seed override (zbalint's own explicit caveat, accepted as a deliberate, evidence-gated deferral
   for a later milestone — "only query now, but dont forget to extend later" — not a permanent
   foreclosure; this spec does not build any such override and must not be read as closing the door
   on one). The result reuses the existing `memories[]`/`inclusion` envelope, extended with two new
   `inclusion` values, plus new sibling `metadata.*` namespaces mirroring the existing
   `metadata.fan_out`/`metadata.budget` shape — never a materially different top-level result shape.
2. **Seeding (constraint 27)**: the query embedding is compared directly, via cosine similarity,
   against each **leaf** community's own centroid vector (D1's `fetch_leaf_community_centroids`) —
   entity-level primary search plays no role in global-mode seeding at all, never invokes
   `retrieve_context`'s existing strict abstention gate, and never reuses or recalibrates
   `search_memory`'s broad mode either. Seeding is unconditional best-effort top-K: there is no
   minimum-similarity abstention floor in v1 (deferred to post-ship real-usage evidence, per
   zbalint's own explicit choice — do not add one as a "safety" addition this spec was never asked
   for).
3. **Synthesis (constraint 26)**: within a selected leaf, members are ranked by
   query-embedding-similarity (not centrality, not similarity to the leaf's own centroid) and
   packed via the existing G8 real-token budget packer (`pack_context_budget`, extended in §4, never
   a new/parallel packer). Each selected leaf's own constraint-19 representative is force-included
   as a separate, budget-accounted, non-exempt guaranteed slot outside the member pool — mirroring
   how `orphan_community`'s own force-include reserve already works in the existing pipeline, never
   competing with `member_pool` for the same slots. When multiple leaf communities are selected,
   their members compete in one shared, continuously relevance-ranked pool across communities —
   never siloed into separate per-community sub-pools.

**Locked design decisions this spec makes that were left open upstream** (mirroring the sibling
Milestone C/C.5/D1 specs' own §1 precedent for this category):

1. **Two independently-capped reserves, not one gating the other.** Constraint 26 names
   `representative_reserve` and `member_pool` as two separate PLACEHOLDER "reserve sizes" (plural),
   which this spec reads as two genuinely decoupled caps: `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP`
   bounds how many of the top-K *seeded* communities actually get a force-included representative
   slot (ranked by each community's own seed similarity — the lowest-similarity seeded communities'
   representatives are dropped first if the seed count exceeds this cap), while
   `CONTEXT_GLOBAL_MEMBER_POOL_CAP` bounds the combined candidate-member pool across **all** seeded
   communities regardless of whether that community's own representative survived the first cap. A
   community whose representative was dropped by the reserve cap still contributes its own members
   to the member pool on equal footing with every other seeded community's members — the two caps
   never interact.
2. **Member-pool capping is two-layered, mirroring G4+G8's own existing node-eligibility-then-
   token-budget split** (constraint 25's own explicit "independent axes" framing, reused here by
   direct analogy): `CONTEXT_GLOBAL_MEMBER_POOL_CAP` is a flat node-count eligibility cap applied
   *before* token-budget packing (reported under `metadata.community.member_pool`), and
   `pack_context_budget`'s own real-token pass (§4) is the separate, second truncation layer applied
   *after* (reported under `metadata.budget.community_member_*`, alongside `primary_*`/
   `expansion_*`'s existing sibling fields). A member surviving the first cap can still be dropped
   by the second if the token budget is tight; a member failing the first cap is never even offered
   to the second.
3. **`community_retrieval_service.py` owns both caps internally**, mirroring
   `context_expansion_service.expand_context_candidates` and `orphan_community_service.
   find_orphan_community_matches`'s own existing precedent of computing and reporting their own
   node-eligibility caps internally rather than leaving that to the orchestrator
   (`retrieve_context_service.assemble_retrieve_context`). The orchestrator's own job, matching its
   existing role in the local-mode pipeline, is composition and the token-budget pass only.
4. **`retrieval_provenance` shapes for the two new `inclusion` values** (never specified upstream,
   mirroring `orphan_community`'s own `{reason, orphan_entity_id, community_id, similarity}`
   precedent, constraint 16): `community_representative` items carry
   `[{"reason": "community_representative", "community_id": <str>, "seed_similarity": <float>}]`
   (the community's own centroid-to-query similarity that got it selected as a seed);
   `community_member` items carry
   `[{"reason": "community_member", "community_id": <str>, "similarity": <float>}]` (that specific
   member's own entity-embedding-to-query similarity, distinct from its community's seed
   similarity).
5. **`edges`, `lineage`, and `conflict_sets` are always `[]`/`{}`/`[]` for a global-mode result** —
   none of Milestone D's five mechanics tickets (constraints 24-28) mention in-network-edge surfacing,
   supersession-chain assembly, or contradiction detection for community members, and none of
   `expand_context_candidates`/`assemble_lineage`/`assemble_conflict_sets` are ever called on the
   global-mode path (design decision 7 below). This keeps the top-level envelope's key set
   identical across both strategies (constraint 24's own "never a materially different top-level
   shape" requirement) without fabricating meaningless non-empty data for concepts that simply do
   not apply to a community-seeded result in v1.
6. **`metadata.fan_out` is omitted (key absent, not an empty/zeroed dict) for a global-mode result;
   `metadata.budget` is always present in both strategies.** G4's `fan_out` cap is specifically
   about out-of-network-neighbor expansion during local-mode graph traversal, a mechanism that never
   runs on the global-mode path at all — synthesizing a fake all-zero `fan_out` object would imply a
   mechanism ran and found nothing, which is a different, false claim from "this mechanism does not
   apply here." `metadata.budget`, by contrast, is genuinely shared: constraint 26 explicitly reuses
   G8's real packer for both strategies, so the same `budget` shape (extended per §4) is always
   populated, with the strategy-inapplicable `primary_*`/`expansion_*`/`conflict_reserve_*`/
   `orphan_community_reserve_*` fields simply reporting their own natural zero/false values on a
   global-mode call (since `pack_context_budget` is called with empty stand-ins for those inputs,
   §5) rather than being a second, differently-shaped `budget` object per strategy.
7. **`assemble_retrieve_context`'s global branch never calls `memory_service.search_memory`,
   `expand_context_candidates`, `assemble_conflict_sets`, `find_orphan_community_matches`, or
   `assemble_lineage`** — a direct, literal reading of constraint 27's "entity-level primary search
   plays no role in global-mode seeding at all," generalized to the whole global-mode branch, not
   only its seeding step: none of those five functions has any defined role in a global-mode
   result at all under constraints 24/26/27 as locked, so calling any of them would be inventing
   scope this spec was never asked to cover.
8. **Global-mode results are never owner-scoped, and `_assemble_global_context`/
   `seed_and_rank_communities` never receive or consult `owner_id` at all.** This is not an
   oversight this spec introduces — `communities` and `community_membership` (Milestone C's own
   schema, constraint 21) carry no `owner_id` column at all, so community detection already
   clusters the whole relation graph corpus-wide regardless of who wrote what; there is no
   owner-scoped subset of a leaf community's centroid or membership this spec could even construct
   from the tables it reads. `assemble_retrieve_context`'s own `owner_id` parameter continues to be
   threaded to and used only by the unchanged local-mode branch (its own `search_memory` call,
   current line 54), exactly as today.

## 2. `src/saltmdb/config.py`

Insert immediately after the existing `COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD` block (current line
335), before the blank line preceding the `# Rework Phase 6` comment (current line 337):

```python

# Milestone D (wayfinder ticket "Milestone D primary-search seeding," standing constraint 27,
# memory 6ca4317c) -- strategy:"global" retrieve_context's seed selection: the number of nearest
# (by cosine similarity of the query embedding to each leaf community's own centroid) leaf
# communities taken as seeds for a global-mode call. Unconditional best-effort top-K -- no minimum-
# similarity abstention floor in v1 (a deliberate choice, not an oversight: constraint 27 exists
# specifically because retrieve_context's existing strict abstention gate already broke this exact
# query shape once, see b9b75764/e957aa78; adding a second uncalibrated threshold in the same
# subsystem for the same reason would repeat that mistake). A FLAT constant, not scaled by anything
# -- unlike CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's per-primary-hit-count scaling, there is no
# analogous per-call quantity to scale a community seed count against in this design. PLACEHOLDER:
# not yet benchmarked against SALTMDB's own corpus -- seeded at the same order of magnitude as
# CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS's own current value, not derived from any benchmark of its
# own. Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing
# constraint 28) via its own concrete quantitative sweep against the live corpus. Do not remove the
# placeholder framing when tuning this; replace this comment with the benchmark citation once a
# real value is locked.
CONTEXT_GLOBAL_TOP_K_COMMUNITIES = 10

# Milestone D (wayfinder ticket "Milestone D retrieval/synthesis mechanics," standing constraint 26,
# memory 51127287) -- strategy:"global" retrieve_context's representative-slot reserve: how many of
# the CONTEXT_GLOBAL_TOP_K_COMMUNITIES seeded leaf communities actually get their own constraint-19
# representative force-included as a guaranteed, budget-accounted (but never budget-gated) slot.
# Ranked by each seeded community's own seed similarity -- the lowest-similarity seeded communities'
# representatives are dropped first if the seed count exceeds this cap. Independently capped from
# CONTEXT_GLOBAL_MEMBER_POOL_CAP below -- a community whose representative was dropped here still
# contributes its own members to the member pool on equal footing (see this spec's own §1 design
# decision 1). PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus -- seeded at the same
# order of magnitude as CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP's own current value, mirroring that
# constant's own "sparse force-include mechanism" shape, not derived from any benchmark of its own.
# Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28)
# via its own concrete quantitative sweep against the live corpus. Do not remove the placeholder
# framing when tuning this; replace this comment with the benchmark citation once a real value is
# locked.
CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP = 5

# Milestone D (wayfinder ticket "Milestone D retrieval/synthesis mechanics," standing constraint 26,
# memory 51127287) -- strategy:"global" retrieve_context's member-pool node-eligibility cap: the
# maximum combined candidate-member count, across every seeded leaf community, that is even offered
# to context_budget_service.pack_context_budget's own separate real-token packing pass. A node-
# eligibility cap, not a token-count cap -- mirrors CONTEXT_EXPANSION_TOP_K_RELATIONSHIPS/G4's own
# "independent from the token-budget axis" precedent (constraint 25's own explicit framing), applied
# here at the community-member level. Ranked by each member's own query-embedding similarity across
# the whole combined pool (never per-community sub-pools) before this cap truncates it, matching
# constraint 26's own "one shared, continuously relevance-ranked pool" requirement. PLACEHOLDER: not
# yet benchmarked against SALTMDB's own corpus -- seeded at CONTEXT_GLOBAL_TOP_K_COMMUNITIES times a
# small constant, giving each seeded community a comparable member-slot budget on average to what a
# single Milestone-A expansion pass typically admits, not derived from any benchmark of its own.
# Recalibrated by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28)
# via its own concrete quantitative sweep against the live corpus. Do not remove the placeholder
# framing when tuning this; replace this comment with the benchmark citation once a real value is
# locked.
CONTEXT_GLOBAL_MEMBER_POOL_CAP = 30
```

## 3. New file: `src/saltmdb/domain/services/community_retrieval_service.py`

A new domain-service module, sibling to `orphan_community_service.py`, following the same
conventions: module-level `logger = logging.getLogger(__name__)`, the same `db_connection=None,
db_path: str | None = None` open-or-reuse-connection pattern, its own small private `_normalize`
helper (mirrors the existing per-module convention already established independently in both
`community_detection_service.py`'s `_normalize_community_vector` and `orphan_community_service.py`'s
own `_normalize` — this spec does not introduce a new shared-utility module to deduplicate a
one-line helper the codebase already duplicates twice today; that would be an out-of-scope
refactor of already-shipped code, not something this spec was asked to do).

### 3.1 `seed_and_rank_communities` — the module's one public function

```python
def seed_and_rank_communities(
    query: str,
    *,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Seed and rank leaf communities and their members for strategy:"global" retrieve_context.

    Embeds `query` exactly once (constraint 27), compares it against every leaf community's own
    centroid (never a non-leaf/parent centroid -- see D1's fetch_leaf_community_centroids), takes
    the top CONTEXT_GLOBAL_TOP_K_COMMUNITIES nearest as seeds, then computes two independently
    capped, already-ranked candidate lists ready for context_budget_service.pack_context_budget's
    own token-budget pass: one representative per seeded community (ranked by that community's own
    seed similarity, capped at CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP) and a combined member
    pool across every seeded community (ranked by each member's own query similarity, capped at
    CONTEXT_GLOBAL_MEMBER_POOL_CAP, excluding whichever entity is that community's own
    representative -- a representative is never also offered as a member candidate).

    Before Milestone D's hierarchy has ever run once (an empty `communities` table -- a fresh
    install, or one where the community-detection trigger hasn't fired yet), this mechanism
    silently no-ops: zero leaf communities to compare against means zero seeds and every returned
    list/cap-shape reports its own natural empty/zero state, never an error.
    """
```

### 3.2 Algorithm (prescriptive)

1. `should_close = db_connection is None`; `conn = db_connection if db_connection is not None else
   get_connection(db_path or get_db_path())`.
2. If `not query.strip()`: skip straight to step 9's empty-shape return (mirrors
   `embedding_service.embed_text`'s own empty/whitespace-string contract — an empty query embeds to
   an all-zero vector, which would spuriously "match" nothing meaningfully; treat it the same as
   zero leaf communities existing, not as a real query).
3. `query_vector = _normalize(np.array(embed_text(query), dtype=np.float64))`.
4. `centroid_rows = fetch_leaf_community_centroids(conn)` (imported from
   `saltmdb.domain.services.community_detection_service`, D1's own helper — this is the one call
   site this spec depends on D1 for). If empty, skip to step 9.
5. For each `(community_id, blob)` in `centroid_rows`: `centroid = _normalize(np.frombuffer(blob,
   dtype=np.float32).astype(np.float64))`; `similarity = float(np.dot(query_vector, centroid))`.
   Rank all leaf communities by `(-similarity, community_id)` (highest similarity first, lowest
   `community_id` breaking ties — mirrors `orphan_community_service`'s own established tie-break
   convention). `eligible_count = len(centroid_rows)`; `seeded = ranked[:CONTEXT_GLOBAL_TOP_K_
   COMMUNITIES]`; `seed_cap = {"cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES, "eligible_count":
   eligible_count, "truncated": eligible_count > CONTEXT_GLOBAL_TOP_K_COMMUNITIES, "dropped_count":
   max(0, eligible_count - CONTEXT_GLOBAL_TOP_K_COMMUNITIES)}`. If `seeded` is empty, skip to step 9.
6. For the seeded community ids, batch-fetch `id, representative_entity_id, member_count` from
   `communities` and every `community_membership` row for each (`entity_id` where `community_id IN
   (...)`), building `members_by_community: dict[str, list[str]]` (each list excludes that
   community's own `representative_entity_id`).
7. **Representative reserve**: build `representative_candidates = [(entity_id=representative_
   entity_id, community_id, similarity=<that community's own seed similarity from step 5>) for each
   seeded community]`, already ranked in the same order `seeded` is (seed-similarity descending,
   community_id tie-break already applied in step 5 — no separate re-sort needed since this list's
   order is derived directly from `seeded`'s own order). `representative_eligible_count =
   len(seeded)`; `representative_admitted = representative_candidates[:CONTEXT_GLOBAL_
   REPRESENTATIVE_RESERVE_CAP]`; `representative_reserve = {"cap": CONTEXT_GLOBAL_REPRESENTATIVE_
   RESERVE_CAP, "eligible_count": representative_eligible_count, "truncated":
   representative_eligible_count > CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP, "dropped_count":
   max(0, representative_eligible_count - CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP)}`.
8. **Member pool**: batch-fetch `entity_id, embedding` from `entity_embeddings` for every entity_id
   across all `members_by_community` values combined (one query, mirroring
   `orphan_community_service`'s own batch-fetch pattern for member embeddings). For each, compute
   `similarity = float(np.dot(query_vector, _normalize(np.frombuffer(blob, dtype=np.float32)
   .astype(np.float64))))`. Rank the full combined list by `(-similarity, entity_id)`.
   `member_eligible_count = len(...)`; `member_admitted = ranked_members[:CONTEXT_GLOBAL_MEMBER_
   POOL_CAP]`; `member_pool = {"cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP, "eligible_count":
   member_eligible_count, "truncated": member_eligible_count > CONTEXT_GLOBAL_MEMBER_POOL_CAP,
   "dropped_count": max(0, member_eligible_count - CONTEXT_GLOBAL_MEMBER_POOL_CAP)}`. An entity
   missing its own `entity_embeddings` row (mirrors `community_detection_service`'s own graceful
   degradation for a missing embedding) is simply excluded from ranking, not treated as an error.
9. Batch-fetch `id, title` from `entities` for every admitted representative and member entity_id
   (one combined query, mirroring `orphan_community_service`'s own title-fetch pattern) — an id
   present in `entities` with a `NULL` title reads as `"Unknown"`, matching that same precedent
   exactly.
10. Return:
    ```python
    {
        "representative_matches": [
            {"entity_id": ..., "title": ..., "community_id": ..., "seed_similarity": <float>}
            for ... in representative_admitted
        ],
        "member_matches": [
            {"entity_id": ..., "title": ..., "community_id": ..., "similarity": <float>}
            for ... in member_admitted
        ],
        "seed_cap": seed_cap,
        "representative_reserve": representative_reserve,
        "member_pool": member_pool,
    }
    ```
    When any of steps 2/4/5 short-circuits early, return this exact same shape with every list `[]`
    and every cap-shape reporting `{"cap": <the real config constant>, "eligible_count": 0,
    "truncated": False, "dropped_count": 0}` — never a differently-shaped early-return dict (this
    spec's own version of the C.5-precedented "zero-shape must match the real shape exactly" rule,
    §9's amendment below exists precisely because a prior spec in this same effort got this
    wrong once already).
11. `finally: if should_close: close_connection(conn)`.

## 4. `src/saltmdb/domain/services/context_budget_service.py`

Extend `pack_context_budget`'s existing signature with two new optional keyword parameters, inserted
alphabetically-adjacent to the existing `orphan_community_entity_ids` parameter:

```python
def pack_context_budget(
    expansion_result: dict[str, Any],
    primary_hits: list[dict[str, Any]],
    conflict_sets_result: dict[str, Any],
    *,
    budget_tokens: int | None = None,
    community_member_ids: list[str] | None = None,
    community_representative_ids: set[str] | None = None,
    orphan_community_entity_ids: set[str] | None = None,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

`community_member_ids` is an **already-ranked** list (highest relevance first — the caller, §5,
supplies it pre-sorted from `seed_and_rank_communities`'s own already-ranked `member_matches`;
mirrors how `primary_ids`/`expansion_ids` are already pre-sorted by their own upstream producers
before reaching this function — this function itself never re-sorts anything, for any candidate
class). `community_representative_ids` mirrors `orphan_community_entity_ids`'s existing shape and
handling exactly (a `set`, force-included unconditionally, tallied into its own reserve, never
gated by the `used + cost <= effective_budget` check).

Extend the existing all-empty early-return condition (current line 46) to also cover the two new
inputs:

```python
    community_member_ids = community_member_ids or []
    community_representative_ids = community_representative_ids or set()

    if (
        not primary_ids
        and not expansion_ids
        and not conflict_only_ids
        and not orphan_community_ids
        and not community_member_ids
        and not community_representative_ids
    ):
        return {
            "packed_entity_ids": {"primary": [], "expansion": [], "community_member": []},
            "dropped_entity_ids": {"primary": [], "expansion": [], "community_member": []},
            "conflict_only_entity_ids": [],
            "community_representative_entity_ids": [],
            "token_counts": {},
            "budget": {
                "unit": "tokens",
                "limit": effective_budget,
                "used": 0,
                "primary_truncated": False,
                "primary_dropped_count": 0,
                "expansion_truncated": False,
                "expansion_dropped_count": 0,
                "conflict_reserve_tokens_used": 0,
                "orphan_community_reserve_tokens_used": 0,
                "community_member_truncated": False,
                "community_member_dropped_count": 0,
                "community_representative_reserve_tokens_used": 0,
            },
        }
```

(This is the exact same early-return branch already in the file, current lines 46-63 — extended in
place with the three new fields threaded through every affected key, not a second, separately
maintained copy. Both this early-return shape and the normal-path return shape below must be kept
in sync field-for-field — this is precisely the class of bug `SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-
ASSIGNMENT.md`'s own Amendment 1 fixed after locking; verified here, before locking, by writing
both shapes side by side in this section.)

Extend the `all_ids` fetch (current line 72) to include the two new id sources:

```python
        all_ids = (
            set(primary_ids)
            | set(expansion_ids)
            | conflict_only_ids
            | orphan_community_ids
            | set(community_member_ids)
            | community_representative_ids
        )
```

Immediately after the existing expansion-packing loop (current lines 97-103), add a new, structurally
identical loop continuing the **same** `used` accumulator (never resetting it — this is what makes
`community_member_ids` join primary/expansion's existing single continuous relevance-ordered pass,
constraint 26's own "one shared, continuously relevance-ranked pool" requirement, satisfied by
construction rather than by a separate merge step):

```python
        community_member_packed: list[str] = []
        community_member_dropped: list[str] = []
        for entity_id in community_member_ids:
            cost = token_counts[entity_id]
            if used + cost <= effective_budget:
                community_member_packed.append(entity_id)
                used += cost
            else:
                community_member_dropped.append(entity_id)
```

Extend the existing unconditional-reserve tally (current lines 105-110, the
`orphan_community_reserve_tokens_used` computation) with a parallel tally for the new reserve:

```python
        community_representative_reserve_tokens_used = sum(
            token_counts[entity_id] for entity_id in community_representative_ids
        )
```

Extend the normal-path return dict (current lines 111-127) with the new fields, in the same
positions as their early-return counterparts above:

```python
        return {
            "packed_entity_ids": {
                "primary": primary_packed,
                "expansion": expansion_packed,
                "community_member": community_member_packed,
            },
            "dropped_entity_ids": {
                "primary": primary_dropped,
                "expansion": expansion_dropped,
                "community_member": community_member_dropped,
            },
            "conflict_only_entity_ids": sorted(conflict_only_ids),
            "community_representative_entity_ids": sorted(community_representative_ids),
            "token_counts": token_counts,
            "budget": {
                "unit": "tokens",
                "limit": effective_budget,
                "used": used,
                "primary_truncated": len(primary_dropped) > 0,
                "primary_dropped_count": len(primary_dropped),
                "expansion_truncated": len(expansion_dropped) > 0,
                "expansion_dropped_count": len(expansion_dropped),
                "conflict_reserve_tokens_used": conflict_reserve_tokens_used,
                "orphan_community_reserve_tokens_used": orphan_community_reserve_tokens_used,
                "community_member_truncated": len(community_member_dropped) > 0,
                "community_member_dropped_count": len(community_member_dropped),
                "community_representative_reserve_tokens_used":
                    community_representative_reserve_tokens_used,
            },
        }
```

No other line in this file changes — the existing `primary`/`expansion` packing loops, the existing
`conflict_reserve_tokens_used` computation, and every existing field not listed above are untouched.

## 5. `src/saltmdb/domain/services/retrieve_context_service.py`

### 5.1 New signature

Add `strategy: str = "local"` to `assemble_retrieve_context`'s existing keyword-only parameters,
immediately after `budget_tokens`:

```python
def assemble_retrieve_context(  # noqa: C901, PLR0912, PLR0915
    query: str,
    owner_id: str | None,
    *,
    limit: int | None = None,
    budget_tokens: int | None = None,
    strategy: str = "local",
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
```

### 5.2 New branch

Immediately after the existing connection-setup block (current lines 33-46, unchanged), before the
existing `try:` body's local-mode pipeline (current line 47's `pit = ...` onward), branch on
`strategy`:

```python
    try:
        if strategy == "global":
            return _assemble_global_context(query, budget_tokens, conn)
        # ... existing local-mode pipeline (current lines 48-313) is completely unchanged below
        # this point, and runs only for strategy == "local" ...
```

The existing local-mode body (current lines 48-313) is otherwise **byte-for-byte unchanged** —
every line of it stays exactly where it is, simply now reached only through the `else` implied by
the new `if strategy == "global": return ...` guard above it, inside the same existing `try`/
`finally` (the existing `finally: if should_close: close_connection(conn)` at current lines 314-316
still applies to both branches unchanged, since both paths share the single connection opened at
the top of the function).

### 5.3 New private helper `_assemble_global_context`

Add this new module-level private function, called only from §5.2's branch:

```python
def _assemble_global_context(
    query: str, budget_tokens: int | None, conn: sqlite3.Connection
) -> dict[str, Any]:
    seed_result = seed_and_rank_communities(query, db_connection=conn)
    representative_entity_ids = {
        match["entity_id"] for match in seed_result["representative_matches"]
    }
    member_entity_ids = [match["entity_id"] for match in seed_result["member_matches"]]

    budget_result = pack_context_budget(
        {"expansion_candidates": []},
        [],
        {"conflict_sets": []},
        budget_tokens=budget_tokens,
        community_member_ids=member_entity_ids,
        community_representative_ids=representative_entity_ids,
        db_connection=conn,
    )

    title_by_id = {
        match["entity_id"]: match["title"]
        for match in seed_result["representative_matches"] + seed_result["member_matches"]
    }
    community_id_by_id = {
        match["entity_id"]: match["community_id"]
        for match in seed_result["representative_matches"] + seed_result["member_matches"]
    }
    similarity_by_id = {
        match["entity_id"]: match["seed_similarity"]
        for match in seed_result["representative_matches"]
    } | {
        match["entity_id"]: match["similarity"] for match in seed_result["member_matches"]
    }

    surfaced_ids = set(budget_result["community_representative_entity_ids"]) | set(
        budget_result["packed_entity_ids"]["community_member"]
    )
    memory_type_by_id: dict[str, str] = {}
    if surfaced_ids:
        placeholders = ",".join("?" for _ in surfaced_ids)
        rows = conn.execute(
            f"SELECT id, memory_type FROM entities WHERE id IN ({placeholders})",
            tuple(surfaced_ids),
        ).fetchall()
        memory_type_by_id = {row[0]: row[1] for row in rows}

    memories: list[dict[str, Any]] = []
    for entity_id in budget_result["community_representative_entity_ids"]:
        memories.append(
            {
                "entity_id": entity_id,
                "title": title_by_id[entity_id],
                "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                "inclusion": "community_representative",
                "retrieval_provenance": [
                    {
                        "reason": "community_representative",
                        "community_id": community_id_by_id[entity_id],
                        "seed_similarity": similarity_by_id[entity_id],
                    }
                ],
            }
        )
    for entity_id in budget_result["packed_entity_ids"]["community_member"]:
        memories.append(
            {
                "entity_id": entity_id,
                "title": title_by_id[entity_id],
                "memory_type": memory_type_by_id.get(entity_id, "unknown"),
                "inclusion": "community_member",
                "retrieval_provenance": [
                    {
                        "reason": "community_member",
                        "community_id": community_id_by_id[entity_id],
                        "similarity": similarity_by_id[entity_id],
                    }
                ],
            }
        )

    return {
        "query": query,
        "memories": memories,
        "edges": [],
        "lineage": {},
        "conflict_sets": [],
        "metadata": {
            "strategy": "global",
            "community": {
                "seed_top_k": seed_result["seed_cap"],
                "representative_reserve": seed_result["representative_reserve"],
                "member_pool": seed_result["member_pool"],
            },
            "budget": budget_result["budget"],
        },
    }
```

### 5.4 New imports

Add to the existing `from saltmdb.domain.services...` import block:

```python
from saltmdb.domain.services.community_retrieval_service import seed_and_rank_communities
```

## 6. `src/saltmdb/mcp/tools.py`

Extend `retrieve_context`'s signature (current lines 1079-1083) with one new trailing parameter:

```python
@mcp.tool()
def retrieve_context(
    query: str,
    limit: int | None = None,
    budget_tokens: int | None = None,
    strategy: Literal["local", "global"] | None = None,
) -> dict:
```

Extend the existing docstring (current lines 1084-1103) with one new paragraph, inserted after the
existing `limit`/`budget_tokens` paragraph, before the closing `"""`:

```python
    strategy selects the retrieval mode: "local" (the default) is the existing bounded one-hop-plus
    expansion described above. "global" instead seeds from whichever leaf community-detection
    clusters (see Milestone C) are nearest the query by embedding similarity, synthesizing a
    representative-plus-ranked-members result per seeded community for whole-topic breadth rather
    than one-hop-local depth -- it never runs the primary-search/expansion/conflict/lineage pipeline
    above at all, so edges/lineage/conflict_sets are always empty and memories[] items instead carry
    inclusion "community_representative"|"community_member" with a different retrieval_provenance
    shape (community_id plus a similarity score) and metadata carries a new metadata.community
    namespace in place of metadata.fan_out.
```

Extend the existing backend call's payload (current lines 1108-1113) with the new field:

```python
    return _backend_or_raise().call(
        "retrieve_context",
        {
            "query": query,
            "limit": limit,
            "budget_tokens": budget_tokens,
            "strategy": strategy if strategy is not None else "local",
            "owner_id": owner_id_,
        },
    )
```

## 7. `src/saltmdb/daemon/dispatch.py`

Add a new helper immediately after the existing `_optional_mode` (current lines 80-86), mirroring
its exact shape:

```python
def _optional_strategy(kw: dict[str, Any]) -> Literal["local", "global"]:
    value = kw.get("strategy")
    if value is None:
        return "local"
    if value not in {"local", "global"}:
        raise ValueError("strategy must be 'local' or 'global'")
    return value
```

Extend `_dispatch_retrieve_context` (current lines 438-447) with the new field:

```python
def _dispatch_retrieve_context(**kw):
    query = kw.get("query")
    if not isinstance(query, str):
        raise ValueError("query is required")
    return retrieve_context_service.assemble_retrieve_context(
        query=query,
        owner_id=kw.get("owner_id"),
        limit=_optional_int_or_none(kw, "limit"),
        budget_tokens=_optional_int_or_none(kw, "budget_tokens"),
        strategy=_optional_strategy(kw),
    )
```

## 8. `tests/test_community_retrieval_service.py` (new file)

New file, following `tests/test_orphan_community_service.py`'s own fixture conventions (`init_db`
into a temp file, `store_memory`/`store_relation` helpers, `recompute_communities` called directly
against the test's own connection to populate `communities`/`community_membership`/
`community_embeddings` before each scenario that needs them). Required scenarios,
`test_scenario_NN_<description>`, numbered from 1:

1. Zero communities exist (no `recompute_communities` ever run) → `seed_and_rank_communities`
   returns the fully-empty shape from §3.2 step 10, opening no database work beyond the guaranteed
   connection (mirrors `find_orphan_community_matches`'s own "before Milestone C has ever run once"
   precedent).
2. Empty/whitespace-only query string → the same fully-empty shape, without ever calling
   `embed_text` (mirrors step 2's short-circuit).
3. Fewer leaf communities exist than `CONTEXT_GLOBAL_TOP_K_COMMUNITIES` → all of them are seeded,
   `seed_cap.truncated is False`.
4. More leaf communities exist than `CONTEXT_GLOBAL_TOP_K_COMMUNITIES` (monkey-patch the constant
   small) → only the top-K by similarity are seeded, `seed_cap.truncated is True` with the correct
   `dropped_count`.
5. A seeded community's own representative is correctly excluded from that same community's member
   candidates (never appears in `member_matches` for its own community).
6. More seeded communities exist than `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` (monkey-patch
   small) → only the highest-seed-similarity communities' representatives appear in
   `representative_matches`; the lower-similarity seeded communities' members still appear in
   `member_matches` (proves the two caps are independent, §1 design decision 1).
7. More combined candidate members exist than `CONTEXT_GLOBAL_MEMBER_POOL_CAP` (monkey-patch small)
   → `member_pool.truncated is True` with the correct `dropped_count`, and the admitted members are
   exactly the highest-query-similarity ones across the combined pool, not per-community top-N
   (proves "one shared pool," not per-community silos).
8. A member missing its own `entity_embeddings` row is silently excluded from ranking, not an error
   (mirrors `community_detection_service`'s own graceful-degradation precedent).
9. Deterministic tie-break: two leaf communities with identical (engineered) centroid similarity to
   the query seed in `community_id` order; two candidate members with identical similarity rank in
   `entity_id` order.
10. `title` reads `"Unknown"` for an admitted entity whose `entities.title` is `NULL` (mirrors
    `orphan_community_service`'s own precedent exactly).

## 9. `tests/test_context_budget_service.py`, `tests/test_retrieve_context_service.py`,
`tests/test_retrieve_context_wiring.py` — required scenarios and the exact amendment sites

**New scenarios**, `test_context_budget_service.py` (continuing its own numbering, currently ends
at 14, starting at 15): community_member_ids pack in list order up to the token budget and report
`community_member_truncated`/`dropped_count` correctly; community_representative_ids are force-
included and tallied into `community_representative_reserve_tokens_used` regardless of how full the
budget already is (mirrors the existing `test_scenario_13_pack_budget_reserves_orphan_tokens_
outside_ordinary_budget` scenario's own shape, for the new reserve).

**New scenarios**, `test_retrieve_context_service.py` (continuing its own numbering): a
`strategy="global"` call against a corpus with at least one populated leaf community surfaces
`community_representative`/`community_member` items with the exact `retrieval_provenance` shapes
from §1 design decision 4; `edges == []`, `lineage == {}`, `conflict_sets == []`, and
`"fan_out" not in result["metadata"]` for a global-mode result; a `strategy="global"` call against a
corpus with zero communities returns the same empty `memories == []` shape a local-mode zero-hit call
already returns for `edges`/`lineage`/`conflict_sets`, with `metadata.community` reporting every cap
at its own zero/empty state (mirroring §3.2 step 10); a `strategy="local"` (or omitted) call's
result is byte-for-byte unaffected by this spec (regression guard directly exercising §5.2's own
"local-mode body is byte-for-byte unchanged" claim).

**Named amendment sites — the only three pre-existing assertions in scope for editing**, found by
tracing this spec's own additive `strategy` parameter through every existing caller before locking
(pre-lock gate step 2/3):

1. `tests/test_retrieve_context_wiring.py::TestRetrieveContextWiring.
   test_dispatch_forwards_omitted_optional_values_as_none` (current lines 50-63) — its
   `assemble.assert_called_once_with(query="q", owner_id="owner", limit=None, budget_tokens=None)`
   must add `strategy="local"` (§7's `_optional_strategy` always resolves `None` to the literal
   `"local"` before calling `assemble_retrieve_context`, exactly mirroring how `_optional_mode`
   already always resolves `search_memory`'s own `mode` the same way).
2. `tests/test_retrieve_context_wiring.py::TestRetrieveContextWiring.
   test_dispatch_requires_string_query_but_allows_empty_string` (current lines 65-83) — its own
   identical `assemble.assert_called_once_with(...)` needs the same `strategy="local"` addition.
3. `tests/test_retrieve_context_wiring.py::TestRetrieveContextWiring.
   test_public_schema_exposes_query_controls_without_owner_id` (current lines 85-92) — its
   `list(inspect.signature(tools.retrieve_context).parameters) == ["query", "limit",
   "budget_tokens"]` must become `["query", "limit", "budget_tokens", "strategy"]` (§6 appends
   `strategy` as the trailing parameter).

No other line in `test_retrieve_context_wiring.py` changes.
`test_public_tool_reaches_real_dispatch_and_returns_envelope` (current lines 94-112) is deliberately
**not** in this list — its `set(envelope) == {"query", "memories", "edges", "lineage",
"conflict_sets", "metadata"}` assertion exercises a default (`strategy` omitted, i.e. local-mode)
call, and this spec's own §1 design decision 5/§5.2 keep the top-level key set identical across both
strategies, so that assertion continues to pass unmodified — verified here by inspection rather than
assumed, since it is exactly the kind of whole-shape assertion the other three needed amending for.

## 10. Out of scope

- A caller-supplied `entity_ids`/`community_id` seed override for global mode — explicitly deferred
  by constraint 24 itself to a future milestone, not built here.
- A minimum-similarity abstention floor for global-mode seeding — explicitly rejected by constraint
  27 itself (§1 above); do not add one even as a defensive addition.
- Any change to `community_detection_service.py`, `orphan_community_service.py`, `schema.py`, or
  `vector_schema.py` — this spec only reads what
  `SPEC-CONTEXT-RETRIEVAL-D1-HIERARCHICAL-COMMUNITIES.md` already built.
- Any change to `context_expansion_service.py`, `conflict_set_service.py`, or
  `lineage_assembly_service.py`, or to the existing local-mode pipeline inside
  `assemble_retrieve_context` — §5.2 requires the existing local-mode body be reached unchanged, and
  none of those three modules is ever called from the new global branch (§1 design decision 7).
- Numeric acceptance-bar values (the recall/precision/comprehensiveness bar itself, or final values
  for any of this spec's or D1's five PLACEHOLDER constants) — deferred to the Milestone D benchmark
  run (wayfinder ticket `f3f03936`, standing constraint 28), which executes after this spec and D1
  are both implemented, per constraint 23's inherited spec-first-then-calibrate ordering. This
  spec's own acceptance bar (§11) is pure structural correctness against each constant's own
  *symbol*, never a specific numeric expectation.
- Renumbering or otherwise touching any existing test scenario in any of the four edited test files
  beyond the three named amendment sites in §9 — every other line in every edited test file is
  append-only under this spec's scope (§0/§9).

## 11. Acceptance

```bash
cd /home/zbalint/workspace/SALTMDB-context-retrieval-d2
uv run pytest tests/test_community_retrieval_service.py tests/test_context_budget_service.py tests/test_retrieve_context_service.py tests/test_retrieve_context_wiring.py -v
uv run pytest -q
```

Both commands must exit 0. The targeted run must show every scenario listed in §8/§9 present and
passing, with every pre-existing scenario in all four files still passing — the three named
amendment sites in §9 updated exactly as specified and no other line touched. The full suite must
show zero regressions elsewhere. Additionally:

```bash
rg -n "CONTEXT_GLOBAL_TOP_K_COMMUNITIES|CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP|CONTEXT_GLOBAL_MEMBER_POOL_CAP" src/ tests/
```

every test referencing one of these three constants must reference it by symbol (`config.
CONTEXT_GLOBAL_TOP_K_COMMUNITIES` etc.), never a bare numeric literal standing in for its current
seeded value — mirroring `SPEC-CONTEXT-RETRIEVAL-C5-ORPHAN-ASSIGNMENT.md`'s own identical
PLACEHOLDER-acceptance-bar convention (§1 design decision, "Out of scope" above).

## Amendment 1 — §9's amendment-site list was incomplete: `test_context_budget_service.py`'s own existing whole/sub-dict shape assertions also break under §4's unconditional additive keys (OMP BLOCKED, adjudicated)

### Contradiction (as reported by OMP, before any tracked-file edit, worker dispatch, or commit)

§4 mandates that `pack_context_budget`'s return dict — **both** the all-empty early-return shape and
the normal-path return shape — unconditionally includes the three new additive surfaces
(`packed_entity_ids["community_member"]`, `dropped_entity_ids["community_member"]`, the new
top-level `community_representative_entity_ids` key, and three new `budget` sub-fields
(`community_member_truncated`, `community_member_dropped_count`,
`community_representative_reserve_tokens_used`)) on **every call**, regardless of whether a caller
passes the two new optional parameters. This is a deliberate design choice (§4's own commentary:
"Both this early-return shape and the normal-path return shape below must be kept in sync
field-for-field"), not an oversight — it mirrors how `orphan_community_reserve_tokens_used` is
already unconditionally present in today's shipped shape regardless of whether any orphan was
found, and it is what makes constraint 26's "one shared, continuously relevance-ranked pool" and
this spec's own uniform-envelope design (§1 design decision 5) hold without a caller-visible
local/global shape fork.

§9, as originally locked, named only three pre-existing test-assertion amendment sites — all three
in `tests/test_retrieve_context_wiring.py` — and said explicitly: "No other line in
`test_retrieve_context_wiring.py` changes," with the overall list framed as "the only three
pre-existing assertions in scope for editing." It never traced `pack_context_budget`'s own direct
unit tests in `tests/test_context_budget_service.py`, which exact-compare its return shape and
therefore break under §4's own locked, unconditional additive-keys design. Independently verified
against the actual current file (not merely re-read from the original pre-lock pass) — five
existing assertions break, exactly as OMP reported:

1. `test_scenario_1_all_inputs_empty_returns_zero_shape_without_opening_connection` (current lines
   97-116) — whole-`result` `assertEqual` against the old exact all-empty shape.
2. `test_scenario_2_everything_fits_under_budget` (current line 128) —
   `result["packed_entity_ids"]` exact-compared to `{"primary": primary, "expansion": expansion}`.
3. `test_scenario_2_everything_fits_under_budget` (current line 129) —
   `result["dropped_entity_ids"]` exact-compared to `{"primary": [], "expansion": []}`.
4. `test_scenario_5_conflict_only_members_are_always_included_outside_budget` (current line 211) —
   `result["packed_entity_ids"]` exact-compared to `{"primary": [], "expansion": []}`.
5. `test_scenario_10_actual_a1_and_a2_outputs_pack_distinct_pools_end_to_end` (current line 312) —
   `result["dropped_entity_ids"]` exact-compared to `{"primary": [], "expansion": []}`.

OMP correctly stopped rather than either silently widening its own edit scope past §9's literal
"only three" framing or inventing a conditional/legacy return shape to dodge the contradiction —
the latter is explicitly rejected below.

### Fix

This amendment supersedes §9's "Named amendment sites" framing: it is no longer "the only three
pre-existing assertions in scope for editing" — it is the three `test_retrieve_context_wiring.py`
sites already named, **plus** the following five `tests/test_context_budget_service.py` sites,
each amended to add exactly the new key(s) §4 mandates and no other change to the line:

**Site 4 (scenario 1, current lines 97-116)** — replace the whole `assertEqual` block with:

```python
        self.assertEqual(
            result,
            {
                "packed_entity_ids": {"primary": [], "expansion": [], "community_member": []},
                "dropped_entity_ids": {"primary": [], "expansion": [], "community_member": []},
                "conflict_only_entity_ids": [],
                "community_representative_entity_ids": [],
                "token_counts": {},
                "budget": {
                    "unit": "tokens",
                    "limit": config.CONTEXT_BUDGET_DEFAULT_TOKENS,
                    "used": 0,
                    "primary_truncated": False,
                    "primary_dropped_count": 0,
                    "expansion_truncated": False,
                    "expansion_dropped_count": 0,
                    "conflict_reserve_tokens_used": 0,
                    "orphan_community_reserve_tokens_used": 0,
                    "community_member_truncated": False,
                    "community_member_dropped_count": 0,
                    "community_representative_reserve_tokens_used": 0,
                },
            },
        )
```

**Site 5 (scenario 2, current line 128)** — replace:

```python
        self.assertEqual(result["packed_entity_ids"], {"primary": primary, "expansion": expansion})
```

with:

```python
        self.assertEqual(
            result["packed_entity_ids"],
            {"primary": primary, "expansion": expansion, "community_member": []},
        )
```

**Site 6 (scenario 2, current line 129)** — replace:

```python
        self.assertEqual(result["dropped_entity_ids"], {"primary": [], "expansion": []})
```

with:

```python
        self.assertEqual(
            result["dropped_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
```

**Site 7 (scenario 5, current line 211)** — replace:

```python
        self.assertEqual(result["packed_entity_ids"], {"primary": [], "expansion": []})
```

with:

```python
        self.assertEqual(
            result["packed_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
```

**Site 8 (scenario 10, current line 312)** — replace:

```python
        self.assertEqual(result["dropped_entity_ids"], {"primary": [], "expansion": []})
```

with:

```python
        self.assertEqual(
            result["dropped_entity_ids"], {"primary": [], "expansion": [], "community_member": []}
        )
```

No other line at any of these five sites changes — same assertion, same test logic, only the new
key(s) §4 already mandates added to each literal. A conditional/legacy return shape (making the new
keys present only when the two new parameters are actually passed) is explicitly rejected as a fix:
it would fork `pack_context_budget`'s return contract by caller intent, contradicting §4's own
explicit "kept in sync field-for-field" unconditional-shape design and breaking constraint 26's
uniform-envelope precedent for no reason other than avoiding five test-line edits already inside
this spec's own authorized file scope (§0 already permits editing `test_context_budget_service.py`
for "new scenarios" — these five lines are pre-existing scenarios whose shape this spec's own §4
change unavoidably touches, the same class of edit §9's original three sites already were).

### Also corrects: §9's scenario-numbering claim for `test_context_budget_service.py`

Independently found while adjudicating the above (re-verified against the actual current file, not
assumed): §9's original text said new scenarios continue "at 15" because the file "currently ends
at 14." This is incorrect. The file's actual current last scenario is
`test_scenario_10_actual_a1_and_a2_outputs_pack_distinct_pools_end_to_end` (there are two
same-numbered `test_scenario_9_*` methods immediately before it —
`test_scenario_9_missing_entity_row_falls_back_to_empty_content_and_zero_tokens` and
`test_scenario_9_real_empty_entity_row_normalizes_to_zero_tokens` — but no scenario past 10 exists).
New scenarios in this file must continue from **11**, not 15 — using 15 would leave a fabricated
gap (11-14 never existed). The scenario this section's new-scenario prose says to mirror,
`test_scenario_13_pack_budget_reserves_orphan_tokens_outside_ordinary_budget`, is real and exists
exactly as described, but lives in `tests/test_orphan_community_service.py`, not this file — the
original text never claimed otherwise, but is clarified here to prevent a worker searching the
wrong file for it.

### §11 Acceptance correction

§11's sentence "the three named amendment sites in §9 updated exactly as specified and no other
line touched" is superseded to read: the three `test_retrieve_context_wiring.py` sites originally
named in §9, plus this amendment's five `test_context_budget_service.py` sites — eight sites total
across both files — updated exactly as specified, no other line touched in either file.

### Scope

No change to §0's file-edit scope. `test_context_budget_service.py` was already an authorized file
under §0 ("may edit ... `tests/test_context_budget_service.py` ... new scenarios only") — this
amendment clarifies that five of its *pre-existing* scenario assertions are unavoidably touched by
§4's own already-locked design, the same category of necessary edit §9's original three sites
already were, just discovered one file later.

### Independent audit of the rest of the spec (no other contradictions found)

While adjudicating this block, traced `pack_context_budget`'s new unconditional keys through
`retrieve_context_service.py`'s actual current source (not the spec's own prose) to check whether
§5.2's "local-mode body is byte-for-byte unchanged" claim also silently breaks under §4's change,
since local mode's existing code also calls `pack_context_budget`. It does not: local mode's
existing `metadata["budget"]` construction (current lines 289-303) and its `packed_entity_ids`/
`dropped_entity_ids` consumption (current lines 149-168) both access named sub-keys explicitly
(`budget_result["budget"]["unit"]`, `budget_result["packed_entity_ids"]["primary"]`, etc.) — never
a whole-dict passthrough or spread — so the three new `budget` sub-fields and the new
`community_member`/`community_representative_entity_ids` keys are never read or forwarded by
local mode's unchanged pipeline. §5.2's claim holds; no test in `test_retrieve_context_service.py`
or `test_retrieve_context_wiring.py` needs amendment beyond §9's already-named three sites. No other
bug, missing import, or internal contradiction was found elsewhere in the spec.
