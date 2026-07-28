# Adjacent Fields & Transferable Ideas for SALTMDB

**Scope note:** this document deliberately stays outside the "AI agent memory system" niche (mem0,
Letta/MemGPT, Zep/Graphiti, Cognee, ChatGPT memory — covered elsewhere). It surveys personal
knowledge management (PKM) tools, general-purpose graph/vector databases, classical
ontology/knowledge-organization practice, and cognitive-science-derived decay models, then maps
concrete, transferable ideas onto SALTMDB's actual schema surface: `entities` (markdown facts with
folksonomy tags), a typed `relations` graph, hybrid FTS5+vector search, SCD-Type-2 temporal
versioning, a `weight` column, `last_accessed_at` tracking, and a background Librarian
consolidation worker.

---

## Research Summary

### 1. Personal Knowledge Management (PKM) tools

**Obsidian** — Its core data model is deceptively simple: plain markdown files, `[[wikilinks]]`,
a folksonomy of `#tags`, and a graph view built purely from those links. Two patterns have proven
durable at scale:
- **Maps of Content (MOCs)**: manually curated index notes that link out to a cluster of related
  notes, functioning like a personal, editable Wikipedia category page rather than a rigid folder
  ("Automated maps of content in Obsidian," readwithai.substack.com). MOCs are a *human-curated
  rollup layer* sitting above raw notes — conceptually identical to a hand-written "community
  summary" in GraphRAG terms (see below), but written by the user instead of an LLM.
- **Unlinked mentions & orphan detection**: Obsidian surfaces every note that *textually* mentions
  a title without a formal link ("Outgoing links," Obsidian help docs; makeuseof.com's "500 orphan
  notes" piece; safjan.com), and separately flags orphan notes with zero connections in Graph View.
  This is a maintenance signal, not just a browsing feature — it is explicitly used to find graph
  decay/neglect. The Code4Lib Journal piece ("From Notes to Networks: Using Obsidian to Teach
  Metadata and Linked Data," journal.code4lib.org) frames Obsidian's tag+link combo as a teaching
  proxy for real linked-data/metadata modeling — i.e., practitioners already treat folksonomy tags
  + typed-ish links as a lightweight ontology in disguise.
- A caveat surfaced directly in the community: Obsidian's graph view is famously "pretty but
  read-only and untyped" — it visualizes connectivity but assigns no semantic meaning to an edge,
  which is exactly the gap a *typed* relation graph (like SALTMDB's) is positioned to fill.

**Roam Research** — The most technically distinctive PKM tool. Underneath, Roam is a Datomic
database, meaning every fact is a *datom*: `(entity, attribute, value, transaction)` — a genuine
entity-attribute-value-with-transaction-id model (zsolt.blog, "Deep Dive Into Roam's Data
Structure"). Consequences worth noting:
- Both **pages and individual blocks (bullets)** are addressable, linkable, first-class entities —
  not just page-level linking. Blocks track `:block/children` and `:block/parents` bidirectionally,
  giving a navigable forest, not just a flat link graph.
- Every datom carries a transaction ID, meaning Roam's storage is *inherently temporal* — you can
  in principle query "what did this fact look like at transaction N," which is conceptually the
  same guarantee SALTMDB's SCD-Type-2 `entities` table already provides, just arrived at from a
  different direction (Datomic's immutable log vs. explicit valid-from/valid-to rows).
  **Roam validates SALTMDB's existing SCD2 design** rather than suggesting something new — worth
  noting as reassurance, not a gap.
- Roam exposes its graph via **Datalog queries** end-users can write directly ("which pages were
  edited last week," "what are the longest paragraphs" — zsolt.blog). The lesson isn't "adopt
  Datalog" but "expose ad hoc structural queries over the relation graph as a first-class
  capability," not just fixed tool endpoints.
- **Namespaces** (hierarchical page titles like `Books/Sapiens`) are a lightweight, purely
  string-convention hierarchy layered on top of a flat namespace — no schema migration needed
  (Roam namespaces catalog, gist.github.com/jdevera).

**Logseq** — Reinforces the same outliner/block/backlink pattern as Roam but commits hard to
**local-first, plain-text-on-disk** storage (markdown/org files, no proprietary DB, no cloud
dependency) as its core value proposition (aitoolpick.org 2026 review; atlasworkspace.ai). Logseq's
mid-2020s migration from file-based to a SQLite-backed model reportedly introduced real instability
for its user base — a cautionary data point about backing storage migrations, relevant since
SALTMDB is already SQLite-native and doesn't face that particular transition, but is a reminder
that storage-format changes in a knowledge tool are high-risk, user-trust-sensitive events.

**Zettelkasten (Luhmann) / Evergreen notes (Andy Matuschak)** — The classical Zettelkasten method
(sloww.co) rests on **atomicity** (one claim per note), **explicit provenance**, and manual
**one-nearby-link** linking discipline — deliberately *not* maximal linking. Matuschak's "evergreen
notes" (notes.andymatuschak.org) extend this with a documented tension directly relevant to
decay/reinforcement design: *"A [spaced repetition] memory system will help you retain and
continuously engage with what you write, but it won't help you build on those ideas over time. An
Evergreen notes system will help you build on your ideas over time, but it won't help you retain
[them]."* Matuschak's own attempt to fuse the two — the "mnemonic medium," embedding spaced-
repetition prompts inside evergreen prose — is a direct precedent for the question SALTMDB faces:
*can retrieval-strength decay and durable-knowledge-building coexist in the same store, or do they
need separate mechanisms?* Separately, A-MEM (deep-paper.org write-up) is a recent AI-agent-memory
paper explicitly built on Zettelkasten atomicity + flexible linking — noted briefly since it
straddles both research tracks, but the transferable idea (atomic, single-claim notes with
provenance) predates it by decades and is PKM-native.

### 2. General-purpose graph databases & GraphRAG

**Neo4j / property graph vs. RDF triple store** — Neo4j's labeled property graph model (nodes +
typed relationships, both bearing arbitrary key/value properties, nodes carrying labels) was built
explicitly for fast storage/traversal rather than RDF's data-interchange goals (neo4j.com/blog,
"RDF vs. property graphs"; PLOS One / PMC comparison study). The practical takeaway for SALTMDB:
its `relations` table (typed predicate + source + target, presumably no properties-on-edges) is
already a property-graph-*style* model; the property-graph literature's refinement is that **edges
themselves can and often should carry properties** (confidence, provenance, valid-time), not just
the two node references — worth checking whether SALTMDB relation edges support this beyond a bare
predicate string.

**Microsoft GraphRAG** — The concrete, well-documented pipeline (microsoft.github.io/graphrag;
Microsoft Research blog "Improving global search via dynamic community selection"; original paper
arXiv:2404.16130 "From Local to Global: A Graph RAG Approach to Query-Focused Summarization";
memgraph.com's "How GraphRAG works alongside a graph database"):
1. Slice text into **TextUnits**, extract entities/relationships/claims into a graph.
2. Run **Leiden community detection** *hierarchically and recursively* — each level partitions the
   graph mutually-exclusively and collectively-exhaustively, producing nested clusters down to
   leaf communities that can't be split further.
3. Generate an LLM-written **community report** (summary) at every level of that hierarchy —
   bottom-up, fine-grained clusters rolled up into progressively more abstract summaries.
4. Serve three query modes: **Global Search** (reason over community summaries for
   dataset-wide questions), **Local Search** (fan out from a specific entity to its neighbors),
   **DRIFT Search** (hybrid of the two).

This is the single most transferable *structural* idea in this research pass: SALTMDB has a
`relations` table and a Librarian consolidation worker, but (per the task framing) no
community/cluster-detection step over that graph and no hierarchical rollup summary layer — it
consolidates near-duplicate *pairs/small groups* but doesn't appear to detect *emergent clusters*
of many loosely-linked-but-thematically-united memories and summarize them as a unit the way
Obsidian's manual MOCs or GraphRAG's automatic community reports do.

**Community detection algorithms (Louvain → Leiden)** — Louvain optimizes modularity via two
phases (local node-community reassignment, then graph coarsening) but can produce arbitrarily
badly-connected or even disconnected "communities" under iteration; Leiden fixes this, guaranteeing
well-connected communities, and is the algorithm GraphRAG actually uses (nature.com/articles/
s41598-019-41695-z "From Louvain to Leiden"; puppygraph.com Louvain explainer; memgraph.com Leiden
docs). Both have mature, well-documented open-source implementations (igraph, `leidenalg`,
`python-louvain`, networkx) that run fine on graphs of a few thousand nodes — i.e., this is not a
big-data-only technique; it is entirely tractable to run periodically over SALTMDB's own
`relations` table size.

### 3. Ontology / RDF / OWL — the 80/20 version

Full OWL brings class hierarchies, property domains/ranges, and formal reasoning/inference — heavy
machinery most lightweight systems don't need. **SKOS (Simple Knowledge Organization System)** is
the W3C's own deliberately-lightweight subset for exactly this situation (w3.org/TR/skos-reference;
moderndata101.substack.com "Demystifying SKOS for Practitioners"; medium.com/@jaywang.recsys). The
80/20 vocabulary that generalizes far outside RDF:
- **`prefLabel` vs `altLabel` vs `hiddenLabel`** — one canonical label per concept, N searchable
  synonyms, and N silently-indexed-but-never-displayed variants (old names, common misspellings).
  This maps almost one-to-one onto a tag-canonicalization problem: SALTMDB's `get_canonical_tags`
  and `merge_tags` tools currently seem to treat tags as a flat dedup problem; SKOS's three-tier
  label model gives a principled place to put "the tag everyone should converge on" vs. "known
  aliases that should still hit in search" vs. "deprecated tags kept only for backward search."
- **`broader` / `narrower`** (hierarchical, intentionally *non-transitive* per edge to avoid
  silently implying a long transitive chain means something) and **`related`** (symmetric,
  associative, no hierarchy implied) — a two-relation-type minimum for a tag *hierarchy*
  (`#db` broader-than `#sqlite-vec`) distinct from a tag *network* (`#embeddings` related-to
  `#chunking`). SALTMDB's tags currently read as a flat folksonomy with no broader/narrower
  structure at all.
- **`exactMatch` / `closeMatch`** — mapping properties for reconciling two labels that mean the
  same thing across vocabularies, with `exactMatch` transitive+symmetric and `closeMatch`
  deliberately *not* transitive (to stop compounding small approximation errors into false
  equivalences over a long chain). This is directly the shape of SALTMDB's own
  `commit_consolidation`/supersession-candidate logic, just currently applied to whole memory
  entities rather than to tags.

The concrete 80/20 lesson: **adopt SKOS's *label* and *relation* vocabulary (5-6 named concepts),
not RDF's triple-store storage model or OWL's reasoning engine.** No new storage paradigm needed —
just richer, more disciplined use of relations SALTMDB already models as a graph.

### 4. Vector database design choices

Comparative sources: MarkTechPost's 2026 "Best Vector Databases" survey; 4xxi.com's ChromaDB vs.
Qdrant vs. pgvector vs. Pinecone vs. LanceDB piece; builderai.tools' Qdrant/Milvus/Weaviate/LanceDB
benchmark writeup; LanceDB's own docs and GitHub issue tracker.
- **Weaviate** ships genuinely native hybrid search (BM25 + dense vector fused server-side in one
  query, not bolted on) — architecturally the same goal as SALTMDB's FTS5+vector RRF fusion, just
  productized as a first-class query mode rather than an application-level join.
- **Qdrant** applies metadata/payload filters *before* the ANN search rather than after
  ("pre-filtering"), which the comparison literature repeatedly credits as both faster and more
  accurate than post-filter-and-discard — worth checking whether SALTMDB's `tags_filter`/
  `owner_id`/`is_core` filters in `search_memory` are applied before or after the vector kNN step,
  since post-filtering a top-K vector result set can silently starve results when a filter is
  narrow (e.g. filtering to one `owner_id` after only fetching the global top-20 nearest vectors).
- **LanceDB** is the most structurally comparable peer to SALTMDB: an embedded, disk-based,
  single-process store (like SQLite) built on a columnar format (Lance) that supports vector
  search + full-text (BM25) + SQL-style filtering *on the same table*, and — most notably —
  ships **zero-copy versioning, ACID transactions, time-travel, tags and branches on the dataset
  itself, with no extra infrastructure** (lancedb's own docs, "Time-Travel RAG with versioned
  data"; GitHub issue #1502 on incremental indexing). This is functionally a productized version of
  what SALTMDB's SCD-Type-2 `entities` table is already hand-rolling for entity history — good
  external validation that the SCD2 approach is a recognized, deliberate design pattern in this
  space, not an idiosyncratic reinvention.
- Genuinely missing-looking pieces vs. this landscape: none of the surveyed systems' *headline*
  differentiators (native hybrid-search-as-one-query, pre-filter-before-ANN, dataset-level
  time-travel/branching) appear to be things SALTMDB lacks outright — it already does FTS5+vector
  RRF and SCD2 versioning. The more likely gap is **payload-field indexing discipline**: Qdrant's
  advantage is explicit secondary indexes on filterable payload fields; worth confirming
  `tags_filter`/`owner_id` lookups in SALTMDB hit an actual SQLite index rather than a table scan
  as the entity count grows.

### 5. Decay / forgetting mechanisms from cognitive science

- **Ebbinghaus forgetting curve**: `R = e^(-t/S)` — retention `R` decays exponentially with
  elapsed time `t`, moderated by a memory-strength constant `S` (e-student.org; TechRxiv
  "Modeling Memory Retention with Ebbinghaus's Forgetting Curve"). Empirically: ~50% loss within an
  hour, ~70% within a day, absent reinforcement.
- **SM-2 (Wozniak 1987, Anki's original scheduler)**: tracks a single per-card **ease factor**
  (starts at 2.5, floor 1.3), and after the first couple of reviews each new interval = previous
  interval × ease factor, adjusted up/down by recall quality (faqs.ankiweb.net; dev.to SM-2
  writeup). Simple, one scalar state variable, no personalization.
- **FSRS (Free Spaced Repetition Scheduler, Anki's modern default)**: models **two** state
  variables per item — **stability** `S` (days until retrievability decays to ~90%, effectively a
  learned analogue of Ebbinghaus's `S`) and **difficulty** `D`. Retrievability is `R = (1 + F·t/S)^D_ecay`
  (a power-law generalization of the pure exponential), stability *increases* on a successful
  review and *decreases* on a lapse, and the mapping from review outcomes to stability updates is
  itself fit by a small trained model (~21 parameters) rather than fixed constants (deepwiki.com
  FSRS-vs-SM-2 comparison). The key conceptual leap over Ebbinghaus/SM-2: decay rate itself is not
  fixed — it depends on how many times, and how successfully, the item has already been
  reinforced (a memory reviewed 10 times decays slower than one reviewed twice, even at equal
  `S`).
- A directly on-point recent paper: **FadeMem — "Biologically-Inspired Forgetting for Efficient
  Agent Memory"** (arXiv:2601.18642, Wei/Peng/Dong/Xie/Wang) proposes exactly this
  decay-unless-reinforced model for an agent memory store; full formulas weren't extractable from
  the fetched PDF in this pass, but the paper's existence itself is a signal this is an active,
  named research direction worth a follow-up read rather than a novel design SALTMDB would be
  inventing from scratch.

**Fit assessment for SALTMDB**: SALTMDB already tracks the two variables this entire literature
needs — a `weight` column (≈ memory strength / stability) and `last_accessed_at` (≈ time since last
reinforcement) — but per the task brief, `weight` is set once at creation and never decays, and
`last_accessed_at` is tracked but not fed back into retrieval ranking or archival decisions. This is
the single largest, cleanest gap surfaced by this whole research pass: **the schema already has the
two inputs a decay function needs; only the function itself and a place to call it are missing.**

### 6. Other adjacent ideas worth flagging

- **Digital garden / "stream vs. garden" framing** (implicit in the Obsidian/Logseq/Roam material
  above via daily-notes-as-timeline vs. MOC-as-garden-bed): daily notes act as a low-friction,
  chronological *capture* stream, while MOCs/evergreen notes act as a curated, revised, timeless
  *garden* — two different maturity states for the same underlying fact. SALTMDB's raw
  `log_event` stream vs. `store_memory` long-term entities is already structurally this same
  two-tier split (ephemeral capture → durable synthesis); the PKM literature's addition is the
  *explicit UX/labeling* of "this note is still a stream entry" vs. "this note has graduated to
  evergreen," which SALTMDB doesn't currently surface (a memory just "is" long-term once stored,
  with no visible maturity/confidence gradient).
- **`skos:Concept` vs. `skos:ConceptScheme`** (from ontology section) also generalizes as: a tag can
  belong to zero, one, or several *schemes* simultaneously without conflict — relevant if SALTMDB
  ever wants per-project or per-domain tag namespaces without hard-partitioning the tag table.

---

## Current SALTMDB State

(Grounding restated from the task brief and the research above — not independently re-verified
against the codebase in this pass, per the web-research-only scope of this task.)

- **Storage**: entities are markdown facts with folksonomy tags, versioned via SCD-Type-2 (no hard
  deletes — supersession/valid-time rows instead), backed by SQLite.
- **Retrieval**: hybrid FTS5 (BM25) + dense vector search, fused via RRF, with 1-hop relation-graph
  expansion by default (`include_related=True`).
- **Graph**: a `relations` table storing typed, directional edges between entities
  (`manage_relation`, predicates like `resolves`/`depends_on`/`consolidated_from`), inspectable via
  `inspect_graph` in `dependencies` / `lineage` / `orphans` modes.
- **Consolidation**: a background Librarian worker merges near-duplicate memories (≥0.75 similarity
  auto-flagged as supersession candidates), soft-archiving parents and recording
  `consolidated_from` lineage — pairwise/small-group merging, not cluster-level.
- **Tag governance**: `get_canonical_tags` suggests existing tags to reduce fragmentation;
  `merge_tags` collapses duplicates; tags otherwise appear to be flat (no broader/narrower
  hierarchy, no distinct preferred-vs-alias-vs-deprecated tiers).
- **Weight/recency**: a `weight` column exists (set once at store time), and `last_accessed_at` is
  tracked, but — per the task brief — neither currently drives an active decay function; weight is
  static after creation and access doesn't reinforce it.

---

## Gaps / Problems

Specific, schema-anchored gaps this research surfaces:

1. **No decay mechanism despite tracking the exact two inputs decay needs.** `weight` is
   write-once; `last_accessed_at` is recorded but not read back into any ranking/archival logic.
   Every decay model surveyed (Ebbinghaus, SM-2, FSRS, FadeMem) needs only `(current_strength,
   time_since_last_reinforcement, was_it_reinforced_just_now)` — SALTMDB already has slots for two
   of those three.
2. **No community/cluster detection over the `relations` graph despite having one.** Consolidation
   currently appears to work pairwise/small-group (via similarity ≥0.75), not by detecting
   emergent, densely-interlinked *clusters* of memories that individually fall under any pairwise
   threshold but are collectively a coherent topic — the exact problem Leiden community detection
   + hierarchical GraphRAG-style summarization solves.
3. **No hierarchical rollup/summary layer analogous to Obsidian MOCs or GraphRAG community
   reports.** `commit_consolidation` promotes or merges existing memories but there's no
   "auto-generate (or prompt-for) a standing index note over this cluster" step, and no evidence of
   a Local/Global-style query mode that first checks a rollup summary before fanning out to raw
   entities.
4. **Flat tag model — no broader/narrower hierarchy, no preferred/alias/hidden label tiers.**
   `get_canonical_tags` + `merge_tags` solve *exact-duplicate* tag fragmentation but have no
   concept of `#db` being broader than `#sqlite-vec`, nor a way to keep a deprecated tag alias
   silently searchable without surfacing it as a suggestion (SKOS's `hiddenLabel` gap).
5. **Relation edges likely carry no properties beyond the predicate string** (unconfirmed without
   reading the schema, but implied by the tool surface) — property-graph practice (Neo4j) treats
   confidence, provenance, and valid-time as first-class edge properties, not just node properties.
6. **Uncertain pre-filter vs. post-filter order in hybrid search.** If `tags_filter`/`owner_id` in
   `search_memory` are applied *after* the vector/FTS top-K fetch rather than before, narrow filters
   on a growing entity table can silently under-return relevant results — the exact failure mode
   Qdrant's architecture is built to avoid.
7. **No visible "maturity" or "confidence" gradient between a fresh capture and a settled,
   revised fact** — the stream-vs-garden distinction PKM tools make implicitly (daily note vs. MOC)
   collapses in SALTMDB to a binary: `log_event` (ephemeral) vs. `store_memory` (permanent), with
   nothing recording "this stored memory is still provisional / low-confidence."
8. **No exposed ad hoc query capability over the relation graph** akin to Roam's Datalog — only the
   fixed `inspect_graph` modes (`dependencies`/`lineage`/`orphans`). Not necessarily a real gap (a
   fixed, safe tool surface is defensible for an MCP server with no SQL access allowed), but worth
   naming since it's the one Roam-derived idea SALTMDB's own no-raw-SQL policy explicitly forecloses
   — flagged for awareness, not necessarily action.

---

## Proposed Improvement Plan

### Quick Wins (schema-light, low risk, days not weeks)

- **Add a decay function fed by existing columns.** No new column strictly required: compute an
  *effective* retrieval weight at query time as `effective_weight = weight * exp(-(now -
  last_accessed_at) / half_life)` (Ebbinghaus form) rather than mutating stored `weight`, so nothing
  destructive happens to the persisted value. Bump `last_accessed_at` (and optionally a new
  lightweight `access_count` counter) on every `search_memory` hit that actually gets returned/used,
  giving the "reinforced by access" half of the model for free. This is a service-function change
  (wherever `search_memory`'s ranking/ORDER BY currently lives) plus one new counter column
  (`access_count`) — no migration of `weight` itself needed.
- **Tag tiers via existing `tags` machinery.** Extend `get_canonical_tags`/`merge_tags` to
  recognize a lightweight 3-state label model (canonical / alias / deprecated-but-searchable) —
  this can likely be a convention on top of the existing tag table (e.g. a `tag_aliases` mapping
  table: `alias_tag -> canonical_tag`, `status`) rather than a new subsystem, directly modeled on
  SKOS `prefLabel`/`altLabel`/`hiddenLabel`.
- **Surface a maturity flag.** Add a small enum/boolean (`provisional` vs. `settled`, or reuse
  `is_core`-style flag pattern) so a freshly-stored memory can be marked "still a stream entry" and
  later promoted — cheap to add, and gives the stream-vs-garden distinction PKM tools rely on.

### Medium-Term (new service logic, still no new storage paradigm)

- **`broader`/`narrower`/`related` as first-class relation predicates for tags**, reusing the
  existing `relations` table (tags would need to become addressable entities or a parallel small
  table) rather than inventing a second graph system — this turns the current flat folksonomy into
  a shallow SKOS-style taxonomy without adopting RDF/OWL.
- **Periodic community detection over the `relations` graph.** Run Leiden (via `leidenalg` or
  `python-networkx`+`python-louvain` as a fallback) as a scheduled/background job — same
  execution model as the existing Librarian consolidation worker — over the entity-relation graph
  to surface clusters the current pairwise-similarity consolidation misses. Store the result as a
  new relation type (e.g. `predicate="in_community:<cluster_id>"`) or a lightweight
  `entity_communities(entity_id, cluster_id, run_id)` table, cheap to add/drop without touching
  core schema.
- **Auto-draft a rollup summary per detected community** (GraphRAG's community-report step,
  Obsidian's MOC pattern) — an LLM-written short markdown summary of each cluster, stored as a
  normal entity linked to its members via `consolidated_from`-style edges, giving `search_memory` a
  cheap "check the rollup first" option analogous to GraphRAG's Global Search mode.
- **Verify and, if needed, fix filter-then-search ordering** in `search_memory`: confirm
  `tags_filter`/`owner_id`/`is_core` are applied as a SQL `WHERE` restricting the candidate set
  *before* the vector kNN / FTS5 MATCH runs, not as a post-hoc filter on an already-truncated top-K
  — a targeted fix to the query builder, not a schema change.

### Larger Bets (worth scoping separately, real design decisions)

- **Edge properties on `relations`.** If not already present, extend the relations table (or a
  companion `relation_properties` table) to carry confidence/provenance/valid-time per edge, not
  just per entity — brings SALTMDB's graph in line with property-graph practice (Neo4j) rather than
  a bare directed-labeled-graph.
- **A tunable, per-owner decay profile**, echoing FSRS's two-variable (stability + difficulty)
  model instead of a single global half-life: e.g. `#core` memories or those tagged as
  architectural decisions decay slower (long half-life / never decay) than `#issue`/`#attempt`
  ephemeral-leaning entries — turns one global constant into a per-tag or per-`is_core` policy,
  which is a genuine design decision (needs user input — see Open Questions) rather than a pure
  engineering task.
- **A safe, constrained "ad hoc graph query" tool** as a middle ground between Roam's full Datalog
  exposure and today's three fixed `inspect_graph` modes — e.g. a small allow-listed query DSL
  (not raw SQL, staying within the no-direct-SQL constraint) for graph traversal patterns not yet
  anticipated by the fixed modes.

---

## Risks & Trade-offs

- **Decay is destructive if applied wrong.** Mutating the stored `weight` column directly on every
  read risks silently degrading a memory's true importance based on retrieval *frequency* rather
  than *validity* — a rarely-needed but still-critical architectural rule (e.g. a `#core` constraint
  consulted once a quarter) could decay below relevance and get archived/deprioritized. The
  Quick-Win proposal above deliberately computes an ephemeral `effective_weight` at query time
  rather than overwriting stored `weight`, precisely to keep this reversible — but any later
  decision to persist decayed weight needs a floor/never-decay carve-out for `#core` entities.
  Ebbinghaus-style curves themselves have no notion of "this fact matters regardless of recall
  frequency"; that has to be layered on deliberately.
- **Community detection adds a moving part and a tuning surface** (resolution parameter, minimum
  cluster size, how often to re-run) that can drift out of sync with the underlying graph if run
  too infrequently, or churn cluster assignments confusingly if run too often — needs a stable
  cadence (e.g. tied to the existing Librarian consolidation schedule) rather than running on every
  write.
- **LLM-written community/rollup summaries introduce a hallucination surface** on top of otherwise
  human/agent-authored ground-truth memories — worth clearly tagging auto-generated rollups as
  such (their own tag, e.g. `#auto-summary`) so they're never mistaken for a directly-asserted fact
  during a `#core` or governance check.
- **SKOS-style tag hierarchy is a modeling investment users must maintain.** Broader/narrower
  relations decay in value fast if nobody curates them going forward — same failure mode that
  makes real-world OWL ontologies rot; a lightweight version is safer but not immune.
- **None of this is validated against SALTMDB's actual current schema** in this pass — this
  document is web-research-only by design; before implementing any Quick Win, the actual column
  names/types in `entities`/`relations` need to be re-confirmed against the live schema (a
  five-minute check, but a real precondition, not a formality).

---

## Open Questions for zbalint

1. Should decay be **global** (one half-life for all memories) or **per-tag/per-`is_core`**
   (e.g. `#core` and architectural-decision memories never decay; `#issue`/`#attempt` ephemeral
   entries decay fast)? This is a policy call, not an engineering one.
2. Is it acceptable for decay to affect **retrieval ranking only** (query-time `effective_weight`),
   or do you also want it to eventually drive **archival** (auto-archiving memories whose decayed
   weight crosses a floor with no recent access)? The latter is a much bigger behavioral change and
   probably needs a human-in-the-loop review step before any auto-archive fires.
3. For community detection: is a **scheduled background job** (piggybacking on the existing
   Librarian worker's cadence) the right model, or would you rather trigger it manually / on
   demand given the graph's current size?
4. Do you want **auto-generated rollup/MOC-style summary notes** at all, given the hallucination-
   surface trade-off above — or would you prefer community detection to only ever *surface a
   cluster for you to review*, with any resulting summary written and stored manually (keeping the
   Obsidian-MOC "human-curated" spirit rather than the GraphRAG "LLM-curated" one)?
5. Should tag broader/narrower relations reuse the existing `relations` table (treating tags as
   pseudo-entities) or live in a dedicated, smaller table — is there an appetite for tags becoming
   first-class graph nodes, or should they stay a lighter-weight parallel structure?

---

## Sources

1. [What Is Obsidian Used For? A Practical 2026 Guide | Obsibrain](https://www.obsibrain.com/blog/what-to-use-obsidian-for-in-2026)
2. [Automated maps of content in Obsidian with Templater and Dataview](https://readwithai.substack.com/p/automated-maps-of-content-in-obsidian)
3. [Graph view - Obsidian Help](https://obsidian.md/help/plugins/graph)
4. [The Code4Lib Journal — From Notes to Networks: Using Obsidian to Teach Metadata and Linked Data](https://journal.code4lib.org/articles/18535)
5. [I had 500 orphan notes in Obsidian — here's the exact system I used to link them all (MakeUseOf)](https://www.makeuseof.com/orphan-notes-in-obsidian-linking-system/)
6. [List Unlinked (Orphaned) Notes in Obsidian — safjan.com](https://safjan.com/list-unlinked-orphaned-notes-obsidian/)
7. [Organizing Your Notes in Roam - Pages, Blocks, Tags, and Outlining — zsolt.blog](https://www.zsolt.blog/2021/02/organizing-your-notes-in-roam.html)
8. [Deep Dive Into Roam's Data Structure - Why Roam is Much More Than a Note Taking App — zsolt.blog](https://www.zsolt.blog/2021/01/Roam-Data-Structure-Query.html)
9. [Namespaces Catalog for Roam Research (v.1.0) — GitHub Gist](https://gist.github.com/jdevera/4a708abe505cb008478e69d1f35c90b7)
10. [Logseq Review 2026 — AIToolPick Blog](https://aitoolpick.org/blog/logseq-review-2026/)
11. [Obsidian vs Logseq (2026): Which Plain-Text PKM Wins? — Atlas Workspace](https://www.atlasworkspace.ai/blog/obsidian-vs-logseq)
12. [Zettelkasten 101: Smart Note-Taking System of Niklas Luhmann — Sloww](https://www.sloww.co/zettelkasten/)
13. [Building Brains for AI: How A-MEM Uses the Zettelkasten Method — Deep Paper](https://deep-paper.org/en/paper/2502.12110/)
14. [The mnemonic medium can be extended to one's personal notes — Andy Matuschak's notes](https://notes.andymatuschak.org/The_mnemonic_medium_can_be_extended_to_one%E2%80%99s_personal_notes)
15. [Similarities and differences between evergreen note-writing and Zettelkasten — Andy Matuschak's notes](https://notes.andymatuschak.org/Similarities_and_differences_between_evergreen_note-writing_and_Zettelkasten)
16. [Welcome - GraphRAG (Microsoft, official docs)](https://microsoft.github.io/graphrag/)
17. [GraphRAG: Improving global search via dynamic community selection — Microsoft Research Blog](https://www.microsoft.com/en-us/research/blog/graphrag-improving-global-search-via-dynamic-community-selection/)
18. [From Local to Global: A Graph RAG Approach to Query-Focused Summarization — arXiv:2404.16130](https://arxiv.org/pdf/2404.16130)
19. [How Would Microsoft GraphRAG Work Alongside a Graph Database? — Memgraph](https://memgraph.com/blog/how-microsoft-graphrag-works-with-graph-databases)
20. [From Louvain to Leiden: guaranteeing well-connected communities — Scientific Reports / Nature](https://www.nature.com/articles/s41598-019-41695-z)
21. [What is the Louvain Method? — PuppyGraph](https://www.puppygraph.com/blog/louvain)
22. [Leiden community detection — Memgraph docs](https://memgraph.com/docs/advanced-algorithms/available-algorithms/leiden_community_detection)
23. [SKOS Simple Knowledge Organization System Reference — W3C](https://www.w3.org/TR/skos-reference/)
24. [Demystifying SKOS for Practitioners: A Practical Guide to Controlled Vocabularies — Modern Data 101](https://moderndata101.substack.com/p/demystifying-skos-for-practitioners)
25. [Ontology, Taxonomy, and Graph standards: OWL, RDF, RDFS, SKOS — Jay Wang, Medium](https://medium.com/@jaywang.recsys/ontology-taxonomy-and-graph-standards-owl-rdf-rdfs-skos-052db21a6027)
26. [RDF vs OWL: Key Differences, Use Cases and Examples Explained — Atlan](https://atlan.com/know/rdf-vs-owl/)
27. [RDF triple stores vs. property graphs: What's the difference? — Neo4j Blog](https://neo4j.com/blog/knowledge-graph/rdf-vs-property-graphs-knowledge-graphs/)
28. [Property Graph vs RDF Triple Store: A Comparison on Glycan Substructure Search — PLOS One / PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC4684231/)
29. [Best Vector Databases in 2026: Pricing, Scale Limits, and Architecture Tradeoffs — MarkTechPost](https://www.marktechpost.com/2026/05/10/best-vector-databases-in-2026-pricing-scale-limits-and-architecture-tradeoffs-across-nine-leading-systems/)
30. [Vector Database Comparison 2026: ChromaDB vs. Qdrant vs. pgvector vs. Pinecone vs. LanceDB — 4xxi](https://4xxi.com/articles/vector-database-comparison/)
31. [Qdrant vs Milvus vs Weaviate vs LanceDB: Vector DB Comparison — BuilderAI Tools](https://builderai.tools/blog/vector-database-benchmarks-qdrant-milvus-weaviate-lancedb)
32. [Time-Travel RAG with versioned data — LanceDB Docs](https://docs.lancedb.com/tutorials/agents/time-travel-rag)
33. [Incremental data indexing mode · Issue #1502 — lancedb/lancedb, GitHub](https://github.com/lancedb/lancedb/issues/1502)
34. [Comparison with SM-2 — fsrs-optimizer, DeepWiki](https://deepwiki.com/open-spaced-repetition/fsrs-optimizer/7.3-comparison-with-sm-2)
35. [What spaced repetition algorithm does Anki use? — Anki FAQs](https://faqs.ankiweb.net/what-spaced-repetition-algorithm)
36. [The Ebbinghaus Forgetting Curve (and How to Beat It) — e-student.org](https://e-student.org/ebbinghaus-forgetting-curve/)
37. [Modeling Memory Retention with Ebbinghaus's Forgetting Curve and Interpretable Machine Learning on Behavioral Factors — TechRxiv](https://www.techrxiv.org/users/907969/articles/1286417-modeling-memory-retention-with-ebbinghaus-s-forgetting-curve-and-interpretable-machine-learning-on-behavioral-factors)
38. [FadeMem: Biologically-Inspired Forgetting for Efficient Agent Memory — arXiv:2601.18642](https://arxiv.org/pdf/2601.18642)
