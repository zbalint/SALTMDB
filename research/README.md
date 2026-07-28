# SALTMDB Improvement Research — Index

Seven research tracks, each produced by an independent web-research subagent, each grounded in
SALTMDB's actual current implementation (schema, service files, known live issues) and each
containing: Research Summary, Current SALTMDB State, Gaps/Problems, a phased Improvement Plan
(Quick Wins / Medium-Term / Larger Bets), Risks & Trade-offs, Open Questions for zbalint, and a
Sources list with real URLs. Research date: 2026-07-27.

None of this touched any code — these are planning documents to execute from selectively, not a
commitment to build all of it.

## The seven tracks

| # | File | One-line takeaway |
|---|------|--------------------|
| 1 | [`01-dashboard-design.md`](./01-dashboard-design.md) | Kill glassmorphism/gradients (the literal "AI slop" fingerprint), adopt a Linear/Raycast-style hairline-border surface ladder + one restrained accent; split the 1728-line template into `<template>`-partials; vendoring `d3-force` for the graph view is the one dependency-boundary-crossing call that needs explicit sign-off. |
| 2 | [`02-sqlite-concurrency.md`](./02-sqlite-concurrency.md) | **✅ Implemented in v0.1.0-alpha.53** (2026-07-28). The current WAL setup was sound at this scale — no writer-queue/MVCC engine needed — but the one real bug risk found was confirmed and fixed: writes now use `BEGIN IMMEDIATE` via `write_transaction_retrying`, not Python's default deferred `BEGIN`, so `busy_timeout` actually engages on write-write contention (verified with a real two-connection test). Also fixed a bonus bug found during implementation: bulk operations weren't actually atomic despite claiming to be. See `MIGRATION.md`'s alpha.53 entry. |
| 3 | [`03-agent-memory-systems-survey.md`](./03-agent-memory-systems-survey.md) | Architecture comparison of mem0, Letta, Zep/Graphiti, Cognee, ChatGPT memory, Anthropic's memory tool, txtai, LangMem/LlamaIndex. Biggest gap: no true bi-temporal fact model (conflates "when true" with "when recorded") — Graphiti's `created_at/expired_at` + `valid_at/invalid_at` split is the concrete fix. |
| 4 | [`04-adjacent-projects-lessons.md`](./04-adjacent-projects-lessons.md) | Broader PKM/graph-DB/ontology/decay survey (Obsidian, Roam, GraphRAG, SKOS, LanceDB, Ebbinghaus/SM-2/FSRS). Cleanest gap: SALTMDB already tracks `weight` and `last_accessed_at` but neither feeds a decay function — free-standing infrastructure with no consumer. |
| 5 | [`05-tag-system.md`](./05-tag-system.md) | **✅ Quick Wins implemented in v0.1.0-alpha.54** (2026-07-28). Diagnosed *why* fragmentation still happened: `get_canonical_tags` was advisory not enforced, and heuristic merging was lexical-only. Fixed via a shared `resolve_or_create_tag()` helper (exact → normalized → new plural/suffix fallback → create) now used by both write paths — which turned up a bonus bug: `commit_consolidation()` had its own divergent, buggier tag logic ignoring `canonical_id` entirely. Medium-Term embedding-based review queue and the faceted/hierarchy model remain future work. See `MIGRATION.md`'s alpha.54 entry. |
| 6 | [`06-relation-system.md`](./06-relation-system.md) | The `valid_from`/`valid_to` columns exist but are functionally dead (nothing ever sets `valid_to`). Adopt Graphiti's "expire, never mutate" pattern for `commit_consolidation` instead of its current row-`UPDATE` behavior; extend the tag-canonicalization machinery to predicates instead of a hard enum; add a real unique index + confidence/provenance fields. |
| 7 | [`07-information-categorization.md`](./07-information-categorization.md) | Everything is one flat entity type today. Proposes an additive `memory_type` enum (fact/event/procedure/decision, CoALA-style episodic/semantic/procedural) as a quick win, embedding-cluster-assisted domain *suggestions* (never silent auto-tagging) as medium-term, explicitly designed to not become "tag-fragmentation-2.0." |

## Cross-cutting findings worth noting before picking what to build

**Two tracks independently converged on the same fix.** Track 3 (agent memory survey) and Track 6
(relation system) each separately identified SALTMDB's dormant `valid_from`/`valid_to` columns as
the highest-leverage gap, and each independently proposed adopting Graphiti's bi-temporal
invalidation pattern (new edge inserted + old edge's `valid_to`/`expired_at` set, never
UPDATE-in-place) as the fix. Two unrelated research passes landing on the same specific prior art
is a stronger signal than either alone — this is probably the single best-supported "Larger Bet"
across the whole research set.

**One real design collision needs a decision, not two implementations.** Track 5 (tags) proposes a
`type:value` / `component:value` *facet convention inside the tag namespace* (e.g. `type:fix`,
`type:feature`) to stop categorical concepts from colliding with descriptive tags. Track 7
(categorization) independently proposes a dedicated `memory_type` *enum column* on `entities`
(fact/event/procedure/decision) for the same underlying need — distinguishing what *kind* of memory
something is. These are two different mechanisms for overlapping intent. Building both would
recreate exactly the fragmentation-surface problem both documents separately warn about (two ways
to say the same thing). **Pick one**: a first-class `memory_type` column (Track 7's proposal) is
probably the better fit since it's queryable/indexable and closed-vocabulary by construction,
versus a tag-namespace convention that still lives in the same free-text tag table everything else
does — but this is exactly the kind of call that belongs to zbalint, not to be silently resolved by
whichever track gets implemented first.

**Two tracks agree the current SQLite/schema-versioning choices are already good, not just
tolerated.** Track 2 explicitly concludes the current WAL setup doesn't need a writer-queue/MVCC
architecture at this scale — most of its "Larger Bets" section is talked out of rather than argued
for. Track 4 independently notes that Roam's Datomic transaction-log model and LanceDB's zero-copy
versioning both validate SALTMDB's existing SCD-Type-2 approach rather than suggesting a
replacement. Worth reading both as "this part of SALTMDB is in reasonably good shape" rather than
assuming every track found a pile of problems.

**The tag and relation tracks each reference the other's machinery.** Track 6 explicitly recommends
*not* building a separate predicate-canonicalization system, but reusing/extending Track 5's
existing tag-alias infrastructure (parameterized by a `kind` column, or a sibling table) for
predicates. If both tag and relation work land, this reuse should happen — don't build two parallel
alias-resolution systems.

## Suggested cross-cutting priority order

Roughly cheapest-and-highest-value first, pooling all seven tracks' own phase labels:

1. **Immediate correctness check (near-zero cost):** verify `BEGIN IMMEDIATE` vs. deferred
   transactions in the write path (Track 2) — this determines whether the already-shipped 20s
   `busy_timeout` bump is even doing its job.
2. **Quick Wins, low-risk, additive-only, from Tracks 1/2/5/6/7:** dashboard token/CSS pass
   (Track 1), `PRAGMA optimize` + retry/backoff (Track 2), tag write-time validation + mandatory
   canonical-lookup (Track 5), relation dedup + unique index (Track 6), additive `memory_type`
   column (Track 7, pending the collision decision above).
3. **Medium-term, needs a design pass but no irreversible schema break:** embedding-based tag
   merge-candidate queue (Track 5), Graphiti-style `valid_from`/`valid_to` wiring for consolidation
   (Tracks 3 + 6, converging recommendation), `parent_ids`/`relations` redundancy resolution
   (Track 6), decay function consuming `weight`/`last_accessed_at` (Track 4).
4. **Larger bets needing explicit buy-in:** vendoring `d3-force` for the graph view (Track 1, the
   one dependency-boundary exception), confidence/provenance fields on relations (Track 6),
   community/cluster detection over the relation graph (Track 4), faceted/hierarchical tag or
   memory-type model (Tracks 5 + 7, contingent on the collision decision above).

## Next step

Each file's own "Open Questions for zbalint" section has track-specific decisions (predicate
canonicalization ownership, facet vs. enum, dark-mode-only, embedding threshold tuning, etc.) —
those are the actual per-topic decisions to work through before implementation starts on any one
track.
