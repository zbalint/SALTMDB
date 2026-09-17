# SPEC-CONTEXT-RETRIEVAL-D3: Relative Similarity-Gap Floor on Global Community Seeding

## 1. Why

Milestone D2 (global/community retrieval, `strategy="global"`) shipped and merged into
`context-aware-search`. A post-ship benchmark session (constraint-28/`f3f03936` mandatory
validation probes, memory `510c19ff`) ran four probes against `seed_and_rank_communities` in
`src/saltmdb/domain/services/community_retrieval_service.py` and found two independently-confirmed
defects, both spec-compliant behavior (not coding bugs) per constraints 19/26/27 as originally
locked. Bug A (query-independent representative selection) was already fixed and shipped at
`99086ff` (memory `3c40dd0e`). This spec fixes Bug B.

**Bug B — no relevance floor on community seeding.** `seed_and_rank_communities` (current source,
lines 90-99):

```python
        eligible_count = len(ranked_communities)
        seeded = ranked_communities[:CONTEXT_GLOBAL_TOP_K_COMMUNITIES]
        seed_cap = {
            "cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "eligible_count": eligible_count,
            "truncated": eligible_count > CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "dropped_count": max(0, eligible_count - CONTEXT_GLOBAL_TOP_K_COMMUNITIES),
        }
        if not seeded:
            return _empty_result()
```

is a pure top-K-by-rank slice with no minimum-similarity check anywhere. Probe 3 (memory
`510c19ff`, isolated synthetic DB) proved this cleanly: with only 3 leaf communities total and
`CONTEXT_GLOBAL_TOP_K_COMMUNITIES=10`, two sub-communities at **exactly 0.0 cosine similarity**
(fully orthogonal to the query vector) were still admitted as seeds and surfaced as
`community_representative`/`community_member` matches, purely because nothing in the corpus
exceeded the K-cap. On the live corpus this manifests as ~40+ weakly-matched communities diluting
results whenever fewer than `CONTEXT_GLOBAL_TOP_K_COMMUNITIES` genuinely relevant communities
exist (live DNS-query repro, memory `bb609f00`: 54 eligible communities, only a handful relevant).

**Why this isn't a one-line patch.** `config.py`'s own constraint-27 comment states the missing
floor was a *deliberate* choice: `local` strategy's strict, multi-signal abstention gate (memory
`b9b75764` — requires strong lexical support or dual-channel FTS+vector corroboration, not a raw
similarity threshold) already caused a real false-negative production bug, returning `[]` for a
legitimate broad natural-language query (the original DNS-incident query, memory
`e957aa78`/`33cb492f` — zbalint: *"there is no value in retrieve_context if it does not retrieve
the context"*). Constraint 27 explicitly avoided adding "a second uncalibrated threshold in the
same subsystem for the same reason." Separately, this file's own `NOTE` (below
`RERANK_GAP_SKIP_RATIO`, current lines ~461-478) documents that a *fixed absolute* similarity/
distance floor was tried once already for the entity-level relevance gate
(`RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE`) and abandoned after a 21k-entity holdout proved it doesn't
generalize — an unrelated query's nearest-neighbor distance at that scale overlapped the positive
class fully. A naive fixed floor on community-centroid similarity risks repeating exactly that
failure as the corpus/community count grows.

**Resolved design (grilling session, all 6 questions answered by zbalint this session — no
open branches remain):**

1. Fix scope: a **relative, gap-based** admission floor at the community-seeding stage only (not
   an absolute floor, not "no gate," not dynamic-K alone).
2. The **rank-1** (highest centroid-similarity) community is **always admitted**, regardless of
   its own absolute similarity — this structurally forecloses the false-negative failure mode
   above; the fix can never produce an empty seed set on its own.
3. Gap metric is **absolute difference** (`top_similarity - candidate_similarity`), not a ratio —
   a ratio is unstable near zero/negative similarities (e.g. top=0.05/candidate=0.03 gives the same
   ratio as top=0.80/candidate=0.48). An absolute gap anchored to *this call's own* top match is a
   structurally different shape from the abandoned *fixed absolute floor on raw similarity* above:
   it scales with whatever this specific query's own similarity landscape looks like, rather than
   applying one static cutoff regardless of corpus/query shape.
4. Ship a new **PLACEHOLDER** constant now (`CONTEXT_GLOBAL_SEED_SIMILARITY_GAP = 0.5`), following
   this file's existing `CONTEXT_GLOBAL_*` convention (seed now, benchmark-calibrate later — see
   §2's value justification). Not blocking this fix on a full calibration sweep.
5. Diagnostics: **add** a new `gap_dropped_count` field to `seed_cap`, additive only.
   `dropped_count` keeps its existing K-cap-only meaning, unchanged — `dropped_count` is a
   codebase-wide shared shape (`{cap, eligible_count, truncated, dropped_count}`) reused
   byte-identically in `orphan_community_service.py`, `context_expansion_service.py`, and
   `conflict_set_service.py`, and consumed verbatim by `retrieve_context_service.py`'s own
   metadata passthrough (`"seed_top_k": seed_result["seed_cap"]`, line 410) and asserted via exact
   full-dict equality in `tests/test_retrieve_context_service.py`. Renaming it would be a much
   wider-blast-radius change than what was actually approved; this spec is additive only.
6. Scope: the gap floor applies **only** to community seeding
   (`community_retrieval_service.py`'s `seed_and_rank_communities`). `representative_reserve` and
   `member_pool` (lines ~164-185) are **not** touched — they only ever draw from communities that
   already survived the new seeding gap-filter, so they inherit the fix transitively.

## 2. `src/saltmdb/config.py`

**Exact anchor**: immediately after the `CONTEXT_GLOBAL_TOP_K_COMMUNITIES = 10` block (current
line 384), before the `CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP` comment block (current line
386). Insert:

```python
# Milestone D2 Bug B fix (SALTMDB memory 510c19ff, live DNS repro bb609f00) -- strategy:"global"
# retrieve_context's community-seeding relative admission gate: a candidate leaf community is
# admitted only if its centroid-to-query cosine similarity is within this absolute gap of the
# single best-matching (rank-1) community's own similarity in the same call. Rank-1 itself is
# always force-included regardless of its own absolute similarity (never subject to this gap), so
# this floor can only ever shrink the existing CONTEXT_GLOBAL_TOP_K_COMMUNITIES window, never
# produce an empty seed set on its own -- structurally avoiding the exact false-negative failure
# mode (local strategy's strict abstention gate, memory b9b75764, original incident
# e957aa78/33cb492f) that constraint 27's original "no floor in v1" choice was written to avoid
# repeating. This is an ABSOLUTE GAP anchored to this call's own top match, not a FIXED absolute
# similarity floor on raw centroid similarity -- the fixed-absolute-floor shape was already tried
# and abandoned once for the entity-level relevance gate elsewhere in this file (see the
# RELEVANCE_GATE_MAX_SEMANTIC_DISTANCE removal NOTE below RERANK_GAP_SKIP_RATIO: an absolute
# cosine-distance/-similarity cutoff does not generalize as candidate-pool size grows). PLACEHOLDER:
# not yet benchmarked against SALTMDB's own live corpus -- seeded by direct verification against
# every existing test_community_retrieval_service.py scenario at this value: it changes only the
# one scenario Bug B's fix is meant to change (two orthogonal leaf communities, gap 1.0), and
# every other scenario's real cross-community gap (<=0.4) stays admitted, unaffected. Recalibrate
# via a real benchmark sweep (mirroring COMMUNITY_ORPHAN_SIMILARITY_THRESHOLD's positive/negative-
# pair methodology) before treating this as final; do not remove the placeholder framing when
# tuning it.
CONTEXT_GLOBAL_SEED_SIMILARITY_GAP = 0.5
```

No other line in this file changes.

## 3. `src/saltmdb/domain/services/community_retrieval_service.py`

### 3.1 Import block (current lines 9-14)

Before:
```python
from saltmdb.config import (
    CONTEXT_GLOBAL_MEMBER_POOL_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
    CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
    get_db_path,
)
```

After:
```python
from saltmdb.config import (
    CONTEXT_GLOBAL_MEMBER_POOL_CAP,
    CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
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
        "seed_cap": {"cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **zero_cap},
        "representative_reserve": {
            "cap": CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
            **zero_cap,
        },
        "member_pool": {"cap": CONTEXT_GLOBAL_MEMBER_POOL_CAP, **zero_cap},
    }
```

After (only the `seed_cap` line changes):
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

### 3.3 Docstring (current lines 48-57)

Append one paragraph after the existing docstring body, before the closing `"""`:

```python
    Community seeding itself is gated by a relative similarity floor: after ranking every leaf
    community by centroid-to-query similarity and capping at CONTEXT_GLOBAL_TOP_K_COMMUNITIES, the
    single best-ranked (rank-1) community is always admitted regardless of its own absolute
    similarity, and every subsequent community is admitted only while its centroid similarity
    stays within CONTEXT_GLOBAL_SEED_SIMILARITY_GAP of rank-1's own similarity. This floor can only
    shrink the top-K window, never grow it, and can never produce zero seeded communities when at
    least one eligible community exists.
    """
```

### 3.4 Seeding + `seed_cap` construction (current lines 90-99)

Before:
```python
        eligible_count = len(ranked_communities)
        seeded = ranked_communities[:CONTEXT_GLOBAL_TOP_K_COMMUNITIES]
        seed_cap = {
            "cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "eligible_count": eligible_count,
            "truncated": eligible_count > CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "dropped_count": max(0, eligible_count - CONTEXT_GLOBAL_TOP_K_COMMUNITIES),
        }
        if not seeded:
            return _empty_result()
```

After:
```python
        eligible_count = len(ranked_communities)
        capped = ranked_communities[:CONTEXT_GLOBAL_TOP_K_COMMUNITIES]
        # Relative admission gap (Bug B fix, SALTMDB memory 510c19ff): rank-1 (capped[0]) is always
        # admitted regardless of its own absolute similarity -- this floor can only shrink the
        # top-K window, never produce an empty seed set. Every subsequent community, in
        # similarity-descending order, is admitted only while it stays within
        # CONTEXT_GLOBAL_SEED_SIMILARITY_GAP of rank-1's own similarity; the first community that
        # violates the gap ends the admitted prefix (every later entry has equal or lower
        # similarity, so it would violate the gap too).
        seeded: list[tuple[str, float]] = []
        for community_id, similarity in capped:
            if seeded and capped[0][1] - similarity > CONTEXT_GLOBAL_SEED_SIMILARITY_GAP:
                break
            seeded.append((community_id, similarity))
        seed_cap = {
            "cap": CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
            "eligible_count": eligible_count,
            "truncated": eligible_count > CONTEXT_GLOBAL_TOP_K_COMMUNITIES
            or len(seeded) < len(capped),
            "dropped_count": max(0, eligible_count - CONTEXT_GLOBAL_TOP_K_COMMUNITIES),
            "gap_dropped_count": len(capped) - len(seeded),
        }
        if not seeded:
            return _empty_result()
```

No other line in this file changes. `seeded_ids = [community_id for community_id, _ in seeded]`
(current line 101) and every downstream reference to `seeded` are unaffected — `seeded` remains a
`list[tuple[str, float]]` in centroid-rank order, just a possibly-shorter prefix of `capped`.

## 4. `tests/test_community_retrieval_service.py`

### 4.1 `_zero_shape()` helper (current lines 130-142)

Before:
```python
    @staticmethod
    def _zero_shape() -> dict[str, Any]:
        cap = {"eligible_count": 0, "truncated": False, "dropped_count": 0}
        return {
            "representative_matches": [],
            "member_matches": [],
            "seed_cap": {"cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES, **cap},
            "representative_reserve": {
                "cap": config.CONTEXT_GLOBAL_REPRESENTATIVE_RESERVE_CAP,
                **cap,
            },
            "member_pool": {"cap": config.CONTEXT_GLOBAL_MEMBER_POOL_CAP, **cap},
        }
```

After (only the `seed_cap` line changes):
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

This is read by `test_scenario_01_zero_communities_returns_empty_shape` and
`test_scenario_02_blank_query_returns_empty_shape_without_embedding` — both pass unchanged once
this helper matches the new `_empty_result()` shape from §3.2.

### 4.2 `test_scenario_04_top_k_seeding_truncates_by_centroid_similarity` (current lines 186-221)

Verified (do not re-derive from a different search than this one): similarities are 1.0, 0.8, 0.2
with `CONTEXT_GLOBAL_TOP_K_COMMUNITIES` patched to 2. `capped` = `[(a, 1.0), (b, 0.8)]`; gap for `b`
is `1.0 - 0.8 = 0.2 <= 0.5`, so `b` is still admitted — `seeded == capped`, behavior is unchanged.
Only the dict literal needs the new key. Change the existing `seed_cap` assertion (current lines
209-217):

Before:
```python
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": 2,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 1,
            },
        )
```

After:
```python
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": 2,
                "eligible_count": 3,
                "truncated": True,
                "dropped_count": 1,
                "gap_dropped_count": 0,
            },
        )
```

### 4.3 Scenarios 06, 07, 09 — verified unaffected, do not edit

Re-verified against current source (not assumed): none of `test_scenario_06_representative_and_member_caps_are_independent`,
`test_scenario_07_member_pool_is_shared_and_truncated_by_query_similarity`, or
`test_scenario_09_ties_break_by_community_and_entity_id` assert `result["seed_cap"]` at all (they
assert `representative_matches`, `representative_reserve`, `member_matches`, and `member_pool`
only). Their own centroid-similarity gaps in each scenario (0.2/0.4 in scenario 06; 0.0/0.0 in 07
and 09) are all `<= 0.5`, so seeding behavior is unchanged regardless. **Do not touch these three
tests** — if a diff touches any of them, stop and report why, since this spec's own verification
found no reason to.

### 4.4 `test_scenario_03_fewer_leaf_communities_all_seeded` (current lines 157-184) — rewritten

This test's own name and both fixture communities (`community-a` at similarity 1.0, `community-b`
at similarity 0.0 — exactly orthogonal) are the confirmed Bug B reproduction (probe 3) in
miniature. Its current assertion that both communities' representatives are returned is precisely
the defect this spec fixes, so it must change, not merely gain a key. Replace the entire method:

Before:
```python
    def test_scenario_03_fewer_leaf_communities_all_seeded(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _axis_vector(0),
        )
        second_rep, _ = self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _axis_vector(1),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": False,
                "dropped_count": 0,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
        )
```

After:
```python
    def test_scenario_03_gap_floor_excludes_orthogonal_second_community(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _axis_vector(0),
        )
        self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _axis_vector(1),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # community-b's centroid is orthogonal to the query (similarity 0.0) while community-a's is
        # an exact match (1.0) -- a gap of 1.0, exceeding CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5).
        # This reproduces probe 3's confirmed Bug B defect shape in miniature: before this fix,
        # community-b was still seeded purely because fewer leaf communities existed than
        # CONTEXT_GLOBAL_TOP_K_COMMUNITIES, regardless of its zero relevance to the query.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep],
        )
```

### 4.5 New scenarios, appended after `test_scenario_10_null_title_is_reported_as_unknown`

(current lines 397-415), before `if __name__ == "__main__":`. Both are required — they cover the
two behaviors this spec's own §1 design decisions 2 and 4 must not silently regress on later
(within-gap admission still working; rank-1 never excluded):

```python
    def test_scenario_11_communities_within_gap_all_seeded(self):
        first_rep, _ = self._insert_community(
            "community-a",
            [("A representative", _axis_vector(0)), ("A member", _axis_vector(1))],
            _cosine_vector(1.0),
        )
        second_rep, _ = self._insert_community(
            "community-b",
            [("B representative", _axis_vector(0)), ("B member", _axis_vector(1))],
            _cosine_vector(0.7),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # Both centroids are within CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5) of the top match's own
        # similarity (gap 0.3) -- the floor admits both, confirming the fix trims genuine outliers
        # only, not every community below rank 1.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": False,
                "dropped_count": 0,
                "gap_dropped_count": 0,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [first_rep, second_rep],
        )

    def test_scenario_12_top_ranked_community_seeded_despite_weak_absolute_similarity(self):
        weak_rep, _ = self._insert_community(
            "community-a",
            [("Weak representative", _axis_vector(0)), ("Weak member", _axis_vector(1))],
            _cosine_vector(0.05),
        )
        self._insert_community(
            "community-b",
            [("Excluded representative", _axis_vector(0)), ("Excluded member", _axis_vector(1))],
            _cosine_vector(-0.5),
        )

        with patch.object(community_retrieval_service, "embed_text", return_value=_axis_vector(0)):
            result = seed_and_rank_communities("community query", db_connection=self.conn)

        # community-a is rank 1 (similarity 0.05) and is force-included despite being a weak
        # absolute match -- the gap floor can never produce an empty seed set. community-b
        # (similarity -0.5, gap 0.55 from rank 1) exceeds CONTEXT_GLOBAL_SEED_SIMILARITY_GAP (0.5)
        # and is correctly excluded.
        self.assertEqual(
            result["seed_cap"],
            {
                "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                "eligible_count": 2,
                "truncated": True,
                "dropped_count": 0,
                "gap_dropped_count": 1,
            },
        )
        self.assertEqual(
            [match["entity_id"] for match in result["representative_matches"]],
            [weak_rep],
        )
```

## 5. `tests/test_retrieve_context_service.py`

Two `seed_top_k` dict literals assert the full `seed_cap` shape via exact dict equality (verified:
these are the only two `seed_top_k`/`strategy="global"` sites in this file — checked with
`rg -n "seed_top_k|seed_cap"` and `rg -n 'strategy="global"'` against current source). Both need
the same additive key; no behavior differs (one has 1 eligible community, the other 0 — the gap
floor never drops anything in either case).

### 5.1 `test_global_populated_leaf_community_...` (current lines 782-804)

Before:
```python
        self.assertEqual(
            result["metadata"]["community"],
            {
                "seed_top_k": {
                    "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                    "eligible_count": 1,
                    "truncated": False,
                    "dropped_count": 0,
                },
```

After (only the `seed_top_k` block changes; `representative_reserve`/`member_pool` below it, and
everything else in the file, are untouched):
```python
        self.assertEqual(
            result["metadata"]["community"],
            {
                "seed_top_k": {
                    "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                    "eligible_count": 1,
                    "truncated": False,
                    "dropped_count": 0,
                    "gap_dropped_count": 0,
                },
```

### 5.2 `test_global_without_communities_returns_empty_hierarchy_envelope` (current lines 829-875)

Before:
```python
                    "community": {
                        "seed_top_k": {
                            "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                            "eligible_count": 0,
                            "truncated": False,
                            "dropped_count": 0,
                        },
```

After:
```python
                    "community": {
                        "seed_top_k": {
                            "cap": config.CONTEXT_GLOBAL_TOP_K_COMMUNITIES,
                            "eligible_count": 0,
                            "truncated": False,
                            "dropped_count": 0,
                            "gap_dropped_count": 0,
                        },
```

No other line in this file changes. In particular, the `fan_out`-shaped `dropped_count` literals
around current lines 160-176 belong to `strategy="local"`'s unrelated fan-out cap and must not be
touched.

## 6. Out of scope

- `representative_reserve`/`member_pool` construction in `community_retrieval_service.py` (current
  lines ~164-185) — untouched; per §1 design decision 6, they only ever draw from communities that
  already passed the new seeding gap-filter.
- `retrieve_context_service.py` itself — its `"seed_top_k": seed_result["seed_cap"]` passthrough
  (line 410) is already correct as-is; only its *tests'* literal dict copies (§5) need the new key.
- `lineage_assembly_service.py`, `context_budget_service.py`, `context_expansion_service.py`,
  `orphan_community_service.py`, `conflict_set_service.py` — none read or construct `seed_cap`.
- `mcp/tools.py`, `daemon/dispatch.py`, `daemon/protocol.py` — no MCP-surface or wire-protocol
  change; the new field is additive inside an already-passthrough dict.
- Renaming, removing, or changing the meaning of the existing `dropped_count` key anywhere in this
  codebase — it keeps its current K-cap-only meaning on every cap dict that has it, including
  `seed_cap`.
- Recalibrating `CONTEXT_GLOBAL_TOP_K_COMMUNITIES`'s own value — separate future benchmark work
  per memory `510c19ff`'s own explicit sequencing ("calibrating `CONTEXT_GLOBAL_TOP_K_COMMUNITIES`
  ... should happen only after Bug B's design question is resolved").
- A real benchmark sweep to calibrate `CONTEXT_GLOBAL_SEED_SIMILARITY_GAP`'s final value — shipped
  as an explicitly-marked placeholder per §1 design decision 4; do not attempt to "improve" the
  value in this pass.
- `tests/test_community_retrieval_service.py` scenarios 01, 02, 05, 06, 07, 08, 09, 10 — verified
  unaffected (§4.1, §4.3); do not edit beyond what §4.1 (helper) already requires.

## 7. Acceptance

```
python -m pytest tests/test_community_retrieval_service.py tests/test_retrieve_context_service.py -v
```
Must exit 0. Every pre-existing scenario in both files must still pass; the only edited assertions
are the ones named in §4.1, §4.2, §4.4, §5.1, §5.2 (scenario 03 renamed/rewritten per §4.4). New
scenarios 11 and 12 (§4.5) must be present and passing.

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

**Scope**: may edit `src/saltmdb/config.py` (§2, one new constant),
`src/saltmdb/domain/services/community_retrieval_service.py` (§3), and
`tests/test_community_retrieval_service.py` (§4) and `tests/test_retrieve_context_service.py`
(§5) only. Does not touch `retrieve_context_service.py`'s own source,
`lineage_assembly_service.py`, `context_budget_service.py`, `context_expansion_service.py`,
`orphan_community_service.py`, `conflict_set_service.py`, `mcp/tools.py`, `daemon/dispatch.py`,
`daemon/protocol.py`, or any other spec file.

**Pre-lock gate, run against the current tree (this session, 2026-09-17):**
1. Drafted §1-§7 in full before writing this section, in that order.
2. §7's four commands are exact, targeted, runnable invocations OMP will run itself per Coding
   Standards rule 18; not run speculatively here as a build (per this workspace's
   verifying-a-spec-never-means-building-the-implementation rule) — but every fact they depend on
   (line numbers, existing test bodies, exact current dict literals) was read directly from the
   current tree in this session, not assumed.
3. Ran `rg -n "seed_cap|dropped_count|CONTEXT_GLOBAL_TOP_K_COMMUNITIES" src/ tests/` against the
   whole tree (not just the two files this spec already knew about) specifically to catch any
   other consumer of `seed_cap`'s shape before finalizing scope. Found `orphan_community_service.py`,
   `context_expansion_service.py`, `conflict_set_service.py` all share the generic
   `dropped_count` shape independently (their own unrelated cap dicts, not `seed_cap` itself) —
   confirms §1 decision 5's reasoning and confirms none of those three files need editing. Found
   `retrieve_context_service.py:410`'s passthrough and its two test call sites (§5) — both now in
   scope.
4. No package-manager/codegen output involved; this is a hand-written source + constant + test
   change with no regenerated lockfile or manifest.
5. Grepped for the OLD `seed_cap`/`dropped_count` dict-literal text specifically in
   `tests/test_retrieve_context_service.py` (not just by function/symbol name) — found both exact
   sites (§5.1, §5.2) as hardcoded literal copies, not derived from a shared constant, confirming
   both needed independent edits.
6. Opened and read every "does not touch" file's actual current content for `seed_cap`/`dropped_count`
   references (§3 of this gate run) — confirmed none of them read or construct `seed_cap`.
7. `seed_cap`'s shape is stated in three places in this spec (this file's own §2 config comment
   prose, §3.4's code block, and §4.1/§5's test literals) — diffed all three against each other:
   `gap_dropped_count` name and `0`/computed-value semantics agree everywhere; `dropped_count`'s
   K-cap-only meaning is unchanged and stated identically in §1 decision 5 and §3.4's code.
8. Concrete instance walked through end-to-end against every other locked decision: probe 3's own
   2-community, similarity-{1.0, 0.0} shape (§4.4) — decision 2 (rank-1 always admitted) keeps
   community-a; decision 3 (absolute gap, 1.0 > 0.5) excludes community-b; decision 6 (scope is
   seeding-only) means community-b's exclusion at the seeding stage is what keeps it out of
   `representative_matches`/`member_matches` too, with no separate downstream-cap change needed —
   all three decisions agree on the same single outcome for this instance, and this is exactly
   what §4.4's rewritten assertion asserts.
9. `CONTEXT_GLOBAL_SEED_SIMILARITY_GAP = 0.5`'s interaction with every *existing* test's actual
   current fixture values was individually recomputed against current source (not assumed
   compatible): scenario 04 (gap 0.2, admitted, dict-only change, §4.2), scenario 06 (gaps
   0.2/0.4, admitted, no assertion on `seed_cap` at all, unaffected, §4.3), scenario 07 (gap 0.0,
   unaffected, §4.3), scenario 09 (gap 0.0, unaffected, §4.3). Only scenario 03's fixture (gap 1.0)
   crosses the threshold, exactly matching what this fix is meant to change.
