# Agentic Workflow & Memory: SALTMDB in Practice

This document is a field report from actually running SALTMDB, day to day, as the shared memory
layer for a multi-agent coding workflow (Claude Code + Google Antigravity/Gemini CLI + GitHub
Copilot CLI, with implementation delegated to an [oh-my-pi](https://github.com/can1357/oh-my-pi)
("OMP") orchestrator running small/cheap "Luna" models). It covers three things people keep
asking about once they see the project:

1. **How the memory system is actually used** — the hooks, the write/search discipline, the
   lifecycle from raw note to consolidated knowledge.
2. **What the numbers say** — real, measured retrieval-accuracy and latency benchmarks against
   the live corpus, not a marketing claim.
3. **The agentic workflow memory makes possible** — a two-tier "strong model plans and reviews,
   cheap model implements" pipeline, with memory as the thing that lets the strong model *not*
   re-derive or re-argue the same decisions every session.

Everything below is drawn from the project's own SALTMDB instance — decisions, benchmark
results, and incident write-ups the agents stored as they went. Session IDs, absolute local
paths, and anything else personal to one machine have been left out; the numbers, architecture,
and workflow are the same regardless of whose machine runs it. For install/setup, see
[`INSTALL.md`](INSTALL.md); for the full agent-facing API and design rationale, see
[`AGENT_GUIDE.md`](AGENT_GUIDE.md); for the full feature/architecture list, see the main
[`README.md`](README.md).

---

## 1. Architecture, in one paragraph

SALTMDB is a local-first MCP memory server: one SQLite database (WAL mode), one backend daemon
per DB path that is the *only* process ever allowed to open it, and one or more thin per-agent
MCP adapters talking to that daemon over a loopback RPC socket. Retrieval is hybrid: SQLite
FTS5/BM25 keyword search and `BAAI/bge-small-en-v1.5` dense-vector search (384-dim, ONNX via
`fastembed`, no PyTorch runtime) run in parallel and are combined with weighted Reciprocal Rank
Fusion; an optional cross-encoder can supply a final reordering pass when configured. Writes go
through a quality gate (entropy/repetition/readability/structure checks — mostly advisory, not
blocking), a secrets-redaction pass, and duplicate detection (exact content-hash rejection, then
a bundled cross-encoder judging near-duplicates). Memory identity is immutable: revising or
superseding a memory always archives the old content byte-for-byte under its existing ID and
links a new entity to it — nothing is ever silently overwritten. Relations between memories are
typed and bi-temporal (system time *and* independently settable event/world time). None of this
requires an LLM at retrieval time — it's arithmetic and a small local embedding model, which is
why it stays fast and works offline.

---

## 2. What it's actually like to use, from the agent's side

Everything above is measured from the outside. This section is the other view: what running on
SALTMDB session after session actually feels like from inside the agent doing the work — this
document included, since it was assembled the same way any other task here would be.

**The part that's genuinely load-bearing:** the difference between starting a session with real
project history already in hand versus starting one with none. Without persistent memory, every
session begins by re-deriving or re-asking things that were already settled — what the retrieval
priority is, why an earlier approach was rejected, what the current default configuration even
is. With it, that digest arrives before the first response is even written (the last-session
summary at the top of this exact conversation is a live example, not a hypothetical one), and
searching a component before touching it routinely turns up the actual reason something is built
the way it is — not a guess reconstructed from reading code cold. Concretely, while writing this
document, memory search surfaced the exact rationale for why a higher-precision deduplication
candidate was *rejected* despite winning its own evaluation (§5) — without that, the natural thing
to do would have been to propose almost the same fix again, since the code alone doesn't explain a
decision that was made and explicitly *not* encoded back into the source.

**The enforcement hook is annoying in exactly the way it's supposed to be.** The `PreToolUse`
search gate described in §4 blocked a routine shell command partway through gathering material for
this very document, with no room to argue past it — and that's the honest point of it. Agent
instructions alone are the kind of thing that's easy to skip under time pressure or when a task
feels simple enough not to bother; a hard gate doesn't offer that option. It cost a few seconds
and one extra tool call here. It's a reasonable trade against skipping the search on the one time
it would have actually mattered.

**Where it's genuinely useful, not just process theater:** catching that a workflow preference is
*already decided* rather than open for debate — for instance, "should the OMP advisor be disabled
to save cost" is not a live question to re-litigate every time budget comes up, because it's
already answered, with evidence, in memory (§6). Re-deriving that from first principles every
session would waste real budget on a question that's already closed. The same goes for smaller
things: a rejected library choice, a latency budget, a naming convention — the kind of fact that's
easy to forget was ever decided and cheap to accidentally re-decide differently.

**Where it's genuinely friction, not sugar-coated:** search occasionally returns a cluster of
near-identical session-handover notes on a broad or generic query — the same `event`-type
weak-spot flagged by the audit numbers in §5 isn't an abstract statistic, it's a thing that
happens in practice and costs a moment sorting through which of several similar-looking notes is
actually the current one. Writing a new memory sometimes comes back flagged with
`duplicate_candidates` against something already close enough in content — correct behavior, since
the alternative is silent duplication, but it does mean pausing to decide whether to supersede the
existing one or the new note is genuinely distinct. And the scarcity of `is_core` cuts both ways:
it keeps the always-injected digest small and actually readable, but it also means a rule that
*should* have been promoted can sit as an ordinary searchable memory instead — which is exactly
what happened with one safety-critical override in this project's own history, re-explained twice
before someone caught that it needed the core tier (§4 covers this directly, not as a hypothetical
risk).

**The honest net assessment:** it's helpful, specifically because the realistic alternative isn't
"a perfectly-briefed agent with the same knowledge some other way" — it's an agent that starts
cold every session and either re-derives settled decisions at real token cost, or worse, quietly
drifts from them without anyone noticing until the drift causes a real problem. Measured against
that actual baseline, not an idealized one, persistent, searchable, server-enforced memory is a
clear net win for this workflow — with the specific, named rough edges above, not despite pretending
they don't exist.

---

## 3. Why store memories *and* events — and what sessions are for

The schema deliberately keeps three different things separate, and conflating them is the most
common way to misuse a system like this:

**Memories** (the `entities` table — what `search_memory` actually searches) are the durable,
curated "worth remembering" layer. Every memory carries a `memory_type` — `fact`, `event`,
`procedure`, `decision`, or `preference` — gets embedded, quality-gated, and checked for
duplicates, and its identity is immutable: revising one always archives the old content
byte-for-byte and links a new entity forward, never silently overwrites it. The point of storing
one is answering a future "have we already solved this, decided this, or hit this exact bug
before?" without re-deriving the answer from scratch — which is the whole reason a two-session-old
decision about, say, a rejected deduplication approach (§5) didn't have to be re-litigated when
this document was written.

**Events** are a *separate*, append-only operational ledger — not curated knowledge, and not
something `search_memory` ranks or returns. Every decision made, issue hit, fix applied, or
attempt tried gets logged there in real time (`log_event`), tagged with a type, the acting agent,
a `context_id` grouping a shared thread of work, and the session that logged it. Three reasons
this exists as its own layer rather than just writing everything straight to memory:

- **A real-time trail that survives a crash.** Logging as things happen, instead of reconstructing
  "what did we do this session" from whatever's still in context at the end, means the actual
  sequence of decisions/issues/fixes is recoverable even if the session never reaches a clean
  wrap-up.
- **Multi-agent coordination without waiting for curation.** A second agent working the same
  `context_id` can call `get_events` and see exactly what another agent just decided or hit,
  without that needing to have been promoted into a polished, searchable memory first — memory
  curation takes judgment and shouldn't gate basic coordination.
- **Ordered synthesis at wrap-up.** Pulling events oldest-first at session close reconstructs the
  whole session chronologically, so the final "what's actually worth keeping long-term" pass into
  `store_memory` is built from a complete record, not a partial one shaped by whatever happened to
  still be in the context window.

The practical pattern this produces: log the raw event as it happens, and only promote the
distilled, durable version into a memory once it's actually resolved — which keeps the searchable
memory graph from filling up with in-progress noise while still preserving the full trail for
anyone who needs to reconstruct exactly what happened and when.

**Sessions** (`_agent_sessions`) are provenance for *which running process touched the database
when* — not knowledge at all. Each adapter session records its ID, working directory, start time,
owning agent, last-activity time, and (if it closed cleanly) an end time — deliberately left null
on a raw connection loss, since the same adapter may reconnect under the same session ID rather
than actually being gone for good. Sessions are what make a few other things in this document
possible, not an incidental bookkeeping detail:

- They're how the `SessionStart` digest hook (§4) knows which session most recently touched *this
  exact working directory*, to build the automatic bootstrap digest.
- Every memory records both which session created it and which session most recently touched it
  as two separate fields — because a memory can legitimately be revisited and updated by a
  session that didn't originally write it, and losing that distinction would make lineage
  unreadable.
- Registration is fail-closed: an adapter's handshake only completes once its session is durably
  committed, so a registration failure means the connection fails closed rather than silently
  proceeding as an unaccounted-for session nothing can later attribute writes to.
- Knowing which sessions are concurrently live in the same working directory is exactly what
  lets multiple agents from different vendors (Claude Code, Antigravity/Gemini CLI, and GitHub
  Copilot CLI, in this project's own deployment) share one database safely at the same time
  instead of racing each other.

---

## 4. Hooks: making memory the default path, not an optional courtesy

The single biggest lesson from running this for real is: **agents will not reliably remember to
use a memory tool on their own, no matter how clearly the instructions say to.** Every hook
described below exists because agent discipline alone was tried first and measurably failed —
this is a direct design principle now:

> Minimize reliance on the calling agent's judgment/diligence for data-safety-critical
> decisions. Agents are variably lazy/low-effort — they cannot be trusted as the primary
> safeguard. The server must protect the data from an agent that skips a step, not just hope the
> agent remembers to double-check.

Concretely, four hooks carry that principle end to end:

- **`SessionStart` digest injection.** At the start of every session (and after context
  compaction), a hook auto-injects the active project's core rules and a digest of the
  most-recent session's key memory titles directly into context — no manual "let me search
  memory first" step required. You can see this at the very top of the transcript that produced
  this document: a `<saltmdb-last-session-digest>` block listing the prior session's decisions
  arrived before any user message was even read.
- **`PreToolUse` "search before you leap" enforcer.** A hook blocks the first file-editing or
  shell command of a session until at least one `search_memory` call has happened, then gets out
  of the way for the rest of the session. This is not a suggestion — it is a hard gate. For
  example, mid-way through gathering material for this very document, a routine `ls`/`head`
  shell command was rejected outright with:

  ```
  SALTMDB Rule 1 (Think Before You Leap): call search_memory for the relevant
  component/task before editing files or running commands. This gate fires once
  per session -- after your first search_memory call, further edits/commands
  this session go through unblocked.
  ```

  That is server-side/harness-side enforcement, not a line in a prompt — the exact pattern the
  design principle above calls for.
- **Stop-time reflection & retrieval-outcome telemetry.** A capped, randomized (1–4 times per
  session, so it behaves like an occasional human check-in rather than nagging on every tool
  call) self-critique prompt asks the agent what it's unsure about before it stops. Separately, a
  lighter always-on gate logs a `retrieval_outcome` event for each search — `used`,
  `irrelevant`, or `insufficient`, with a reason — turning "did that memory actually help" into
  structured, aggregable telemetry instead of a feeling nobody writes down.
- **A strict, opt-in per-message auto-injection contract** (design decided, still selectively
  deployed): inject exactly one validated, high-confidence memory before the agent responds, or
  inject nothing at all — never a context dump of "maybe relevant" results. The point isn't
  volume, it's that useful context arrives automatically when it clearly exists, and nothing
  arrives when it doesn't.

### What the retrieval-outcome telemetry actually shows

The `retrieval_outcome` gate isn't just a design idea — it's been running long enough to pull real
numbers from. Looking at the most recent 200 logged `retrieval_outcome` events (the hook's own
structured self-report, spanning roughly one week and about 79 distinct agent sessions) and
classifying every individual memory citation inside them: roughly **82% were logged as `used`,
~16% `irrelevant`, and the remaining ~2% `insufficient`** (a real result existed but needed a
broader follow-up search). Two caveats worth stating plainly: this is the agent self-reporting on
its own search, not an independent audit, and it's a recent slice, not the whole corpus's
lifetime. With that said, it lines up with the accuracy numbers in §5 rather than contradicting
them — when retrieval misses, the far more common failure is *returning a wrong-but-plausible
hit* (`irrelevant`) rather than *finding nothing at all* (`insufficient`), which is exactly the
shape you'd expect from a system with very high Recall@10 but Top-1 that isn't perfect. The
~79-sessions-in-a-week figure is also its own small data point: this isn't a system evaluated
once and shelved, it's genuinely in continuous daily use.

## Everyday write/search discipline

On top of the hooks, the agent-facing rules that turned out to matter most in practice:

- **Think before you leap** — search memory for the target component/task before any non-trivial
  edit (the same rule the `PreToolUse` hook now enforces mechanically).
- **Circuit breaker on repeated failure** — stop after a command/tool/test fails, especially
  twice in a row; log the issue, search for precedent, form a new plan. Don't loop.
- **Log in real time** — decisions and issues get an event logged as they happen, not batched up
  and reconstructed from memory (irony intended) at the end of a session.
- **Store discoveries immediately** — the moment an issue resolves or a rule gets established, it
  gets written down. An exact-content duplicate hard-fails with the existing entity's ID; a
  near-duplicate always stores anyway and comes back with `duplicate_candidates` so the writer
  can decide whether to supersede or consolidate, rather than silently blocking the write.
- **`is_core` is scarce on purpose** — a hard cap (5 active memories, 2,500 chars each, a
  15,000-char rendered digest) reserved for urgent cross-session hazards and active bugs an agent
  must know before it could reasonably search for them. It is explicitly *not* a general
  "important stuff" tier — that's what titled, searchable, well-tagged normal memories and the
  relation graph are for. This project has its own scar tissue here: the same safety-critical
  override got told to an agent, forgotten, and had to be re-explained twice before someone
  thought to promote it to `is_core` — now written down as a standing example of what *should*
  have been core from the first time it was said.

---

## 5. Performance & accuracy: the actual measured numbers

Every figure below comes from a benchmark run against the live corpus or a held-out query set,
with raw artifacts and report files checked in under `reports/`. Nothing here is a synthetic
leaderboard number — and where a number *does* come from someone else's self-reported benchmark
(the competitive-landscape section), it's labeled as such.

### Live known-item retrieval audit (500 memories × 3 queries)

A live audit tested 500 distinct active memories (of 641 in the corpus at the time — a
recency-biased 78% sample, not a uniform random one) with three deterministic queries each —
exact title, compressed title keywords, and a body-snippet phrase — through the public
`search_memory` MCP tool, `broad` mode, 1,500 calls total, zero errors:

| Metric | Result |
|---|---|
| Top-1 accuracy | **89.27%** (1,339 / 1,500) |
| Recall@3 | 98.53% |
| Recall@5 | 99.20% |
| Recall@10 | **99.40%** (1,491 / 1,500) |
| MRR@10 | 0.9384 |

Broken out by query style: exact-title queries scored 88.80% Top-1 / 100% Recall@10;
title-keyword queries 86.60% / 99.60%; body-phrase queries 92.40% / 98.60% — body phrases had the
best raw Top-1 but the worst Recall@10 tail, i.e. when a body-phrase query missed, it tended to
miss further down the list. By memory type, `event` memories were the weakest slice (80.70%
Top-1), traced to dense clusters of near-identical handover/progress-history notes competing for
the same rank — this is flagged below as a real, still-open shortcoming, not swept under the rug.

Latency (measured under 3 concurrent workers, up to 6 concurrent calls each — a concurrent-audit
timing, not an isolated single-call one): mean 1,559.9 ms, median 1,413 ms, p95 3,577 ms, max
5,320 ms.

### Held-out benchmark: broad vs. strict retrieval mode (100 fresh queries, 60 negatives)

A separate benchmark, run later against a fresh held-out query set (queries not reused from the
live audit above) to compare the two retrieval modes:

- **`broad` mode** (ordinary hybrid retrieval): Top-1 72/100, Top-10 95/100 on the positive set.
- **`strict` mode** (relevance-abstention retrieval, meant for the auto-injection hook): Top-1
  30/100, Top-10 40/100 on the *same* positive set — and on 60 fresh no-answer negative queries,
  it correctly abstained (returned nothing) on 42/60 (70%), with the weakest slice being
  lexical-overlap false premises (9/20 correctly abstained).

The conclusion drawn from this, and now a standing decision: **`broad` stays the default for
ordinary agent work; `strict` is reserved for the auto-injection hook path where a wrong or
irrelevant injected memory is worse than injecting nothing.** Strict mode discarded 55 targets
that broad found in its own Top-10 — that's the real cost of relevance-abstention, and it's an
explicit, evidence-backed tradeoff rather than an assumption.

### Deduplication: a real precision win, honestly bounded

A branch-per-approach rewrite of near-duplicate detection produced two candidates. Candidate 1
(cross-encoder-as-final-judge — score raw title+content prose with the bundled cross-encoder, no
assumption about formatting) shipped. Candidate 2 scored better in its own isolated evaluation
but was **rejected after merge review**, specifically because its precision gain came from
parsing this project's own memory-title convention (`[Project] Phase N ...`) — a signal that
would silently no-op on any other SALTMDB deployment that doesn't title memories that way. That
violates a standing product constraint (SALTMDB is domain-neutral by design — see below) even
though the eval numbers looked good. The residual gap Candidate 2 was chasing — a "hub memory
with generic prose" pattern producing roughly 35–50% false positives at the current cross-encoder
threshold — is real and still open; the lesson kept for next time is that any future fix has to
use signals every memory has regardless of title formatting (type, scope, context, tags,
timestamps, relation-graph evidence), not text-format heuristics.

### The standing priority: accuracy first, latency second, and why

This is a direct, explicit product decision, not incidental: **retrieval accuracy takes priority
over latency/throughput.** A latency increase is accepted when it produces a meaningfully more
correct/complete result set, provided it stays inside an explicit latency budget (currently:
normal search should stay well under 5 seconds). One consequence of holding this line: a
promising cross-encoder reranking configuration that pushed broad-mode Top-1 from roughly 60%
to 87% on a smaller matrix still didn't get promoted as the default, because a later, larger
frozen blind evaluation across a diverse corpus selected plain hybrid RRF over it in a
preregistered head-to-head comparison. The bar for changing the default is evidence from a
blind, preregistered evaluation — not "it looked better on one matrix."

### How this compares to the rest of the field

A survey of ten other open-source/commercial agent-memory projects (Hindsight, Mnemopi,
akitaonrails/ai-memory, mem0.ai, basic-memory, memanto, Tencent's Agent-Memory, MemoryOS,
MemoryBear, MemPalace) turned up a few honest takeaways worth naming:

- **Hybrid FTS/BM25 + vector RRF fusion is the field's universal baseline**, not a distinctive
  SALTMDB idea — most of the surveyed projects converge on the same starting point.
- **The embedded-local-SQLite lane SALTMDB occupies is a real, populated design point**, not an
  outlier — several other projects (Mnemopi, ai-memory, basic-memory's default, MemPalace) ship
  the same shape; the heavier hosted-multi-tenant end (Postgres/Neo4j/Elasticsearch/Redis stacks)
  is a genuinely different product category, not a "more serious" version of the same one.
- **Self-reported benchmark numbers dominate this space and are mostly not independently
  reproducible from a README** — several projects cite LongMemEval/LoCoMo numbers that swing by
  dozens of points between algorithm versions with no visible reproduction path. The two best
  practices seen anywhere in the survey — explicitly caveat that cross-project scores aren't
  comparable, and commit the evaluation dataset plus regeneration commands alongside any
  published number — are aspirational targets for this project's own future public benchmark
  publishing, not something it can already claim credit for.
- One structurally close relative, `ai-memory`, uses a **"compile, don't just retrieve"** pattern
  — periodically compiling high-value raw memories into a curated, deduplicated wiki rather than
  only ever searching the atomic pool at query time. That maps directly onto this project's own
  weakest audit finding (dense near-duplicate `event`-type clusters) and is tracked as a future
  idea, not yet built.

### Is a database even better than a flat-markdown "second brain"?

Worth answering directly, since it's the first thing anyone familiar with Andrej Karpathy's
"LLM wiki" idea or a Zettelkasten-style flat-file second brain reasonably asks: **why a database
at all, instead of just a folder of markdown notes an agent greps and occasionally re-compiles?**
This project actually ran that comparison rather than assuming the answer, and the honest result
doesn't flatter SALTMDB uncritically.

A controlled 10-note-corpus benchmark ran identical store/retrieve workloads through SALTMDB and
through a realistic flat-markdown-plus-grep setup, token-counted as a proxy for real cost:

| Approach | Store | Retrieve | Total |
|---|---|---|---|
| SALTMDB (incl. fixed schema/envelope overhead) | 3,510 | 3,586 | **9,970** |
| Flat markdown + grep (realistic) | 2,768 | 3,176 | **5,944** |
| Just dumping the whole notes folder into context | — | — | **2,055** |

At this small scale, dumping the entire notes folder into context beat *every* search strategy,
SALTMDB included — there's no retrieval problem left to solve once the whole corpus fits cheaply
in context. The crossover point where actually searching starts winning over a full dump was
measured at roughly **15–20 notes**. "A database is always cheaper than a markdown wiki" is
flatly not true below that scale, and the honest conclusion drawn from this benchmark was written
down as exactly that — not softened for the pitch.

Two things keep this from being the end of the story, though:

1. **Cost isn't the only axis, and it's not the one that failed silently.** The same benchmark
   ran a literal-phrase `grep` against the flat-markdown corpus and it missed a note that
   SALTMDB's hybrid FTS5+vector search found on the first try. A cost gap is visible and easy to
   reason about; a recall gap is not — a careless pass over grep's output would have concluded "no
   such note exists" and moved on, which is a correctness failure, not a mere inefficiency. This
   is the same theme as the strict-mode-abstention numbers in §5: retrieval failing *loudly*
   (nothing found, go look elsewhere) is a wildly different outcome from retrieval failing
   *quietly* (the wrong or a missing answer, presented with confidence).
2. **A flat-file wiki degrades in ways a corpus this size doesn't yet show.** The token-cost
   comparison above holds the workload artificially simple — one writer, no concurrent access, no
   need to know which of two similar notes about the same topic is the current one. Once the
   corpus is written and read by more than one agent at the same time, that stops being simple:
   SALTMDB's single-owner backend daemon (§1) exists specifically so concurrent writers don't
   corrupt shared state, its immutable revision/supersession lineage exists specifically so "which
   version of this note is current" is answerable without hand convention, and its bi-temporal
   relation graph exists specifically so "this fact contradicts/supersedes/depends on that one" is
   a queryable edge instead of something only a human skimming a wiki page would notice. A flat
   markdown folder does not have server-enforced answers to any of those questions — it has
   whatever discipline the person or agent maintaining it brings, which is precisely the kind of
   reliance on agent diligence the design principle in §4 exists to avoid.

The honest, unhedged verdict: **a flat-file "second brain" is not simply worse — it is genuinely
cheaper and simpler at small scale, and "compile the corpus into a curated wiki periodically"
(Karpathy's framing, and `ai-memory`'s actual implementation of it) is a real, credible answer to
the small-corpus case, not a strawman.** What a structured backend like SALTMDB buys, and what the
benchmark's own overhead numbers don't show, is what happens once scale, concurrency, and
multi-agent write access are real: recall that doesn't fail silently, structured filtering that
doesn't depend on a hand-maintained frontmatter convention every note has to follow correctly, and
lineage/relation tracking that survives more than one writer touching the same fact over time.
Below roughly a few dozen notes, single writer, low concurrency — a flat markdown wiki is a
perfectly reasonable choice, arguably the *better* one. Past that point, or the moment more than
one agent is writing to the same memory, the tradeoff flips. Neither answer is universally right;
the mistake would be picking one without ever having measured where the line actually falls.

---

## 6. The agentic workflow: strong model plans and reviews, cheap model implements

The other half of "agentic workflow and memory": memory is what makes a *multi-session,
multi-model* pipeline actually hold together, because the workflow's own rules, its cost
tradeoffs, and its failure lessons are themselves stored and retrieved the same way any other
project fact is — instead of being re-explained, re-argued, or silently drifting every time a
new session starts.

### The pipeline

1. A strong model (Claude, in this deployment) does architecture and design work — pressure-
   testing an idea via a structured "grilling" interview when the design has open branches —
   then writes a **locked, detailed implementation spec**.
2. That model creates a **git worktree** for the implementation and sets up its dev environment
   inside it (e.g. `uv sync` for a Python project) — so the implementing agent never has to
   improvise a missing environment.
3. It hands the user a **copy-pasteable prompt** referencing that spec and worktree, to give to
   the OMP orchestrator.
4. Inside OMP: a mid-tier model reads the spec and produces a plan/todo list; on the first actual
   file write, the primary agent switches down to a small, cheap "Luna" model at its highest
   reasoning effort, which does the actual implementation — while a **second, independent
   instance of the same small model runs continuously alongside it as an advisor**, raising nits,
   concerns, and blockers as it goes. A blocker is a hard stop; the advisor also holds veto power
   at the handoff boundary and can refuse to let the implementer declare the work done.
5. Once the implementer finishes and the advisor has no outstanding issues, the strong model
   comes back for a **fresh-session, spec-compliance final review** of the diff and test suite
   before anything is committed or merged.

The strong model's role is deliberately **architecture, specs, and review — not implementation**.
For a real feature, bugfix, or logic change, the default is to write the spec and route
implementation to OMP; the strong model writes code itself only for genuinely quota-trivial
changes (a typo, a config value, a one-line fix), meta-work on the agent-tooling layer itself
that OMP never touches, or when a user explicitly overrides the recommendation once. The
reasoning, in the author's own words: cheap-model agents are cheap, the strong model's quota is
not — so push as much real implementation work onto the cheap tier as the spec quality allows.

### Why the split works: cost and capability data, not just intuition

This isn't taken on faith — it's backed by data collected from actually running the pipeline:

**Advisor cost is treated as non-negotiable**, restated directly: *"I would not disable the
advisor even if it costs more or burns more quota."* That preference is empirically reinforced,
not just asserted — on two separate implementation slices, the advisor caught real blocking
issues mid-run that were fixed by the implementer before the diff was ever handed off for final
review, meaning a clean-looking final review is a property of the implementer+advisor pair's
internal QA cycle, not evidence the advisor was unnecessary overhead.

A third-party coding-agent benchmark (user-supplied source, not independently re-derived — take
the absolute numbers as directional, not gospel) put a cheap model at its highest effort tier
ahead of a mid-tier model on *both* capability and cost:

| Model tier | Capability index | Cost/task | Time/task |
|---|---:|---:|---:|
| Mid-tier, medium effort | 48 | $0.67 | 4.0 min |
| Cheap tier, high effort | 52 | $0.18 | 5.7 min |
| **Cheap tier, max effort** | **57** | **$0.29** | 8.0 min |
| Mid-tier, high effort | 55 | $1.14 | 6.0 min |
| Mid-tier, max effort | 60 | $1.93 | 8.2 min |

The cheap tier at max effort beat the mid-tier model at both medium *and* high effort on raw
capability, at roughly a quarter to a third of the cost — while only the priciest top-end
configuration still won on absolute capability alone, at nearly 7× the cost. The one thing this
table can't settle is whether a model is good at *implementing* versus good at *critiquing
someone else's output* — those are different skills, and the honest caveat kept alongside this
data is that a same-family implementer/advisor pair (as used in step 4 above) needs its own
direct evidence, not just an inference from this table.

A real A/B/C harness-configuration experiment (three independent implementations of the same
spec, deliberately blinded from the reviewing model until after final review, so the review
verdict couldn't be biased by knowing which configuration produced which diff) later showed just
how much the cost side of this can swing for **functionally identical, fully spec-compliant**
output:

| Run | Wall time | Tokens | Cost |
|---|---|---|---|
| A (single implementer) | ~35 min | 35M | $1.07 |
| B (single implementer, pricier model) | ~49 min | 26M | $4.37 |
| C (orchestrator delegating to a sub-agent) | ~28 min | 14M | $2.40 |

All three passed final review clean. B cost roughly 4× what A did on *fewer* tokens — a
per-token pricing difference, not just "used more compute." C used the fewest tokens by a wide
margin and finished fastest, but that's confounded with its different delegation strategy, not
attributable to a single hidden setting. The lesson kept from this isn't "always pick the
cheapest config" — it's that this kind of real, measured comparison is exactly the kind of thing
that's cheap to capture in memory once and expensive to re-discover from scratch every time
someone wants to reconsider the pipeline's cost/quality tradeoff.

### The handoff contract

A finished spec is never just the document. Every handoff to the implementation tier ships three
things together: the locked spec, a git worktree with its dev environment already set up, and a
copy-pasteable prompt for the user to hand to the orchestrator. The orchestrator itself carries no
standing rule to work from a worktree or a spec — that judgment lives upstream, in the planning
model's own process, specifically *because* it never sends the orchestrator anywhere else. Kept
in the author's own framing: the orchestrator "worked really well for the previous ~10-ish
sessions because [the planning model] was the brain — that's why it doesn't belong in [the
orchestrator's] own core instructions." The judgment stays with whoever is doing the planning,
not baked into the executor.

---

## 7. Strengths and shortcomings, honestly

**Strengths, backed by the numbers above:**
- High recall in practice — 99.4% Recall@10 on a live known-item audit means the right memory is
  almost always *somewhere* in a normal result set, even when it's not ranked first.
- A real, enforced latency ceiling (sub-5s budget) that accuracy work is measured against, not an
  unbounded "as accurate as possible, whatever it costs" chase.
- Hybrid retrieval, immutable lineage, bi-temporal relations, and a domain-neutral core schema
  that isn't secretly a coding-agent-only tool wearing a general-purpose label.
- Server-side enforcement (the hooks in §4) that doesn't rely on any single agent remembering to
  behave — verified working across three different agent vendors sharing one instance.
- Genuinely cross-agent: a memory written by one CLI agent has been read back and cited by a
  completely different vendor's agent against the same local database — not just theoretically
  compatible, observed working.
- Deliberate intellectual honesty about its own evidence: a promising-looking reranking change
  that didn't survive a larger blind replay was *not* promoted to default, even though it "looked
  better" on an earlier, smaller matrix.

**Open shortcomings, not glossed over:**
- Strict (relevance-abstention) mode over-abstains badly enough that it's not viable as a general
  default — it discarded more than half the targets broad mode found, and correctly abstained on
  well under half of realistic false-premise negative queries. It's currently scoped narrowly (the
  auto-injection hook path) for exactly this reason.
- Near-duplicate detection still has a real, unresolved precision gap (roughly 35–50% false
  positives) against one specific pattern — generic, high-connectivity "hub" memories — and the
  fix needs new, genuinely domain-neutral signals, not a quick heuristic.
- `event`-type memories (dense handover/progress-history notes) are measurably the weakest
  retrieval slice in the corpus — a corpus-hygiene problem as much as a ranking one.
- The 500-memory live audit sampled a recency-biased 78% of the corpus, not a uniform random
  slice — a real limitation on how far its exact percentages generalize, flagged here rather than
  quietly extrapolated.
- Cross-encoder reranking is a real, working capability but is not unconditionally beneficial —
  it has to be evaluated and gated per change, not switched on by default on the strength of one
  good-looking benchmark run.
- Public, reproducible benchmark publishing (in the "commit the dataset and the regeneration
  command" sense the competitive survey called out as best-in-class elsewhere) is an acknowledged
  gap relative to the field's own best practice, not yet something this project can claim.

---

## 8. Standing rules worth stealing

The agent-workflow half of this isn't only about SALTMDB's own code — it's also a small set of
standing operating rules for the agents *using* SALTMDB, written once into shared instructions
and applied on every task since. A few have turned out to be worth more than their weight in
prompt tokens:

- **The circuit breaker.** On a failing command, tool call, or test — especially two consecutive
  times — stop. Don't blindly re-run the same command or start editing random files hoping
  something sticks. Log the failure as an event, search memory for precedent (has this exact
  error happened before, and what fixed it), then form one deliberate new hypothesis before
  trying anything else. This single rule is most of the difference between a session that burns
  its budget flailing at a stack trace and one that fixes it in two tries — and it's cheap to
  adopt in any agent workflow, memory system or not.
- **Reuse over reinvention.** Before writing any new helper, state explicitly whether something
  reusable already exists — grep/search first, prefer the standard library over a new
  dependency, extend an existing function's signature over adding a near-duplicate second one.
- **Surgical, minimal-diff edits.** Targeted search-and-replace, not full-file rewrites. Never
  touch unmodified functions or reorder imports as a side effect of an unrelated fix. State the
  target files/line ranges before editing; if the real fix spills outside that boundary, stop and
  re-scope rather than expanding silently. Check the diff before calling anything done, specifically
  to catch stray collateral changes.
- **Deliberate-shortcut ledger.** When a real corner gets cut on purpose — a global lock instead
  of per-key locking, a naive O(n²) scan, a stubbed edge case — it gets a `# shortcut:` comment
  naming the ceiling and the upgrade trigger (e.g. *"global lock, move to per-account locks if
  throughput becomes a problem"*), instead of just quietly living in the code as unexplained debt.
  A separate periodic audit sweeps a repo for every such marker and flags any that don't name a
  trigger — cheap insurance against "temporary" shortcuts nobody remembers agreeing to.
- **Self-verification before declaring done.** Run the repo's actual documented test command as a
  mandatory last step before claiming any change is complete — read the real failures, fix, and
  re-run, never declare success on assumed correctness. Paired with a preference for writing (or
  extending) a failing test first, then implementing to green, for new features and bugfixes.
- **No silently swallowed failures.** No bare `except: pass` or an equivalent empty catch in any
  language — an error that's deliberately ignored still gets a comment saying why, not just a
  disappearing act.
- **Never touch anything resembling production.** No agent SSHes into or otherwise connects to a
  production server — full stop, no exceptions carved out for an "it seems safe" judgment call in
  the moment. The point isn't distrust of any one action, it's that the failure mode (an agent
  running something destructive against live infrastructure) is bad enough that the rule stays
  categorical rather than case-by-case.
- **Never print secret material into tool output.** Tool output isn't local-only — it goes back
  into the model's own context on the next turn, so dumping the contents of an API key or a
  session cookie into a shell result leaks it exactly as if it were committed to a public repo.
  The standing pattern instead: check *existence* (does the file exist), check *field names*
  (what keys does the config have), check *shape* (size, one non-secret field's value) — never
  the secret value itself.
- **Architecture/specs/review, not implementation, for the planning model** — already covered in
  §6 above, but worth naming here as the same style of rule: a strong model's scarce budget goes
  toward judgment (design, spec-writing, final review), not toward generating code a cheaper
  model can produce just as correctly from a good enough spec.

None of these are SALTMDB-specific — they're general agentic-workflow hygiene. What SALTMDB adds
is the part that makes them durable across sessions instead of living only in one conversation's
context window: written once, retrieved automatically by the hooks in §4, and never silently
re-argued from scratch the next time an agent picks the project back up.

---

## 9. What's next

Current exploratory work (design-stage, nothing shipped yet) is aimed at graph-aware, multi-hop
context retrieval — going beyond single-memory known-item lookup toward bounded graph-neighborhood
expansion, community detection over the relation graph (evaluating Leiden clustering), and
benchmark methodology for things known-item retrieval doesn't measure at all: context
precision/recall across a *set* of returned memories, contradiction detection, and correct
supersession handling under multi-hop queries. None of this is committed to a timeline here — it's
listed so anyone reading this as a snapshot of the project knows where the live edge of the work
currently is.

---

*This document was assembled by Claude Code directly from the project's own SALTMDB memory graph
— the same system it describes — as a demonstration of what the stored decisions, benchmark
results, and workflow lessons actually look like once pulled together in one place.*
