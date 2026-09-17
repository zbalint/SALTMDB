# SPEC-CONTEXT-RETRIEVAL-D4: Relative Similarity-Gap Floor on Global Representative Selection

## 1. Why

Milestone D3 (`CONTEXT_GLOBAL_SEED_SIMILARITY_GAP`, commit `4b8938a`, merged `5a14c34`) added a
relative admission floor to which leaf *communities* get seeded for `strategy="global"`. It shipped
believing `representative_reserve`/`member_pool` needed no changes of their own because "they only
ever draw from communities that already survived the new seeding gap-filter, so they inherit the
fix transitively" (D3 spec §1 decision 6, memory `9c4d6277`).

A live post-merge test this session falsified that claim (memory `6dc8924d`). Querying the live
corpus with `strategy="global"` for a genuinely narrow, single-incident question surfaced
`seed_top_k.gap_dropped_count: 0` (D3's own floor correctly found nothing to trim — the 10 K-capped
communities' centroid similarities for that query spanned only ~0.15, all comfortably inside the
0.5 gap) while still displaying clearly off-topic memories (an unrelated viewer feature, an
unrelated engineering-process write-up) as headline `community_representative` matches, ahead of
the actually-relevant content. Side-by-side against `search_memory(mode="broad")` on the identical
query, `retrieve_context(strategy="global")` was measurably *less* accurate — the user's own stated
bar ("retrieve_context should be at least as accurate as search") was not met.

**Root cause, traced to exact source** (`community_retrieval_service.py`, current lines 169-197):
a community surviving D3's seeding floor only proves its *centroid* is reasonably close to the
query's own best match — it says nothing about whether that community's single best *member* (the
entity that actually becomes its `community_representative`) is itself a good match. Once a
community is seeded, its representative candidate — carrying its own real per-query similarity,
already computed by Bug A's fix (memory `3c40dd0e`) as `top_similarity` — is admitted into
`representative_reserve` purely by slicing `representative_candidates[:CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP]`,
where the list order is still the *community's own centroid-rank order*, never re-sorted by the
representative's own similarity, and never floored at all. Contrast with `member_candidates`
(same function, line 199), which *is* sorted by each member's own real similarity before its own
cap — only the representative slot skipped this discipline. The final result list then makes this
worse structurally: `retrieve_context_service.py` (lines 368-393) always emits every
`representative_matches` entry before every `member_matches` entry, so even a member with a far
stronger real match is always displayed after every representative, including a weak one.

**Why this isn't a one-line patch, and why it stays scoped to the representative slot.** A full
grilling round with zbalint this session (Q1-Q7, all accepted) settled the shape:

1. **Scope**: fix three compounding defects together — (a) `representative_candidates` ignores its
   own real similarity and uses community-centroid rank instead; (b) no floor at all can exclude a
   weak representative; (c) the final list stays block-ordered (representatives always before
   members). `member_pool` getting its own floor is explicitly **out of scope** — it is already
   sorted by real similarity (just uncapped by a floor), lower severity, and no live evidence yet
   shows it needs one.
2. **Placeholder convention**: ship the new floor as a PLACEHOLDER constant, validated against test
   fixtures plus this session's live evidence — not a full calibration sweep, matching every
   `CONTEXT_GLOBAL_*` constant shipped so far.
3. **Floor mechanism**: a **relative gap**, not an absolute threshold, anchored to the best-admitted
   representative's own similarity this call — mirroring `CONTEXT_GLOBAL_SEED_SIMILARITY_GAP`
   exactly, for the same reason D3 chose it (`config.py`'s own NOTE on the abandoned
   `RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE` absolute floor: a fixed cosine cutoff doesn't generalize
   as the candidate pool grows).
4. **Floor-fail fallback**: a community whose representative candidate fails the floor is **not**
   dropped entirely — its other, already-independently-scored members still compete for
   `member_pool` on their own merits. This requires **no new code**: `member_candidates` is already
   built in the same loop as `representative_candidates`, unconditionally, before any cap or gap is
   applied to the representative side — confirmed directly against current source, not assumed.
5. **Representative sort mechanics**: sort `representative_candidates` by each candidate's own real
   `top_similarity` (descending, tie-break by `entity_id`) before capping — the exact same
   `(-similarity, entity_id)` pattern `member_candidates` already uses.
6. **Final list ordering**: **keep the existing block structure** (all representatives before all
   members) — do **not** interleave. With (a)+(b) fixed, the residual harm (a floor-passing-but-
   modest representative occasionally outranking a stronger member) is much smaller than what was
   diagnosed live, and interleaving would reopen D2's own locked "budget-accounted but never
   budget-gated" representative-reserve decision and touch `context_budget_service.py`'s packing
   logic — a materially larger blast radius for an unproven benefit.
7. **Boundary**: `CONTEXT_GLOBAL_SEED_SIMILARITY_GAP` (D3's own constant) is explicitly **not**
   touched by this spec, even though today's live evidence also touched on it — it was already
   independently deferred by D3's own locked spec, and bundling it in would blur the diff/spec
   boundary between two independently-reviewable changes.

## 2. `src/saltmdb/config.py`

**Exact anchor**: the existing `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` comment block and
constant (current lines 408-423), immediately before the `CONTEXT_GLOBAL_MEMBER_POOL_CAP` comment
block (current line 425).

Before:
```python
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
```

After:
```python
# Milestone D (wayfinder ticket "Milestone D retrieval/synthesis mechanics," standing constraint 26,
# memory 51127287) -- strategy:"global" retrieve_context's representative-slot reserve: how many of
# the CONTEXT_GLOBAL_TOP_K_COMMUNITIES seeded leaf communities actually get their own constraint-19
# representative force-included as a guaranteed, budget-accounted (but never budget-gated) slot.
# Milestone D4 fix (SALTMDB memory 6dc8924d, live DNS repro this same session) -- ranked by each
# admitted representative's own real per-query similarity (post Bug A fix, memory 3c40dd0e), NOT by
# its community's centroid/seed-rank similarity -- the weakest-matching representatives are dropped
# first if the eligible count exceeds this cap, and CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP
# below can additionally shrink this window before the cap is even reached. Independently capped
# from CONTEXT_GLOBAL_MEMBER_POOL_CAP below -- a community whose representative candidate is dropped
# here (by either the cap or the gap) still contributes its own other members to the member pool on
# equal footing; the dropped representative candidate itself is never redirected into the member
# pool (this constant's own pre-existing cap-independent behavior, unchanged by the D4 fix).
# PLACEHOLDER: not yet benchmarked against SALTMDB's own corpus -- seeded at the same order of
# magnitude as CONTEXT_EXPANSION_ORPHAN_COMMUNITY_CAP's own current value, mirroring that constant's
# own "sparse force-include mechanism" shape, not derived from any benchmark of its own. Recalibrated
# by the Milestone D benchmark run (wayfinder ticket f3f03936, standing constraint 28) via its own
# concrete quantitative sweep against the live corpus. Do not remove the placeholder framing when
# tuning this; replace this comment with the benchmark citation once a real value is locked.
CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP = 5
# Milestone D4 fix (SALTMDB memory 6dc8924d, live DNS repro this same session) -- strategy:"global"
# retrieve_context's representative-slot relative admission gate: an admitted representative
# candidate is kept only if its own real per-query similarity is within this absolute gap of the
# single best-matching representative's own similarity in the same call. The best representative
# itself is always force-included regardless of its own absolute similarity (its own gap-to-itself
# is always 0, so it is never subject to this gap), so this floor can only ever shrink the existing
# CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP window, never produce an empty representative_reserve on
# its own when at least one eligible representative candidate exists. This mirrors
# CONTEXT_GLOBAL_SEED_SIMILARITY_GAP above exactly -- same ABSOLUTE GAP (not FIXED absolute floor)
# shape and the same generalization reasoning (see that constant's own comment and the
# RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE removal NOTE below RERANK_GAP_SKIP_RATIO) -- just applied one
# stage later, to each representative's own real per-query similarity instead of its community's
# centroid similarity. Closes the gap D3 (memory 9c4d6277's decision 6) left open: a community
# relevant enough to be seeded does not guarantee its own single best member is itself a strong
# match, and representative_reserve previously had no floor of its own at all. PLACEHOLDER: not yet
# benchmarked against SALTMDB's own live corpus -- seeded at the same value as
# CONTEXT_GLOBAL_SEED_SIMILARITY_GAP for consistency, validated by hand against every existing
# test_community_retrieval_service.py scenario before locking (see this spec's own pre-lock gate).
# Recalibrate via a real benchmark sweep before treating this as final; do not remove the placeholder
# framing when tuning it.
CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP = 0.5
```

No other line in this file changes.

## 3. `src/saltmdb/domain/services/community_retrieval_service.py`

### 3.1 Import block (current lines 9-14)

Before:
```python
from saltmdb.config import (
    CONTEXT_GLOBAL_MEMBER_POOL_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
    CONTEXT_GLOBAL_SEED_SIMILARITY_GAP,
    CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
    get_db_path,
)
```

After:
```python
from saltmdb.config import (
    CONTEXT_GLOBAL_MEMBER_POOL_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP,
    CONTEXT_GLOBAL_SEED_SIMILARITY_GAP,
    CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
    get_db_path,
)
```

### 3.2 `_empty_result` (current lines 28-39)

Before:
```python
def _empty_result() -> dict[str, Any]:
    zero_cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
    return {
        "representative_matches": [],
        "member_matches": [],
        "seed_cap": {"cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **zero_cap, "gap_dropped_count": 0},
        "representative_reserve": {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            **zero_cap,
        },
        "member_pool": {"cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP, **zero_cap},
    }
```

After (only the `representative_reserve` line changes):
```python
def _empty_result() -> dict[str, Any]:
    zero_cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
    return {
        "representative_matches": [],
        "member_matches": [],
        "seed_cap": {"cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **zero_cap, "gap_dropped_count": 0},
        "representative_reserve": {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            **zero_cap,
            "gap_dropped_count": 0,
        },
        "member_pool": {"cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP, **zero_cap},
    }
```

### 3.3 Docstring (current lines 48-65)

Append one paragraph after the D3-added paragraph, before the closing `"""`:

```python
    Representative selection itself is then gated by a second relative similarity floor: after
    ranking every seeded community's own best-matching member by that member's real per-query
    similarity and capping at CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP, the single best-ranked
    representative candidate is always admitted regardless of its own absolute similarity, and
    every subsequent representative candidate is admitted only while its own similarity stays
    within CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP of the best one's own similarity. This
    floor can only shrink the representative-reserve window, never grow it, and can never produce
    zero admitted representatives when at least one eligible representative candidate exists. A
    community whose representative candidate is excluded by either the cap or this gap still
    contributes its own other members to the shared member pool, unaffected.
    """
```

### 3.4 Representative selection + `representative_reserve` construction (current lines 169-197)

Before:
```python
        # For each seeded community (in centroid-seed rank order), the single best per-query match
        # becomes that community's representative for this response; every other scored member of
        # that community falls through to the shared member pool below. A community contributes
        # nothing to either list if none of its members have an embedding row.
        representative_candidates: list[tuple[str, str, float]] = []
        member_candidates: list[tuple[str, str, float]] = []
        for community_id, _centroid_similarity in seeded:
            scored = scored_by_community.get(community_id, [])
            if not scored:
                continue
            scored.sort(key=lambda item: (-item[1], item[0]))
            top_entity_id, top_similarity = scored[0]
            representative_candidates.append((top_entity_id, community_id, top_similarity))
            member_candidates.extend(
                (entity_id, community_id, similarity) for entity_id, similarity in scored[1:]
            )

        representative_eligible_count = len(representative_candidates)
        representative_admitted = representative_candidates[
            :CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
        ]
        representative_reserve = {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            "eligible_count": representative_eligible_count,
            "truncated": representative_eligible_count > CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            "dropped_count": max(
                0, representative_eligible_count - CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
            ),
        }
```

After:
```python
        # For each seeded community (in centroid-seed rank order), the single best per-query match
        # becomes that community's representative candidate for this response; every other scored
        # member of that community falls through to the shared member pool below, unconditionally --
        # independent of whatever happens to its own community's representative candidate afterward.
        # A community contributes nothing to either list if none of its members have an embedding row.
        representative_candidates: list[tuple[str, str, float]] = []
        member_candidates: list[tuple[str, str, float]] = []
        for community_id, _centroid_similarity in seeded:
            scored = scored_by_community.get(community_id, [])
            if not scored:
                continue
            scored.sort(key=lambda item: (-item[1], item[0]))
            top_entity_id, top_similarity = scored[0]
            representative_candidates.append((top_entity_id, community_id, top_similarity))
            member_candidates.extend(
                (entity_id, community_id, similarity) for entity_id, similarity in scored[1:]
            )

        # Relative admission gap (D4 fix, SALTMDB memory 6dc8924d): representative_candidates is
        # ranked by each candidate's own real per-query similarity -- not by its community's
        # centroid/seed rank -- mirroring member_candidates' own sort exactly. The best-ranked
        # candidate (representative_capped[0]) is always admitted regardless of its own absolute
        # similarity -- this floor can only shrink the reserve-cap window, never produce an empty
        # representative_reserve. Every subsequent capped candidate, in similarity-descending order,
        # is admitted only while it stays within CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP of the
        # best one's own similarity; the first candidate that violates the gap ends the admitted
        # prefix (every later entry has equal or lower similarity, so it would violate the gap too).
        representative_candidates.sort(key=lambda item: (-item[2], item[0]))
        representative_eligible_count = len(representative_candidates)
        representative_capped = representative_candidates[
            :CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
        ]
        representative_admitted: list[tuple[str, str, float]] = []
        for entity_id, community_id, similarity in representative_capped:
            if representative_admitted and (
                representative_capped[0][2] - similarity
                > CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP
            ):
                break
            representative_admitted.append((entity_id, community_id, similarity))
        representative_reserve = {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            "eligible_count": representative_eligible_count,
            "truncated": representative_eligible_count > CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
            or len(representative_admitted) < len(representative_capped),
            "dropped_count": max(
                0, representative_eligible_count - CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP
            ),
            "gap_dropped_count": len(representative_capped) - len(representative_admitted),
        }
```

No other line in this file changes. `representative_admitted` keeps its existing type
(`list[tuple[str, str, float]]`) and its existing downstream consumers (current lines 209, 235, 247
— `admitted_ids`, the `representative_matches` list comprehension, and the `representative_reserve`
passthrough) are unaffected by this rename-in-place: the variable is reassigned from a plain slice
to the gap-filtered list, same shape, same name.

## 4. `tests/test_community_retrieval_service.py`

### 4.1 `_zero_shape()` helper (current lines 130-146)

Before:
```python
    @staticmethod
    def _zero_shape() -> dict[str, Any]:
        cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
        return {
            "representative_matches": [],
            "member_matches": [],
            "seed_cap": {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                **cap,
                "gap_dropped_count": 0,
            },
            "representative_reserve": {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                **cap,
            },
            "member_pool": {"cap": config.CONTEXT_GLOBAL_MEMBER_POOL_CAP, **cap},
        }
```

After (only the `representative_reserve` line changes):
```python
    @staticmethod
    def _zero_shape() -> dict[str, Any]:
        cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
        return {
            "representative_matches": [],
            "member_matches": [],
            "seed_cap": {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                **cap,
                "gap_dropped_count": 0,
            },
            "representative_reserve": {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                **cap,
                "gap_dropped_count": 0,
            },
            "member_pool": {"cap": config.CONTEXT_GLOBAL_MEMBER_POOL_CAP, **cap},
        }
```

This is read by `test_scenario_01_zero_communities_returns_empty_shape` and
`test_scenario_02_blank_query_returns_empty_shape_without_embedding` — both pass unchanged once
this helper matches the new `_empty_result()` shape from §3.2.

### 4.2 `test_scenario_04_top_k_seeding_truncates_by_centroid_similarity` (current lines 196-232) — assertion relaxed to order-independent

**Verified** (do not re-derive from a different search than this one): both `community-a` and
`community-b`'s `"{community_id} representative"` fixture member sits at `_axis_vector(0)` — an
exact match to the query, which is also `_axis_vector(0)`. Both communities' representative
candidates therefore tie at `top_similarity == 1.0`. Before this spec, `representative_candidates`
preserved seed/centroid-rank order (community-a's centroid 1.0 ranks above community-b's 0.8), so
`representative_matches` was incidentally, deterministically `[community-a's rep, community-b's
rep]`. After §3.4's sort-by-own-similarity, a tie is broken by `entity_id` — a random UUID from
`store_memory` — so the order between these two specific entries is no longer deterministic. This
test's actual subject is top-K **seeding** truncation (proving `community-c`, centroid 0.2, never
gets seeded at all under `CONTEXT_GLOBAL_TOP_K_COMMUNITIES=2`) — it was never actually asserting
representative-level ordering on purpose, so the fix here is to make the assertion order-independent
rather than touch the fixture. Change the existing assertion (current lines 229-232):

Before:
```python
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            representatives[:2],
        )
```

After:
```python
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            representatives[:2],
        )
```

The `seed_cap` assertion above it (current lines 219-228, already `gap_dropped_count`-bearing from
D3) is untouched.

### 4.3 `test_scenario_06_representative_and_member_caps_are_independent` (current lines 256-298) — rewritten

**Verified**: this scenario's current fixture gives every community's `"{community_id}
representative"` member the same `_axis_vector(0)` exact-match vector, so all three tie at
`top_similarity == 1.0` — under §3.4's new sort, the single admitted representative
(`CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` patched to 1) becomes an `entity_id` tie-break,
non-deterministic, breaking the current exact-identity assertion. This scenario is also the natural
place to positively demonstrate §3.4's core mechanism — that representative selection is now driven
by each candidate's own real similarity, independent of (here, deliberately inverted from) its
community's centroid rank — while preserving the scenario's original purpose (the reserve cap and
the member-pool cap operate independently). Replace the entire method:

Before:
```python
    def test_scenario_06_representative_and_member_caps_are_independent(self):
        representatives = []
        lower_member_ids = []
        for community_id, similarity in (
            ("community-a", 1.0),
            ("community-b", 0.8),
            ("community-c", 0.6),
        ):
            representative, member_ids = self._insert_community(
                community_id,
                [
                    (f"{community_id} representative", _axis_vector(0)),
                    (f"{community_id} member", _cosine_vector(0.3)),
                ],
                _cosine_vector(similarity),
            )
            representatives.append(representative)
            lower_member_ids.append(member_ids[1])

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 3):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP", 1
            ):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            representatives[:1],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": 1,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 2,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertTrue(set(lower_member_ids[1:]).issubset(member_ids))
```

After:
```python
    def test_scenario_06_representative_and_member_caps_are_independent(self):
        _, first_ids = self._insert_community(
            "community-a",
            [("A representative", _cosine_vector(0.5)), ("A member", _cosine_vector(0.3))],
            _cosine_vector(1.0),
        )
        _, second_ids = self._insert_community(
            "community-b",
            [("B representative", _cosine_vector(0.9)), ("B member", _cosine_vector(0.3))],
            _cosine_vector(0.8),
        )
        _, third_ids = self._insert_community(
            "community-c",
            [("C representative", _cosine_vector(0.7)), ("C member", _cosine_vector(0.3))],
            _cosine_vector(0.6),
        )

        with patch.object(community_retrieval_service, "CONTEXT_GLOBAL_TOP_K_COMMUNITIES", 3):
            with patch.object(
                community_retrieval_service, "CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP", 2
            ):
                with patch.object(
                    community_retrieval_service, "embed_text", return_value=_axis_vector(0)
                ):
                    result = seed_and_rank_communities("community query", db_connection=self.conn)

        # Representative selection is driven entirely by each candidate's own real per-query
        # similarity (0.5/0.9/0.7), not its community's centroid rank (1.0/0.8/0.6) -- community-b's
        # representative (0.9) and community-c's representative (0.7) win the 2 reserve slots despite
        # community-a having the single highest centroid; community-a's representative (0.5, weakest)
        # is dropped by the cap, but its own other member still surfaces via the independent member
        # pool.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [second_ids[0], third_ids[0]],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": 2,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 1,
                "gap_dropped_count": 0,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertIn(first_ids[1], member_ids)
```

### 4.4 Scenarios 07/08/10/12 — verified unaffected, do not edit

Re-verified against current source (not assumed): `test_scenario_07_member_pool_is_shared_and_truncated_by_query_similarity`
seeds two communities but never patches `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` (stays default
5, both candidates admitted regardless of sort order) and never asserts `representative_matches`
order or `representative_reserve` at all. `test_scenario_08_missing_member_embedding_is_excluded`
and `test_scenario_10_null_title_is_reported_as_unknown` each seed exactly one community (no
cross-candidate ordering possible). `test_scenario_12_top_ranked_community_seeded_despite_weak_absolute_similarity`
has exactly one seeded community after D3's own seed-gap excludes the second, so its single
representative candidate is trivially, unambiguously admitted regardless of this spec's sort. **Do
not touch these four tests** — if a diff touches any of them, stop and report why, since this
spec's own verification found no reason to.

### 4.5 `test_scenario_09_ties_break_by_community_and_entity_id` (current lines 374-406) — two assertions relaxed to order-independent

**Verified**: both communities' per-query-winning members (`"A member"`/`"B member"`, i.e.
`first_ids[1]`/`second_ids[1]`) sit at `_axis_vector(0)`, tying at `top_similarity == 1.0` across
communities — the same collision as §4.2/§4.3. This scenario's actual subject (per its own name and
inline comment) is the **member-pool** tie-break for the two *fixed, orthogonal* representatives
that lose their own community's per-query race (asserted via `sorted([first_ids[0], second_ids[0]])`
on `member_matches`, current lines 403-406) — that assertion is untouched. Only the two
representative-side assertions above it need relaxing. Change (current lines 395-402):

Before:
```python
        self.assertEqual(
            [match["community_id"] for match in result["representative_matches"]],
            ["community-a", "community-b"],
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_ids[1], second_ids[1]],
        )
```

After:
```python
        self.assertCountEqual(
            [match["community_id"] for match in result["representative_matches"]],
            ["community-a", "community-b"],
        )
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_ids[1], second_ids[1]],
        )
```

### 4.6 `test_scenario_11_communities_within_gap_all_seeded` (current lines 428-459) — assertion relaxed to order-independent

**Verified**: both communities' `"A representative"`/`"B representative"` fixture members sit at
`_axis_vector(0)`, tying at `top_similarity == 1.0` — same collision shape as §4.2/§4.3/§4.5. This
scenario's subject is D3's own community-**seeding** gap (asserted via `seed_cap`, current lines
446-455, untouched), not representative ordering. Change the existing assertion (current lines
456-459):

Before:
```python
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
        )
```

After:
```python
        self.assertCountEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
        )
```

### 4.7 New scenarios, appended after `test_scenario_12_top_ranked_community_seeded_despite_weak_absolute_similarity`

(current lines 461-493), before `if __name__ == "__main__":`. Both are required — they cover the
two behaviors this spec's own §1 design decisions 3 and 4 must not silently regress on later (a weak
second representative genuinely gets excluded and its community's other member still surfaces; the
single best representative is never excluded even when absolutely weak):

```python
    def test_scenario_13_representative_gap_floor_excludes_weak_second_representative(self):
        first_rep, first_ids = self._insert_community(
            "community-a",
            [("A representative", _cosine_vector(1.0)), ("A member", _cosine_vector(0.5))],
            _cosine_vector(1.0),
        )
        _, second_ids = self._insert_community(
            "community-b",
            [("B representative", _cosine_vector(0.3)), ("B member", _cosine_vector(0.1))],
            _cosine_vector(0.6),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # Both communities are seeded (centroid gap 1.0-0.6=0.4 <= CONTEXT_GLOBAL_SEED_SIMILARITY_GAP,
        # 0.5) -- D3's community-level floor admits both. But community-b's own best member (0.3) is
        # 0.7 away from community-a's own best member (1.0), exceeding
        # CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP (0.5) -- its representative candidate is
        # excluded from representative_matches. community-b's own dropped representative candidate is
        # never redirected into the member pool (this cap's pre-existing, unchanged cap-independent
        # behavior) -- but "B member" (0.1), always separately scored, still competes for member_pool
        # on its own merits.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        member_ids = [match["entity_id"] for match in result["member_matches"]]
        self.assertIn(second_ids[1], member_ids)
        self.assertNotIn(second_ids[0], member_ids)

    def test_scenario_14_weakest_possible_representative_still_admitted_when_sole_candidate(self):
        weak_rep, _ = self._insert_community(
            "community-a",
            [("Weak representative", _cosine_vector(0.02)), ("Weak member", _cosine_vector(0.01))],
            _cosine_vector(0.02),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # A single, extremely weak community/representative pair (similarity 0.02) -- the
        # representative gap floor compares each candidate to the best ADMITTED representative this
        # call, and the single best one's own gap-to-itself is always 0, so it is force-included
        # regardless of how weak its absolute similarity is. Mirrors scenario 12's proof of the same
        # guarantee at the community-seeding stage, one level down.
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [weak_rep],
        )
        self.assertEqual(
            result["representative_reserve"],
            {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                "eligible_count": 1,
                "truncated": False,
                "dropped_count": 0,
                "gap_dropped_count": 0,
            },
        )
```

## 5. `tests/test_retrieve_context_service.py`

Two `representative_reserve` dict literals assert the full shape via exact dict equality (verified:
these are the only two `representative_reserve`/`strategy="global"` sites in this file — checked
with `rg -n "representative_reserve"` and `rg -n 'strategy="global"'` against current source, both
matching D3's own §5 verification of the sibling `seed_top_k` sites). Neither scenario seeds more
than one community, so the new gap floor never drops anything in either case — both need only the
additive key.

### 5.1 `test_global_populated_leaf_community_...` (current lines 792-797)

Before:
```python
                "representative_reserve": {
                    "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                    "eligible_count": 1,
                    "truncated": False,
                    "dropped_count": 0,
                },
```

After:
```python
                "representative_reserve": {
                    "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                    "eligible_count": 1,
                    "truncated": False,
                    "dropped_count": 0,
                    "gap_dropped_count": 0,
                },
```

### 5.2 `test_global_without_communities_returns_empty_hierarchy_envelope` (current lines 848-853)

Before:
```python
                        "representative_reserve": {
                            "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                            "eligible_count": 0,
                            "truncated": False,
                            "dropped_count": 0,
                        },
```

After:
```python
                        "representative_reserve": {
                            "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                            "eligible_count": 0,
                            "truncated": False,
                            "dropped_count": 0,
                            "gap_dropped_count": 0,
                        },
```

No other line in this file changes.

## 6. Out of scope

- `member_pool`'s own relevance floor — per §1 design decision 1, explicitly deferred; it is already
  sorted by real per-query similarity (just uncapped by a floor), lower severity, no live evidence
  yet shows it needs one.
- Interleaving `representative_matches` and `member_matches` into one flat cross-population
  relevance-sorted list, or any other change to the final block-ordering (`retrieve_context_service.py`
  current lines 368-393) — per §1 design decision 6, explicitly deferred; keeping block-order is the
  smaller, already-sufficient fix.
- `retrieve_context_service.py` itself — its `"representative_reserve": seed_result["representative_reserve"]`
  passthrough (current line 411) is already correct as-is (verified directly, pure passthrough, no
  reconstruction of the dict shape); only its *tests'* literal dict copies (§5) need the new key.
- `context_budget_service.py` — verified directly: it consumes `representative_matches`' entity-id
  list for token accounting (`community_representative_reserve_tokens_used`) and never reconstructs
  or duplicates `representative_reserve`'s own shape; unaffected by an additive key on that dict and
  automatically consistent with whichever entity IDs this spec's fix admits.
- `orphan_community_service.py`, `context_expansion_service.py`, `conflict_set_service.py`,
  `lineage_assembly_service.py` — none read or construct `representative_reserve`.
- `tests/test_context_budget_service.py`, `tests/test_orphan_community_service.py` — verified
  directly: neither references `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` or
  `representative_reserve`'s dict shape at all, only the separately-named
  `community_representative_reserve_tokens_used` token-count field.
- `mcp/tools.py`, `daemon/dispatch.py`, `daemon/protocol.py` — no MCP-surface or wire-protocol
  change; the new field is additive inside an already-passthrough dict.
- `CONTEXT_GLOBAL_SEED_SIMILARITY_GAP` (D3's own constant) — per §1 design decision 7, explicitly
  untouched; already independently deferred by D3's own locked spec.
- Renaming, removing, or changing the meaning of the existing `dropped_count` key anywhere in this
  codebase — it keeps its current K-cap-only meaning on every cap dict that has it, including
  `representative_reserve`.
- Recalibrating `CONTEXT_GLOBAL_TOP_K_COMMUNITIES` or `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP`'s
  own existing values — separate future benchmark work.
- A real benchmark sweep to calibrate `CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP`'s final value —
  shipped as an explicitly-marked placeholder per §1 design decision 2; do not attempt to "improve"
  the value in this pass.
- `tests/test_community_retrieval_service.py` scenarios 01, 02 (helper-only touch, §4.1), 03, 05, 07,
  08, 10, 12 — verified unaffected (§4.4); do not edit beyond what §4.1/§4.2/§4.3/§4.5/§4.6 already
  require.
- `specs/SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL.md` — a historical, already-implemented,
  already-merged spec document; it still contains the pre-D4 comment wording this spec's §2
  supersedes in live source, but historical spec files are records of what was locked at the time,
  never retroactively edited.

## 7. Acceptance

```
python -m pytest tests/test_community_retrieval_service.py tests/test_retrieve_context_service.py -v
```
Must exit 0. Every pre-existing scenario in both files must still pass; the only edited assertions
are the ones named in §4.1, §4.2, §4.3, §4.5, §4.6, §5.1, §5.2 (scenario 06 rewritten per §4.3). New
scenarios 13 and 14 (§4.7) must be present and passing.

```
python -m pytest -q
```
Full suite must exit 0 with zero regressions elsewhere.

```
ruff check src/saltmdb/config.py src/saltmdb/domain/services/community_retrieval_service.py tests/test_community_retrieval_service.py tests/test_retrieve_context_service.py
ruff format --check src/saltmdb/config.py src/saltmdb/domain/services/community_retrieval_service.py tests/test_community_retrieval_service.py tests/test_retrieve_context_service.py
mypy src/saltmdb/domain/services/community_retrieval_service.py
```
All three must exit 0.

```
git diff --stat
```
Must show exactly these four files changed: `src/saltmdb/config.py`,
`src/saltmdb/domain/services/community_retrieval_service.py`,
`tests/test_community_retrieval_service.py`, `tests/test_retrieve_context_service.py`. No other
file (including this spec file, already committed separately before this diff) may appear.

## 0. Status

**LOCKED.**

**Scope**: may edit `src/saltmdb/config.py` (§2, one new constant plus one comment rewrite),
`src/saltmdb/domain/services/community_retrieval_service.py` (§3), and
`tests/test_community_retrieval_service.py` (§4) and `tests/test_retrieve_context_service.py`
(§5) only. Does not touch `retrieve_context_service.py`'s own source, `context_budget_service.py`,
`orphan_community_service.py`, `context_expansion_service.py`, `conflict_set_service.py`,
`lineage_assembly_service.py`, `mcp/tools.py`, `daemon/dispatch.py`, `daemon/protocol.py`,
`CONTEXT_GLOBAL_SEED_SIMILARITY_GAP`, or any other spec file.

**Pre-lock gate, run against the current tree (this session, 2026-09-17):**
1. Drafted §1-§7 in full before writing this section, in that order.
2. §7's four commands are exact, targeted, runnable invocations OMP will run itself per Coding
   Standards rule 18; not run speculatively here as a build (per this workspace's
   verifying-a-spec-never-means-building-the-implementation rule) — but every fact they depend on
   (line numbers, existing test bodies, exact current dict literals, exact tuple index positions in
   `representative_candidates`) was read directly from the current tree in this session, not
   assumed.
3. Ran `grep -rln "representative_reserve|CONTEXT_GLOBAL_REPRESENTATIVE" src/ tests/` against the
   whole tree (not just the two files this spec already knew about) specifically to catch any other
   consumer of `representative_reserve`'s shape or the reserve-cap constant before finalizing scope.
   Found `context_budget_service.py` (token-count field only, different name, confirmed via direct
   read of both it and `retrieve_context_service.py`'s own passthrough line), plus
   `tests/test_context_budget_service.py`/`tests/test_orphan_community_service.py` (same
   token-count field only, confirmed via a second targeted grep for the reserve-cap constant name in
   those three files specifically — zero matches) — confirms §1 decision 4's "no new code needed"
   reasoning and confirms none of those three files need editing.
4. No package-manager/codegen output involved; this is a hand-written source + constant + test
   change with no regenerated lockfile or manifest.
5. Grepped for the OLD `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` comment text ("Ranked by each
   seeded community's own seed similarity") across the whole tree, not just the file/symbol name
   being changed — found it in exactly two places: `config.py` itself (edited by §2) and
   `specs/SPEC-CONTEXT-RETRIEVAL-D2-GLOBAL-RETRIEVAL.md` (a historical, already-merged spec document,
   correctly left untouched per §6's own explicit entry — historical specs are records of what was
   locked at the time, never retroactively edited).
6. Opened and read every "does not touch" file's actual current content for
   `representative_reserve`/`CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` references (§3 of this gate
   run) — confirmed none of them read or construct `representative_reserve`'s own shape.
7. `representative_reserve`'s new shape (the additive `gap_dropped_count` key, and the new
   `truncated` OR-condition) is stated in three places in this spec (§2's config comment prose,
   §3.4's code block, and §4.1/§5's test literals) — diffed all three against each other:
   `gap_dropped_count` name and `0`/computed-value semantics agree everywhere; `dropped_count`'s
   K-cap-only meaning is unchanged and stated identically in §1 decision 4 and §3.4's code.
8. Concrete instance walked through end-to-end against every other locked decision, mirroring D3's
   own gate step 8: scenario 06's redesigned 3-community, similarity-{0.5, 0.9, 0.7} shape (centroid
   rank {1.0, 0.8, 0.6}, deliberately inverted from representative-similarity rank) — decision 5
   (sort by own similarity) picks community-b (0.9) and community-c (0.7) as the top-2 candidates,
   NOT community-a despite its highest centroid; decision 3/§3.4's gap check (0.9-0.7=0.2 <= 0.5)
   confirms both survive the gap once capped-to-2; decision 4 (members-only survival) means
   community-a's OTHER member ("A member", not its dropped representative candidate) still appears in
   `member_matches` — all four decisions agree on the same single outcome for this instance, and this
   is exactly what §4.3's rewritten assertion asserts. Separately walked scenario 13's 2-community,
   similarity-{1.0, 0.3} shape (gap 0.7 > 0.5): decision 3 excludes community-b's representative
   candidate; decision 4 confirms community-b's *other* member ("B member", 0.1) still surfaces while
   community-b's *dropped representative candidate itself* ("B representative", 0.3) does not appear
   in either list — this second concrete instance is what distinguishes decision 4's exact claim
   ("its other members survive," not "the dropped candidate itself gets redirected") and is asserted
   explicitly via the `assertNotIn(second_ids[0], member_ids)` check.
9. `CONTEXT_GLOBAL_REPRESENTATIVE_SIMILARITY_GAP = 0.5`'s interaction with every *existing* test's
   actual current fixture values was individually recomputed against current source (not assumed
   compatible): scenarios 04/09/11 all tie at `top_similarity == 1.0` across every seeded community
   in each (gap always exactly 0, trivially within any positive threshold) — the new gap mechanism
   never excludes anything in these three, only the now-nondeterministic *order* of a tie needed
   addressing (§4.2/§4.5/§4.6). Scenario 06's redesigned fixture (gap 0.9-0.7=0.2, within 0.5) and
   scenario 13's new fixture (gap 1.0-0.3=0.7, exceeds 0.5) were each individually computed by hand
   against the exact cosine values chosen, not assumed compatible with the placeholder value.
