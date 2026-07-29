# Information Categorization & Classification Schemes for SALTMDB

> **Status: Quick Win (`memory_type`) implemented in v0.1.0-alpha.56** (2026-07-28); **Phase 2
> manual `domain` column implemented in v0.1.0-alpha.58** (2026-07-29). Shipped the additive
> `memory_type` enum column (`fact`/`event`/`procedure`/`decision`/`preference`, CHECK-constrained,
> `DEFAULT 'fact'`) on `entities`, plus `store_memory(memory_type=...)` and
> `search_memory(memory_type_filter=...)` support in `memory_service.py` and the MCP tool layer.
> This also resolved the Track 5 vs. Track 7 design collision noted in `research/README.md` in
> favor of the column over a tag-namespace convention. Later shipped a `domain TEXT` column —
> deliberately shaped differently from `memory_type` (no `DEFAULT`, no DB-level `CHECK`; enforced
> instead at the service layer via a `VALID_DOMAINS` allow-list, since this vocabulary is expected
> to grow as new projects/life-areas appear), currently `('SALTMDB', 'CADET', 'Business',
> 'Homelab', 'General')` — plus `store_memory(domain=...)` and `search_memory(domain_filter=...)`
> support. **NOT implemented**: the embedding-cluster-assisted (UMAP/HDBSCAN) domain *suggestion*
> pipeline originally scoped as part of Phase 2 remains future work and out of scope — what shipped
> is a simpler, manually-set domain column only, never silent/automatic tagging. See
> `MIGRATION.md`'s alpha.56 and alpha.58 entries.

> Scope note: this document is about structural **classification schemes and type systems** —
> what *kind* of thing a memory is (episodic event vs. semantic fact vs. procedure vs. decision),
> what *subject/domain* it belongs to, and whether that axis should be a first-class column vs.
> left entirely to freeform tags. It deliberately does **not** re-cover tag-string dedup/canonicalization
> (a separate research track) or do a system-by-system architecture comparison of mem0/Letta/Zep-Graphiti
> (also a separate track). Those systems are referenced here only insofar as they bear on typing schemes.

## Research Summary

### 1. Cognitive-science memory-type taxonomies (Tulving) and their adoption in AI memory systems

The episodic/semantic distinction originates with Endel Tulving (1972); Larry Squire added procedural
memory to the trichotomy in 1987. In 2023, the "Cognitive Architectures for Language Agents" (CoALA)
paper (Sumers et al., [arXiv:2309.02427](https://arxiv.org/pdf/2309.02427)) translated this trichotomy
directly into an LLM-agent memory framework: **working memory** (the immediate context/scratchpad) plus
three long-term stores — **episodic** (records of past events/experience, "what happened and when"),
**semantic** (generalized, decontextualized facts about the world), and **procedural** (skills, learned
routines, "how to do X," often stored as code or heuristics rather than as prose).

This taxonomy has become close to a field standard. According to industry write-ups
([Atlan: Types of AI Agent Memory](https://atlan.com/know/types-of-ai-agent-memory/),
[Atlan: Episodic Memory for AI Agents](https://atlan.com/know/episodic-memory-ai-agents/)),
"most major frameworks, including Letta, Mem0, and LangChain, use CoALA as their taxonomy foundation,"
and by 2025–2026 "the agent ecosystem converged on a remarkably consistent three-tier taxonomy —
episodic, semantic, and procedural memory — that mirrors decades of cognitive science research."
A December-2025 survey ("Memory in the Age of AI Agents," arXiv:2512.13564, tracked via the
[Agent-Memory-Paper-List](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)) notes the taxonomy
is now fragmenting further as the field matures, proposing refinements like "Factual Memory" and
"Experiential Memory" (itself split into case-based / strategy-based / skill-based sub-levels) — i.e.
the trend is *toward* more explicit typing, not away from it.

Critically, this is not decorative metadata — the type field changes system *behavior*, not just
display. Per a comparative write-up on [Mem0 vs. Letta](https://vectorize.io/articles/mem0-vs-letta):
- **Retrieval differs by type.** Letta's Recall Memory (episodic) stores full conversation history
  with date/text search and stays *out-of-context by default*, fetched only on demand; Mem0's episodic
  API stores timestamped events retrieved via a hybrid vector+graph mechanism distinct from its
  semantic-fact retrieval path.
- **Consolidation is formally defined as a type-transition operation.** The
  [Zylos Research piece on memory consolidation](https://zylos.ai/research/2026-04-20-memory-consolidation-ai-agents/)
  describes consolidation as "the episodic-to-semantic transition: losing the specific instance in
  exchange for the generalized rule" — i.e., an episodic memory ("on 2026-07-20 we tried config X and
  it broke the build") gets *distilled* into a semantic fact ("config X is incompatible with Y") during
  consolidation, while a *procedural* memory (a working fix, a runbook) is not the same kind of
  transformation target at all — it's kept and refined in place, not abstracted-and-discarded like an
  episodic instance.
- Letta's three-tier model (core/archival/recall) maps cleanly onto working/semantic-procedural/episodic,
  and treats paging between them as an agent-directed action, not a passive background job.

**Grounding takeaway for SALTMDB**: SALTMDB's current `consolidation` mechanism (raw → consolidated,
soft-archiving parents) is exactly the kind of operation that *should* behave differently depending on
memory type, but today has no type signal to condition on. A "we tried X, it failed" episodic memory
and "X is broken because Y" semantic fact are currently indistinguishable rows to the consolidation
logic — both are just markdown blobs with tags.

### 2. Library/information-science classification: faceted vs. enumerative hierarchy

S. R. Ranganathan's **Colon Classification** (1933) was explicitly proposed as an alternative to the
Dewey Decimal Classification (DDC). The core distinction
([Wikipedia: Faceted classification](https://en.wikipedia.org/wiki/Faceted_classification);
[Hedden Information Management](https://www.hedden-information.com/faceted-classification-and-faceted-taxonomies/);
[LIS Edu Network: Colon Classification](https://www.lisedunetwork.com/colon-classification/)):

- **Enumerative systems** (DDC, LCC) pre-list every valid subject as a fixed slot in one master tree.
  Every book gets exactly one shelf position. Adding a genuinely new subject area that doesn't fit the
  existing tree can require renumbering large swaths of the schema.
- **Faceted (analytico-synthetic) systems** decompose a subject into independent **facets** — orthogonal
  axes such as Personality, Matter, Energy, Space, Time (Ranganathan's "PMEST" formula) — and a given
  item is described by *combining* values from each facet rather than being dropped into one branch of
  a single tree. Ranganathan's stated critique of DDC-style hierarchy was that it is "too limiting and
  finite" for a fast-growing, unpredictable universe of subjects; faceted systems are "more hospitable
  to new subjects" because a new facet value can be added without restructuring the whole schema.

**Applicability to SALTMDB**: SALTMDB's corpus is multi-agent-authored (Claude, Antigravity/Gemini,
human owner) and grows continuously and unpredictably — structurally the opposite of a closed catalog
a librarian curates up front. A single enumerative tree (e.g., a rigid subject hierarchy an admin must
extend by hand) would be a poor fit and would recreate exactly the "have to renumber everything when a
new domain shows up" brittleness Ranganathan was reacting against. A **faceted** model — a small number
of *independent, orthogonal* classification axes (e.g., `memory_type` × `domain` × existing freeform
`tags` × existing `scope`) each with their own small vocabulary — is the better structural fit: axes can
be added or extended independently, and no single axis has to capture the whole of "what is this."
This is directly actionable: it argues for *multiple thin typed columns*, not one deep hierarchical
`category` tree.

### 3. Structured entity typing in modern knowledge systems (schema.org, Wikidata, Notion)

- **schema.org** models the world as a class hierarchy under one root, `Thing`, with types allowed
  multiple inheritance ("Classes are arranged in a multiple inheritance hierarchy where each class can
  be a subclass of multiple other classes") — [meta.schema.org data model](https://meta.schema.org/docs/datamodel.html).
  Explicitly, schema.org's authors say the hierarchy is *not* meant to be "a global ontology of the
  world" — it's a pragmatic, extensible, "good enough for search engines" typing layer, not a
  philosophically complete taxonomy. That pragmatism (a shallow, useful type system beats a
  theoretically pure one that nobody finishes) is the relevant lesson, not the specific type list.
- **Wikidata** structures its graph with `instance of` (P31) and `subclass of` (P279) as the two core
  typing predicates, distinguishing individual items from classes
  ([Wikidata:WikiProject Ontology/Modelling](https://www.wikidata.org/wiki/Wikidata:WikiProject_Ontology/Modelling)).
  This is a heavier ontology-engine model than SALTMDB needs — full class/subclass/instance reasoning
  is explicitly out of scope for this initiative per the task brief.
- **Notion**'s pattern is the most directly transferable: a **database** is a typed-property container
  (status, owner, priority, dates — small, closed, structured fields) whose rows can each expand into a
  **free-form page** underneath for the actual prose content
  ([Notion Help: Intro to databases](https://www.notion.com/help/intro-to-databases)). This is
  precisely "typed metadata envelope + freeform body," which is already SALTMDB's shape (structured
  columns + markdown `content`) — the finding is that adding 1–2 more *narrow, closed-vocabulary*
  columns is consistent with how Notion scales structure without turning every page into a rigid form.

**Lightweight, SQLite-friendly takeaway**: the useful pattern from all three is *not* "build a class
hierarchy" — it's "keep a handful of small closed-enum columns (schema.org-style pragmatic shallow
typing, Notion-style typed properties) alongside the freeform body," explicitly avoiding Wikidata-style
formal subclass reasoning, which would be over-engineering for SALTMDB's scale.

### 4. Automatic/unsupervised categorization via topic modeling and embedding clusters

SALTMDB already computes dense embeddings for every entity (per the grounding brief), which makes
unsupervised clustering a live option rather than a hypothetical.

- **BERTopic** (embedding-based topic modeling: SBERT embedding → UMAP dimensionality reduction →
  HDBSCAN clustering → c-TF-IDF labeling) is the current mainstream successor to LDA, and per
  [Chamomile AI's comparative overview](https://chamomile.ai/topic-modeling-overview/) generally
  outperforms LDA on coherence for short, informal documents (which memory notes typically are).
  It supports incremental/dynamic operation: **BERTrend** ([arXiv:2411.05930](https://arxiv.org/pdf/2411.05930))
  processes new documents in time-sliced batches, extracts topics per batch, and *merges* them into a
  cumulative topic set via a similarity threshold — i.e., topics are expected to evolve/split/merge
  over time rather than being fixed at initial-fit time, and BERTopic explicitly supports visualizing
  that drift.
- **Cluster stability in production** is measurable, not just theoretical: a practical write-up on
  [HDBSCAN clustering of text embeddings](https://www.cognitivetoday.com/2026/07/clustering-unstructured-text-hdbscan/)
  reports run-to-run stability (normalized mutual information) exceeding 0.90, and HDBSCAN's design
  explicitly favors *not* forcing every point into a cluster — ambiguous items get labeled "noise"
  rather than mis-bucketed, which is important for an append-heavy store like SALTMDB where forcing a
  new, genuinely novel memory into an existing cluster would be worse than leaving it unclustered.
  A related [Towards Data Science piece on clustering sentence embeddings for intent discovery](https://towardsdatascience.com/clustering-sentence-embeddings-to-identify-intents-in-short-text-48d22d3bf02e/)
  demonstrates the same embedding→cluster→human-labels-the-cluster workflow applied to short informal
  text, which is a closer analogue to memory titles/snippets than long-document topic modeling.
- **Maturity assessment**: the approach is production-viable as a *suggestion* mechanism (propose
  candidate domain labels a human/agent confirms or edits) but is **not** mature enough to be an
  authoritative, silent auto-tagger. Clusters drift, merge, and split as new documents arrive; cluster
  *identity* (which cluster is "the same domain as before") is not guaranteed stable across re-fits
  without explicit merge-tracking machinery (exactly what BERTrend adds on top of vanilla BERTopic).
  For a small-to-medium personal/team memory store, re-clustering cost is cheap, but a naive
  "auto-assign domain = nearest cluster centroid" without a human-in-the-loop confirmation step risks
  silently relabeling old memories' domains every time the corpus grows enough to shift cluster
  boundaries — a stability problem, not an accuracy problem per se.

### 5. Domain/subject hierarchies in PKM and enterprise knowledge tools

- **Confluence** enforces a strict top-level-container + parent-child page tree (one Space per
  team/project); this "hierarchy enforces consistency… every page has a clear location," which scales
  well for large, permission-governed enterprises but is rigid
  ([eesel AI: Confluence vs Notion](https://www.eesel.ai/blog/confluence-vs-notion)).
- **Notion** workspaces skip the enforced tree and rely on databases with typed properties (including a
  "category"/"area" property) plus tags for cross-cutting filtering — flexible but "without discipline…
  can become chaotic as they grow" (same source).
- **Obsidian's Maps of Content (MoC)** pattern, and the broader PKM-community consensus documented at
  [dsebastien.net](https://www.dsebastien.net/2022-05-15-maps-of-content/) and
  [Shuvangkar Das's note-organization writeup](https://blog.shuvangkardas.com/obsidian-note-organization/),
  converges on an explicit **three-layer hybrid**: "folders for high-level separation, MOCs for
  connection and navigation inside each folder, and tags for discovery and filtering across the entire
  vault" — summarized as "folders answer *where does this belong?*, tags answer *what is this about?*."
  Some heavier setups add a full `domain > category > subcategory > topic > subtopic` tag hierarchy,
  but the community's more common finding is a **two-layer split**: one coarse, small, structural axis
  (folder/MOC/domain) plus one fine-grained, unbounded, freeform axis (tags) — deliberately *not*
  collapsing both jobs onto the tag system alone.
- The **Zettelkasten** method (Luhmann) offers a distinct but related lesson: it types notes by
  *processing stage/purpose* — fleeting (raw, disposable), literature (source-linked), permanent
  (reusable claim) — rather than by subject at all
  ([Better Humans: Zettelkasten's 3 note-taking levels](https://medium.com/better-humans/zettelkastens-3-note-taking-levels-help-you-harvest-your-thoughts-58326840f969)).
  This maps suggestively onto SALTMDB's existing `status` field (raw → consolidated) — SALTMDB already
  has a *lifecycle*-type axis; what it lacks is a *content-kind* axis (fact/event/procedure/decision)
  and a *subject* axis (domain), which are different dimensions entirely from lifecycle stage.
- **Architecture Decision Records (ADRs)** ([adr.github.io](https://adr.github.io/)) are the most
  concrete existing prior-art for the "decision" memory type specifically: a decision record is
  structurally distinct from a fact or a how-to — it has Context, Decision, Consequences, Alternatives
  Considered, and Status (proposed/accepted/superseded/deprecated) as near-universal fields. This is
  independent confirmation that "decision" deserves to be its own recognized kind, not merely a tag,
  because it has a recognizably different internal shape (rationale + alternatives + supersession
  status) than a plain fact does.

**Takeaway**: real systems converge on *combining* one coarse structural axis with an unbounded freeform
axis — never one or the other alone. SALTMDB today has only the freeform axis (tags) and is missing the
coarse structural layer(s) that Confluence/Obsidian/Notion all provide in some form.

### 6. Is a fixed `entity_type`/`domain` enum column good practice or an anti-pattern?

Database-design literature is split depending on *how* the type column is used:

- **Anti-pattern framing** applies specifically to (a) using a single shared "generic entity + type
  string" table to avoid ever adding real columns (the classic EAV/polymorphic-association smell —
  see [DoltHub: Choosing a Database Schema for Polymorphic Data](https://www.dolthub.com/blog/2024-06-25-polymorphic-associations/)
  and [The Art of PostgreSQL: Database Modelization Anti-Patterns](https://tapoueh.org/blog/2018/03/database-modelization-anti-patterns/)),
  and (b) native SQL `ENUM` types specifically, because "there's no syntax to add or remove a value
  from an ENUM — you can only redefine the column," which is brittle under schema evolution. The
  recommended fix for (b) is a small lookup/reference table (or, in SQLite terms, a plain `TEXT` column
  with an app-level allow-list) rather than a native enum type — giving the openness to extend the
  vocabulary without a migration.
- **Good-practice framing** applies to "class table inheritance"-style designs: "a central table
  [with] a 'type' column… [while] per-type fields live on separate tables that can be joined as
  necessary" (same DoltHub piece). This is the opposite of an anti-pattern — it's the standard way to
  give rows a lightweight, structurally-meaningful category without going full EAV.

The synthesis relevant to SALTMDB: a `memory_type` or `domain` column is **not inherently** an
anti-pattern — the risk is entirely in implementation choice. A *small, closed, rarely-changing*
vocabulary (4–6 values, changed by a code review, not by agents at write-time) stored as plain `TEXT`
with an app-level `CHECK`/allow-list (not a rigid SQL `ENUM`) is the good-practice shape; a large,
agent-extensible, unbounded vocabulary living in the same column would just recreate tag fragmentation
under a different column name — that is the "tag-fragmentation-2.0" failure mode this plan must avoid
(see Risks section below).

---

## Current SALTMDB State

As given in the task brief, and confirmed as the baseline for this analysis:

- Every row is one undifferentiated "entity" — there is no structural distinction between a fact, an
  event, a procedure, or a decision record. All of that differentiation, if it exists at all today,
  is smuggled into freeform markdown content and tag strings.
- The only structural fields that exist are: `status` (raw / consolidated / archived — a **lifecycle**
  axis, not a content-kind axis), `is_core` (a boolean importance flag), `scope` (private / shared —
  an **access** axis), and freeform `tags` (an unbounded, agent-assigned vocabulary).
- There is no subject/domain hierarchy of any kind — no coarse "this belongs to project X /
  subsystem Y" field independent of tags.
- There is no entity-kind typing (person / project / decision / bug / component) distinguishing
  *what the entity is about* from *what the entity says*.
- Consequently, tags today carry the entire load of every classification job at once: subject matter,
  content kind, importance signaling (informally), and ad hoc cross-referencing — all flattened into
  one unstructured string list per entity.

## Gaps / Problems

1. **No content-kind axis means retrieval and consolidation can't distinguish memory kinds that
   *should* behave differently.** As the CoALA/Mem0/Letta research shows, an episodic memory
   ("we tried X on 2026-07-20, it broke the build") and a semantic fact ("X is incompatible with Y")
   are meant to be treated differently by consolidation (episodic → semantic distillation), by decay
   policy (old episodic instances are more disposable than durable facts), and by retrieval ranking
   (a "how do I do X" query should prefer procedural entries; a "what happened when we tried X" query
   should prefer episodic entries). SALTMDB's consolidation logic today has no signal to make any of
   these distinctions — it treats every raw entity identically regardless of whether it's a log of an
   event, a durable fact, a runnable procedure, or a decision rationale.

2. **Decisions specifically lose their distinguishing shape.** ADR practice shows a decision record
   has a recognizably different internal structure (context, alternatives considered, consequences,
   supersession status) than a plain fact. Today a "we decided to use SQLite over Postgres because…"
   memory is stored exactly like any other note — nothing marks it as needing an alternatives/rationale
   shape, nothing signals it can be *superseded* by a later decision (versus merely archived), and
   nothing lets an agent cheaply query "show me all decisions about the storage layer" without a
   full-text or tag-based guess.

3. **No subject/domain axis independent of tags means every "what area is this about" query is a tag
   query, at tag-fragmentation risk.** Per the PKM research (Obsidian MoC, Confluence Spaces, Notion
   databases), real systems universally split "coarse area/domain" from "fine-grained freeform tag" —
   SALTMDB currently has only the fine-grained layer, so a query like "everything about the MCP tools
   layer" depends entirely on tag discipline that the sibling tag-governance research track is already
   flagging as fragile.

4. **No cheap way to auto-surface an emerging domain/category.** SALTMDB already computes embeddings
   per entity but does nothing with the embedding space structurally — it's used only for retrieval
   (RRF hybrid search), not for corpus-level structure discovery. This is a missed opportunity: as the
   BERTopic/HDBSCAN research shows, the embeddings already being computed at write time are sufficient
   input for an unsupervised "what domains/clusters exist in this corpus right now" pass, at effectively
   zero marginal embedding cost — SALTMDB just isn't running that pass.

5. **Lifecycle (`status`) is being asked to imply content-kind, and it can't.** `status=raw` conflates
   "freshly logged event," "freshly stated fact," and "freshly recorded decision" into one bucket, and
   `status=consolidated` conflates "distilled fact," "confirmed procedure," and "finalized decision"
   into another. The Zettelkasten fleeting/literature/permanent distinction is a *processing-stage* axis
   analogous to SALTMDB's `status` — useful, but orthogonal to, and no substitute for, a content-kind
   axis.

6. **Cost in practice**: agents (Claude, Antigravity, human) currently have no cheap, structured way to
   ask "give me only the how-to memories for component X" or "give me only decisions, not facts, about
   the schema" — every such query degrades to full-text/tag search over undifferentiated markdown,
   which is slower, noisier, and more dependent on the querying agent guessing the right tag strings
   than a structured filter would be.

## Proposed Improvement Plan

### Phase 1 — Quick Wins (additive, non-breaking)

- **Add a `memory_type` column** to the entities table in `schema.py`: a plain `TEXT` column (not a
  native SQL `ENUM`, per the anti-pattern research above) with a small, fixed, code-reviewed vocabulary
  — start with exactly four values: `fact` (semantic — durable, generalized knowledge), `event`
  (episodic — something that happened, ideally timestamped), `procedure` (how-to / runbook / skill),
  `decision` (rationale record — what was chosen and why, with room to note what it supersedes). Default
  to `fact` for backward compatibility with every existing row (a nullable/defaulted column is a
  zero-downtime additive migration, not a breaking change).
- **Surface it in `store_memory`** (`memory_service.py` / `mcp/tools.py`): add an optional `memory_type`
  parameter, defaulting to `fact` when omitted so existing callers (agents that don't know about the new
  field yet) keep working unchanged. Validate against the fixed vocabulary in the service layer (an
  app-level allow-list, matching the "lookup-table, not native enum" good-practice pattern) rather than
  a DB-level `CHECK` that would require a migration to extend later.
- **Surface it as an optional filter in `search_memory`**: `memory_type_filter` alongside the existing
  `tags_filter`, so a query can ask "only procedures" or "only decisions" without depending on tag
  discipline.
- **For `decision`-typed memories specifically**, lightly borrow the ADR shape: encourage (not enforce)
  a content template with Context / Decision / Alternatives / Consequences headings in the markdown
  body — this is a documentation convention, not a schema change, so it costs nothing structurally but
  captures most of the ADR benefit.
- **Log a `#core` memory documenting the new column and its four-value vocabulary** immediately after
  shipping, so agents (Claude and Antigravity both) discover it via the existing `search_memory
  tags_filter=['#core']` bootstrap step rather than guessing it exists.

### Phase 2 — Medium-Term (embedding-cluster-assisted domain suggestion)

- **Add a `domain` column**, same shape as `memory_type` (plain `TEXT`, small vocabulary, defaulted/
  nullable), representing the coarse "what area/subsystem/project is this about" axis — independent of
  both `memory_type` (content kind) and `tags` (fine-grained freeform detail). This is the second facet
  in the faceted-classification sense: orthogonal to content-kind, not a sub-level of it.
- **Run an offline/periodic clustering pass over the existing embedding space** (SBERT-style embeddings
  SALTMDB already computes) using UMAP + HDBSCAN, mirroring the BERTopic pipeline. Because SALTMDB's
  corpus is comparatively small and personal/team-scale (not web-scale), this can be a cheap batch job
  (e.g., a maintenance script or a periodic `store_memory`-adjacent background task), not a live service.
- **Treat cluster output as *suggestions*, never as silent authority.** Per the maturity assessment
  above (clusters drift/merge/split as the corpus grows; run-to-run stability is high but not perfect),
  the clustering pass should propose "these N entities look like they share domain X" to a human or to
  an agent for confirmation — e.g., surfaced as a new `log_event(type='domain_suggestion', ...)` an
  agent can review during a cognitive-sweep pass — rather than auto-writing the `domain` column. This
  avoids the single biggest failure mode from the research (silent re-labeling of old memories' domains
  every time cluster boundaries shift as new documents insert).
- Consider tracking cluster identity across re-fits (BERTrend's merge-by-similarity-threshold approach)
  only if/when the corpus is large enough that re-running clustering from scratch each time becomes
  actually expensive or the naive re-fit starts producing visibly unstable domain suggestions — don't
  build merge-tracking machinery pre-emptively.

### Phase 3 — Larger Bet (faceted multi-axis classification, evaluate only if Phase 1/2 prove insufficient)

- Formalize the full faceted model explicitly: `memory_type` × `domain` × `tags` × `scope` × `status`
  as five independent, orthogonal axes, each with its own small (or, for `tags`, deliberately unbounded)
  vocabulary — the Ranganathan PMEST-style approach rather than ever collapsing back into one
  hierarchical `category` tree. Concretely this could mean:
  - A `facets` or `axes` concept surfaced explicitly in `search_memory`'s parameter list (rather than
    ad hoc filters bolted on one at a time), so new axes can be added later without repeatedly touching
    the tool signature.
  - Optional per-`memory_type` structured sub-fields (e.g., a `decision` entity gaining an explicit
    `supersedes_entity_id` field, populated via the existing `manage_relation` graph edges with a
    `supersedes` predicate rather than a new column) — this leans on SALTMDB's existing graph-relation
    mechanism instead of adding more flat columns, keeping the "few structured fields, not an ontology
    engine" constraint from the task brief.
  - This phase should be treated as speculative and revisited only after Phase 1/2 usage data shows
    real query patterns that a flat two-column addition can't serve — do not build it pre-emptively.

## Risks & Trade-offs

- **"Tag-fragmentation-2.0" is the central risk, and it is real if the vocabulary is left open.** The
  entire justification for `memory_type`/`domain` as *columns* rather than *more tags* is that their
  vocabularies stay small, fixed, and centrally governed — the moment agents are allowed to invent new
  `memory_type` or `domain` values ad hoc at write time, the column degenerates into exactly the same
  fragmentation problem tags already have, just duplicated across two places instead of one. Mitigation:
  enforce the allow-list in the service layer (`memory_service.py`), require a deliberate code change
  (not an agent-time decision) to add a new value to either vocabulary, and keep both vocabularies small
  (4–6 values) by design — closer to schema.org's "pragmatic, not a global ontology" philosophy than to
  Wikidata's open class system.
- **Native SQL `ENUM` types are themselves an anti-pattern for a still-evolving vocabulary** (per the
  DoltHub/tapoueh research) — use plain `TEXT` + app-level validation instead, so extending the
  vocabulary later is a code change, not a schema migration.
- **Backward compatibility**: both new columns must be nullable/defaulted so every pre-existing row and
  every caller unaware of the new fields keeps working unchanged — this is explicitly required by the
  task framing ("additive column, not a breaking change") and is standard practice for schema evolution.
- **Clustering-suggested domains carry a distinct risk from manual tags**: instability across re-fits
  could cause an agent to see a different suggested domain for the same entity on two different days.
  This is why Phase 2 treats cluster output as a suggestion surfaced via `log_event`, never as an
  auto-write — the human/agent confirmation step is not optional overhead, it's the guardrail that
  prevents silent domain churn.
- **Over-engineering risk on the opposite side**: adopting a Wikidata-style full class/subclass/instance
  ontology, or a Confluence-style enforced strict hierarchy, would be disproportionate to SALTMDB's
  scale and would reintroduce the enumerative-hierarchy brittleness Ranganathan's faceted critique
  specifically warns against (rigid trees don't accommodate open-ended, multi-agent-authored growth).
  The faceted, few-small-orthogonal-axes model is deliberately the more conservative choice here.
- **Two new axes still means two more things agents must remember to set correctly.** Defaulting
  `memory_type='fact'` and leaving `domain` nullable until Phase 2 ships keeps the immediate cognitive
  load on agents close to zero; the cost is paid gradually as the vocabulary proves useful, not all at
  once.

## Open Questions for zbalint

1. Does the four-value `memory_type` vocabulary (`fact` / `event` / `procedure` / `decision`) match how
   you actually think about your own memories, or would you want a fifth value (e.g., `preference` for
   user/agent working-style preferences, which today get folded into `fact`)?
2. Should `domain` in Phase 2 be scoped to *project/repo* granularity (e.g., "SALTMDB", "CADET") or
   *subsystem* granularity (e.g., "SALTMDB.schema", "SALTMDB.mcp-tools") — the former is a much smaller,
   more stable vocabulary; the latter is more useful for retrieval but closer to tag-fragmentation risk.
3. For `decision`-typed memories, do you want `supersedes`/`superseded_by` enforced structurally (a real
   relation predicate agents must set) or left as a documentation convention inside the markdown body,
   at least initially?
4. Is a periodic embedding-clustering job (Phase 2) worth the added moving part at SALTMDB's current
   corpus size, or should that wait until the corpus is demonstrably large enough that manual domain
   assignment is becoming a bottleneck?
5. Should this `memory_type`/`domain` work be sequenced before or after the sibling tag-governance
   track's changes land, given both tracks touch `store_memory`'s parameter surface and ideally should
   ship as one coherent schema/tooling update rather than two overlapping migrations?

## Sources

- [Cognitive Architectures for Language Agents (CoALA), arXiv:2309.02427](https://arxiv.org/pdf/2309.02427)
- [Atlan — Types of AI Agent Memory: Episodic, Semantic, Procedural and More](https://atlan.com/know/types-of-ai-agent-memory/)
- [Atlan — Episodic Memory for AI Agents: How It Works](https://atlan.com/know/episodic-memory-ai-agents/)
- [Agent-Memory-Paper-List (tracking "Memory in the Age of AI Agents" survey, arXiv:2512.13564)](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
- [Vectorize — Mem0 vs Letta (MemGPT): AI Agent Memory Compared](https://vectorize.io/articles/mem0-vs-letta)
- [Zylos Research — Memory Consolidation in Long-Running AI Agents](https://zylos.ai/research/2026-04-20-memory-consolidation-ai-agents/)
- [Wikipedia — Faceted classification](https://en.wikipedia.org/wiki/Faceted_classification)
- [Hedden Information Management — Faceted Classification and Faceted Taxonomies](https://www.hedden-information.com/faceted-classification-and-faceted-taxonomies/)
- [LIS Edu Network — A Brief Information About Library Colon Classification](https://www.lisedunetwork.com/colon-classification/)
- [Schema.org — Data model (meta.schema.org)](https://meta.schema.org/docs/datamodel.html)
- [Wikidata:WikiProject Ontology/Modelling](https://www.wikidata.org/wiki/Wikidata:WikiProject_Ontology/Modelling)
- [Notion Help — Intro to databases](https://www.notion.com/help/intro-to-databases)
- [Chamomile AI — Topic Modeling: A Comparative Overview of BERTopic, LDA, and Beyond](https://chamomile.ai/topic-modeling-overview/)
- [BERTrend: Neural Topic Modeling for Emerging Trends Detection, arXiv:2411.05930](https://arxiv.org/pdf/2411.05930)
- [dsebastien.net — Maps of Content (MoC): The Complete Guide for PKM](https://www.dsebastien.net/2022-05-15-maps-of-content/)
- [Shuvangkar Das — Obsidian Note Organization: Folders vs MOCs vs Tags](https://blog.shuvangkardas.com/obsidian-note-organization/)
- [DoltHub Blog — Choosing a Database Schema for Polymorphic Data](https://www.dolthub.com/blog/2024-06-25-polymorphic-associations/)
- [The Art of PostgreSQL — Database Modelization Anti-Patterns](https://tapoueh.org/blog/2018/03/database-modelization-anti-patterns/)
- [eesel AI — Confluence vs Notion: Which knowledge base tool is right for your team](https://www.eesel.ai/blog/confluence-vs-notion)
- [Architectural Decision Records (adr.github.io)](https://adr.github.io/)
- [Better Humans (Medium) — Zettelkasten's 3 Note-Taking Levels](https://medium.com/better-humans/zettelkastens-3-note-taking-levels-help-you-harvest-your-thoughts-58326840f969)
- [Cognitive Today — Clustering Unstructured Text: HDBSCAN & Embeddings Guide](https://www.cognitivetoday.com/2026/07/clustering-unstructured-text-hdbscan/)
- [Towards Data Science — Clustering Sentence Embeddings to Identify Intents in Short Text](https://towardsdatascience.com/clustering-sentence-embeddings-to-identify-intents-in-short-text-48d22d3bf02e/)
