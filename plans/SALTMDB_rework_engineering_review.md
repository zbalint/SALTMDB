# SALTMDB `rework` Branch Engineering Review

**Purpose:** Planning input for Codex and other implementation agents\
**Review focus:** Retrieval accuracy, ranking architecture, memory
lifecycle, daemon/database design, test strategy, and next-step
engineering priorities\
**Status:** **\[Unverified\]** Static source review only. The repository
was inspected publicly, but the codebase and test suite were not
executed as part of this review. Runtime behavior, benchmark results,
and performance characteristics therefore require local verification.

## 1. Executive verdict

\[Inference\] SALTMDB's `rework` branch has moved beyond a conventional
"vector database for agent memory" architecture. It is better
characterized as a centralized long-term memory subsystem for multiple
AI agents, with explicit support for memory evolution, temporal history,
retrieval fusion, relevance abstention, consolidation, and cross-agent
persistence.

\[Inference\] The strongest architectural decision is centralizing
database ownership in a daemon while exposing memory operations through
thin MCP/RPC clients. This reduces distributed SQLite coordination
complexity and gives SALTMDB one place to own embedding work,
maintenance, and memory lifecycle behavior.

\[Inference\] The primary technical risk is now **retrieval quality**,
not persistence. The system already contains several generations of
retrieval safeguards, which suggests that further improvement should
increasingly be driven by a reproducible evaluation corpus rather than
ad-hoc tuning of thresholds and ranking constants.

### Overall verdict

**Architecture:** Strong direction\
**Retrieval design:** Sophisticated, but still the main source of
product risk\
**Testing:** Broad and unusually serious for an alpha project\
**Immediate correctness concern:** Relation-count ranking
semantics/sign\
**Highest-value next investment:** Real-world retrieval evaluation
derived from organic multi-agent usage\
**Recommended planning posture:** Preserve architecture; instrument,
benchmark, and calibrate retrieval before introducing large new
retrieval mechanisms.

------------------------------------------------------------------------

## 2. Product model

SALTMDB's practical role can be expressed as:

> **Agent reasoning + SALTMDB long-term memory + MCP/RPC interface**

The important product property is not simply persistence. It is
**cross-session and cross-agent continuity**.

A memory discovered by one agent can survive: - the end of its
conversation, - context-window loss, - switching agent/model
providers, - switching operating systems, - later architectural work, -
and potentially retrieval by a different agent.

A representative real-world success case is the previously described
Windows bug: an agent encountered a Windows-specific problem, the
knowledge persisted, and a later agent/session recalled the issue during
a backend rework and adapted the implementation accordingly.

\[Inference\] That is closer to **organizational or institutional memory
for agents** than ordinary conversational memory.

------------------------------------------------------------------------

## 3. Architecture findings

### 3.1 Centralized daemon ownership

The rework architecture separates: - daemon/backend ownership, -
database access, - domain services, - MCP transport, - models, -
utilities, - viewer functionality.

\[Inference\] Centralizing SQLite ownership in a single backend daemon
is appropriate for this workload. It reduces the number of independently
managed SQLite connections and moves write coordination, embeddings,
maintenance, and lifecycle operations into one process boundary.

### Planning implication

Do **not** casually decentralize database ownership again. Treat the
daemon as a deliberate concurrency and consistency boundary.

Recommended invariants: 1. One authoritative writer/owner per database
path. 2. MCP clients remain thin. 3. Background maintenance is
daemon-owned. 4. Retrieval semantics live in domain services rather than
transport code. 5. Persistence and retrieval behavior remain
independently testable from MCP.

------------------------------------------------------------------------

## 4. Retrieval pipeline observed

The inspected retrieval path includes multiple stages and safeguards:

1.  FTS5 lexical retrieval.
2.  Dense/vector retrieval.
3.  Reciprocal Rank Fusion (RRF).
4.  Supersession-chain handling.
5.  Optional chunk/topic reranking.
6.  Optional cross-encoder reranking.
7.  Relevance/abstention gating.
8.  Memory-type bias.
9.  Supersession/correction demotion.
10. Overfetch for strict-mode pagination.
11. Query-centered snippets.

\[Inference\] This is already a multi-stage information-retrieval
system. Planning should therefore avoid treating retrieval as a single
"choose a better embedding model" problem.

------------------------------------------------------------------------

## 5. Verified concern: relation-count ranking semantics

### Finding

The inspected FTS ranking expression adds a positive relation-count term
to SQLite FTS5's BM25-derived score while sorting ascending.

SQLite FTS5's BM25 implementation is designed so that **lower values
rank better**. Adding a positive relation-count value therefore makes a
highly related memory's score larger and moves it downward, all else
equal.

At the same time, project documentation has described relation count as
a small "boost", while the implementation/config naming includes
`RELATION_COUNT_PENALTY`.

### Verdict

There is an intent mismatch that must be resolved.

Either:

**A. Relations should increase importance** - then the current
arithmetic/sign is wrong;

or:

**B. Highly connected memories should be penalized** - then the
implementation may be intentional, but documentation and terminology
need to say so clearly.

### Recommended action

Create a targeted ranking test before changing the code:

-   Two otherwise equivalent FTS matches.
-   Candidate A has zero relations.
-   Candidate B has N relations.
-   Assert the intended ordering explicitly.

Then rename the constant and update documentation to encode the chosen
semantic meaning.

### Priority

**P0 / correctness and specification alignment**

------------------------------------------------------------------------

## 6. RRF fusion risk

### Current concept

FTS and vector candidates are fused using Reciprocal Rank Fusion with a
conventional `k` value and contributions from both channels.

### Risk

\[Inference\] Equal-channel RRF can favor a moderately relevant
candidate that appears in both channels over a highly important
candidate appearing strongly in only one channel.

This matters for agent memory because historically important memories
can have weak lexical overlap with the current task.

Example:

Historical memory: \> A Windows-specific process behavior caused failure
X; use workaround Y.

Future task: \> Rework daemon/backend ownership.

The historical memory may be decision-critical without sharing many
exact tokens with the new task.

### Recommended experiments

Benchmark at least:

1.  Current equal-weight RRF.
2.  Weighted RRF.
3.  RRF with channel confidence.
4.  RRF with memory-type priors.
5.  RRF with calibrated lexical/vector score evidence.
6.  A candidate union followed by a stronger second-stage ranker.

Do not replace RRF without evidence. Use the same gold corpus for all
variants.

### Priority

**P1 / retrieval quality**

------------------------------------------------------------------------

## 7. Chunk-level retrieval appears strategically important

### Finding

The project contains an end-to-end "buried needle" style test where: - a
lexically attractive wrong candidate exists, - the correct information
is buried inside a longer memory, - whole-document representation can
dilute the useful content, - chunk/topic reranking is intended to
recover the correct memory.

### Interpretation

\[Inference\] This failure mode is highly representative of long-term
agent memory.

As memories grow, whole-document embeddings increasingly answer:

> "What is this memory generally about?"

instead of:

> "Does this memory contain one critical fact relevant to the current
> task?"

### Concern

Chunk/topic reranking is optional rather than fundamental to the default
retrieval path.

### Recommended action

Evaluate: - default retrieval without chunk reranking, - default
retrieval with chunk reranking, - chunk-first semantic candidate
generation, - entity retrieval followed by best-chunk scoring.

Measure: - Recall@1, - Recall@5, - MRR, - latency, - embedding
workload, - false-positive rate, - abstention behavior.

### Priority

**P1 / likely high leverage**

------------------------------------------------------------------------

## 8. Cross-encoder limitations

### Observed design

The cross-encoder reranker operates on a limited number of top
candidates and receives truncated candidate text.

### Risks

\[Inference\] This creates two ceilings:

**Candidate ceiling:**\
A relevant memory outside the preselected candidate window cannot be
rescued by the cross-encoder.

**Content ceiling:**\
If the useful information occurs beyond the text truncation boundary,
the cross-encoder never sees it.

This is particularly relevant because the project's own chunk-level test
demonstrates buried-information failure.

### Recommended design experiment

Instead of:

> query -\> top entity -\> first N characters -\> cross-encoder

test:

> query -\> candidate entities -\> candidate chunks -\> best chunk(s)
> -\> cross-encoder -\> entity score

Potential scoring model:

`entity_score = f(RRF, best_chunk_score, cross_encoder_score, memory_type, temporal_state, supersession_state)`

Do not hard-code this formula without evaluation.

### Priority

**P1**

------------------------------------------------------------------------

## 9. FTS normalization and software-engineering memory

### Finding

The FTS sanitization path removes or separates punctuation commonly
found in software engineering artifacts.

Examples include structures resembling: - `check_same_thread=False` -
`C:\Users\...` - `foo/bar` - `--no-semantic` - `#tag` - CLI switches, -
configuration keys, - qualified identifiers.

### Risk

\[Inference\] Natural-language tokenization and
source-code/configuration tokenization have different requirements.

Dense retrieval can compensate for some loss, but exact identifiers are
often extremely valuable in engineering memory.

### Recommended action

Create an engineering-token retrieval benchmark containing: - paths, -
function names, - class names, - environment variables, - CLI flags, -
exception names, - error codes, - config keys, - package/module paths, -
Windows and POSIX paths.

Then evaluate one or more dedicated lexical mechanisms: -
identifier-preserving tokenization, - auxiliary exact-token index, -
n-gram index, - separate code/config FTS column, - query-time identifier
extraction.

### Priority

**P1 for SALTMDB's current dogfooding workload**

------------------------------------------------------------------------

## 10. Relevance abstention is a strong design choice

### Finding

Strict retrieval contains explicit mechanisms to reject weakly supported
semantic results rather than always returning the nearest vector
candidate.

The code distinguishes stronger lexical evidence from weaker fallback
behavior and includes chunk/topic evidence in strict relevance
decisions.

### Verdict

\[Inference\] This is the correct conceptual direction. A memory system
should be able to say:

> "I do not have sufficiently relevant memory."

Nearest-neighbor search alone cannot provide that guarantee.

### Important caveat

The optimization target should not automatically be "minimum false
positives at any cost."

For agent memory, the cost of missing a critical historical constraint
can exceed the cost of returning one or two mildly irrelevant memories.

### Recommended action

Calibrate abstention by memory class and consequence.

Potential categories: - architectural decision, - known bug, -
procedure, - security constraint, - user preference, - observation, -
speculative note, - temporary context.

\[Inference\] Known bugs, decisions, and procedures may deserve a
higher-recall policy than low-value episodic observations.

### Priority

**P1 / calibration**

------------------------------------------------------------------------

## 11. Broad versus strict retrieval modes

### Finding

The code contains a stricter relevance path, while broad retrieval
remains an important/default-facing behavior.

### Recommendation

\[Inference\] Consider explicitly separating two product semantics:

**Recall mode** - exploratory, - higher recall, - acceptable noise, -
suitable when an agent is researching history.

**Action mode** - high-confidence, - abstention-capable, - suitable when
returned memories will directly influence autonomous behavior.

This distinction is clearer than treating strictness as merely a search
tuning parameter.

### Priority

**P2 / API semantics**

------------------------------------------------------------------------

## 12. Semantic relevance is not decision relevance

This is the most important conceptual issue in the retrieval roadmap.

Traditional retrieval optimizes:

> "Which stored text is most similar/relevant to this query?"

SALTMDB ultimately needs:

> "Which past experience is most likely to improve the agent's next
> decision?"

Those are not equivalent.

A memory about daemon architecture may be topically close but useless.

A narrowly worded Windows process bug may be less semantically similar
but prevent a regression.

### Long-term target

\[Inference\] The ideal ranking objective is closer to:

`P(memory materially improves the next action | current task and context)`

This should be treated as a research direction, not as a claim that the
probability can currently be estimated accurately.

------------------------------------------------------------------------

## 13. Query construction may matter more than another embedding model

\[Inference\] The retrieval query should eventually represent more than
the user's latest sentence.

Useful retrieval context could include: - current task, - current
subsystem, - files/modules being modified, - operating
system/platform, - action the agent is considering, - entities
referenced, - active architectural constraints, - recent tool
failures, - error messages, - current branch/workstream, - relevant
recent memories.

A future agent-aware retrieval request might conceptually contain:

``` text
Task:
Rework daemon ownership.

Affected components:
daemon, MCP transport, SQLite lifecycle.

Platform:
Windows + Linux compatibility.

Planned action:
Move process ownership and connection handling.

Retrieve:
Past bugs, architectural decisions, platform constraints,
failed approaches, and superseding decisions relevant to this change.
```

This gives the memory system a better representation of intent than a
single natural-language search string.

### Priority

**P2 / potentially major improvement**

------------------------------------------------------------------------

## 14. Memory lifecycle and temporal correctness

The project includes concepts such as: - supersession, - correction, -
disposition, - temporal history, - consolidation.

This is essential because long-term memory cannot assume that all stored
facts remain simultaneously valid.

### Retrieval requirement

A retrieval system should distinguish: - current knowledge, - historical
knowledge, - superseded knowledge, - contradictory knowledge, -
corrections, - provenance.

### Planning recommendation

Add explicit retrieval tests for:

1.  Old fact superseded by new fact.
2.  Multiple-hop supersession chains.
3.  Historical query that should return the old fact.
4.  Current-state query that should return the new fact.
5.  Correction that should outrank the erroneous original.
6.  Consolidated memory preserving important details from source
    memories.
7.  Cross-agent conflicting memories.

### Priority

**P1 / correctness**

------------------------------------------------------------------------

## 15. Evaluation strategy: the highest-value next project

### Recommendation

Build a persistent retrieval benchmark from **real SALTMDB development
history**.

Do not begin primarily with synthetic examples.

The organic multi-agent workload already contains: - real architecture
decisions, - bugs, - platform constraints, - implementation failures, -
fixes, - cross-agent knowledge transfer, - long time gaps, - terminology
drift, - superseded decisions.

That is unusually valuable evaluation data.

### Evaluation record

Each benchmark case should contain:

``` yaml
case_id:
historical_cutoff:
query_or_task:
context:
expected_memory_ids:
acceptable_memory_ids:
harmful_or_misleading_memory_ids:
expected_abstention:
memory_origin_agent:
retrieving_agent:
cross_session:
cross_model:
reason_expected_memory_matters:
outcome_if_retrieved:
outcome_if_missed:
```

### Core metrics

Track at least:

-   Recall@1
-   Recall@3
-   Recall@5
-   Recall@10
-   MRR
-   Precision@K
-   abstention precision
-   abstention recall
-   false-positive rate
-   false-negative rate
-   cross-agent recall
-   cross-session recall
-   supersession correctness
-   retrieval latency

### SALTMDB-specific outcome metrics

Also track:

-   Did retrieval change the agent's plan?
-   Did it prevent recurrence of a known bug?
-   Did it preserve an architectural constraint?
-   Did a different agent reuse another agent's experience?
-   Was the retrieved memory still valid?
-   Did retrieval introduce stale or superseded guidance?
-   How old was the successfully reused memory?

These are potentially more meaningful than cosine-distance metrics.

------------------------------------------------------------------------

## 16. Create three classes of benchmark cases

### A. Successful historical recalls

Cases where SALTMDB demonstrably helped.

The Windows bug example belongs here.

Purpose: - establish known-positive behavior, - prevent regressions, -
identify what signals caused successful retrieval.

### B. Missed-memory cases

Cases where: - relevant memory existed, - the agent should have used
it, - retrieval failed or ranked it too low.

These are probably the most valuable cases for improving recall.

### C. Distractor/false-positive cases

Cases where: - retrieval returned plausible but irrelevant memory, -
stale memory distracted the agent, - semantically similar content
outranked decision-relevant content.

These drive precision and abstention improvements.

------------------------------------------------------------------------

## 17. Add retrieval observability

\[Inference\] Retrieval tuning without observability will become
increasingly difficult as the pipeline grows.

For development/debug mode, log a structured trace for each search:

``` text
query
normalized FTS query
FTS candidates + raw ranks
vector candidates + distances
RRF contributions
chunk scores
cross-encoder scores
memory-type adjustments
relation adjustment
supersession adjustment
relevance-gate decision
final rank
rejection reason
latency per stage
```

Do not expose sensitive memory content unnecessarily in production logs.

### Benefit

A failed retrieval can then be classified as:

1.  candidate-generation failure,
2.  fusion failure,
3.  reranker failure,
4.  gate failure,
5.  supersession failure,
6.  query-understanding failure.

Without this decomposition, every retrieval problem looks like
"embeddings are inaccurate."

### Priority

**P0/P1 for serious retrieval development**

------------------------------------------------------------------------

## 18. Test-suite assessment

The repository contains broad coverage around areas including: - daemon
client/server behavior, - database write coordination, - hybrid
search, - semantic reranking, - embedding reconciliation, - embedding
stalls, - temporal relations, - deduplication, - consolidation, -
redaction, - pagination, - ranking, - snippets, - tagging, - vector
schema, - MCP tools.

\[Inference\] This is a strong base.

However, retrieval unit tests and seam-controlled tests cannot
substitute for corpus-level evaluation.

### Recommendation

Keep unit tests for invariants.

Add a separate evaluation suite for retrieval quality.

Do not make every retrieval benchmark a conventional pass/fail unit
test. Some experiments should produce comparable metrics and deltas.

Example CI policy:

``` text
Correctness tests:
must pass.

Retrieval regression suite:
must not reduce Recall@5 by > threshold.
must not increase harmful stale-memory rate.
must not materially worsen abstention calibration.
latency must remain within budget.
```

Thresholds should be established empirically.

------------------------------------------------------------------------

## 19. Type-checking/tooling observation

The project supports an older runtime floor while type checking is
configured against a newer Python version because of dependency stub
syntax constraints.

### Risk

\[Inference\] Static type checking therefore does not perfectly model
the minimum supported runtime environment.

### Recommendation

Keep an actual CI execution matrix for every claimed supported Python
version.

Treat runtime CI as authoritative for compatibility; treat mypy as type
analysis, not proof of minimum-version compatibility.

### Priority

**P2**

------------------------------------------------------------------------

## 20. Security/static-analysis observation

A Bandit SQL-expression warning is globally disabled with documentation
indicating that dynamic SQL sites were manually audited and inputs are
constrained.

### Risk

\[Inference\] A global suppression can hide a future unsafe
interpolation that was not part of the original audit.

### Recommendation

Prefer one of: - narrow per-site suppressions, - a custom static test, -
centralized safe SQL construction helpers, - a test/linter that
enumerates dynamic SQL construction sites.

### Priority

**P2**, unless untrusted SQL fragments can reach those construction
paths.

------------------------------------------------------------------------

## 21. Proposed planning backlog

### Phase 0 - Resolve known ambiguity

**P0.1 Relation ranking** - Define whether relation count is a boost or
penalty. - Add explicit ranking test. - Correct formula or
documentation. - Rename constant to encode intent.

**P0.2 Retrieval trace** - Add structured debug output for every ranking
stage. - Include rejection reasons and per-stage latency.

### Phase 1 - Establish evaluation foundation

**P1.1 Gold corpus** - Extract 25-50 real historical SALTMDB-development
retrieval cases. - Include successful, missed, and false-positive cases.

**P1.2 Baseline** - Freeze current retrieval implementation. - Record
all metrics. - Store configuration/model versions with results.

**P1.3 Windows case** - Make the known Windows cross-session recall a
canonical regression case if the underlying data can be safely
represented.

### Phase 2 - Improve candidate recall

**P1.4 Chunk-aware default experiment** - Compare current default
against chunk-aware retrieval.

**P1.5 Engineering-token lexical retrieval** - Add benchmark first. -
Implement identifier-preserving retrieval only if benchmark shows
measurable benefit.

**P1.6 RRF calibration** - Test weighted/channel-aware alternatives.

### Phase 3 - Improve ranking precision

**P1.7 Chunk-aware cross-encoder** - Cross-encode best candidate chunks
instead of only truncated entity prefixes.

**P1.8 Memory-type calibration** - Measure whether decisions, bugs,
procedures, and constraints need different ranking/relevance behavior.

**P1.9 Temporal/supersession suite** - Expand tests around stale and
superseded information.

### Phase 4 - Agent-aware retrieval

**P2.1 Structured retrieval context** - Allow agents to submit
task/subsystem/platform/action metadata.

**P2.2 Query expansion** - Derive retrieval facets such as: - relevant
past bugs, - relevant decisions, - platform constraints, - failed
approaches, - procedures.

**P2.3 Action-mode API** - Explore explicit high-confidence/actionable
retrieval separate from exploratory recall.

### Phase 5 - Outcome evaluation

**P2.4 A/B agent runs** Run the same engineering tasks with: - memory
disabled, - current SALTMDB, - candidate retrieval variants.

Measure: - task success, - repeated mistakes, - architectural
violations, - tokens/time, - corrective iterations, - use of cross-agent
knowledge.

------------------------------------------------------------------------

## 22. What not to optimize prematurely

\[Inference\] Avoid spending significant effort on the following until
the evaluation corpus exists:

-   swapping embedding models based only on generic benchmarks,
-   repeatedly hand-tuning cosine thresholds,
-   adding increasingly complicated score formulas without ablation
    tests,
-   introducing an LLM into every retrieval operation,
-   increasing context returned to agents as a substitute for ranking
    accuracy,
-   aggressively consolidating memories without measuring information
    loss,
-   optimizing benchmark-only synthetic queries.

The current system is sufficiently complex that additional mechanisms
can easily improve one retrieval class while silently damaging another.

------------------------------------------------------------------------

## 23. Suggested retrieval architecture direction

A plausible future pipeline is:

``` text
Agent task/context
        |
        v
Query/context analyzer
        |
        +------------------+
        |                  |
        v                  v
Lexical/code retrieval   Semantic retrieval
        |                  |
        +--------+---------+
                 |
                 v
         Candidate union
                 |
                 v
     Supersession resolution
                 |
                 v
        Chunk-level scoring
                 |
                 v
        Fusion / reranking
                 |
                 v
      Cross-encoder on best
        candidate chunks
                 |
                 v
   Type + temporal calibration
                 |
                 v
        Relevance / abstain
                 |
                 v
      Minimal useful memories
                 |
                 v
               Agent
```

This is a **research direction**, not a recommendation to implement all
stages immediately.

Every added stage should demonstrate measurable benefit on the same
evaluation corpus.

------------------------------------------------------------------------

## 24. Definition of retrieval success

The most useful product-level definition is:

> **Retrieve the smallest set of valid historical memories that
> materially improves the agent's next decision.**

This implies four separate requirements:

### Recall

The important memory must enter the candidate set.

### Ranking

It must rank high enough to reach the agent.

### Validity

It must not be stale, incorrectly superseded, or contextually invalid.

### Utility

It must actually help the agent act better.

Traditional semantic similarity addresses only part of this problem.

------------------------------------------------------------------------

## 25. Recommended Codex planning questions

Before implementing retrieval changes, Codex should answer:

1.  What exact failure mode is being addressed?
2.  Is it candidate generation, ranking, gating, temporal validity, or
    query representation?
3.  Which real benchmark cases demonstrate the failure?
4.  What metric should improve?
5.  What metric could regress?
6.  What is the latency/cost budget?
7.  Does the change preserve deterministic behavior where possible?
8.  Can the feature be ablated/configured for comparison?
9.  Does the change alter stored data or only retrieval?
10. Is a migration required?
11. Does it affect daemon concurrency?
12. Does it affect MCP API compatibility?
13. Does it behave consistently on Windows/Linux/macOS?
14. How will the behavior be observed in retrieval traces?
15. What is the rollback strategy if real retrieval quality worsens?

------------------------------------------------------------------------

## 26. Proposed acceptance criteria for retrieval changes

A retrieval change should not be accepted solely because several
hand-written examples look better.

Suggested acceptance template:

``` text
Correctness:
- Existing unit/integration tests pass.
- New failure-mode regression tests pass.

Retrieval:
- No material Recall@5 regression on gold corpus.
- Target case class improves measurably.
- Harmful stale/superseded retrieval does not increase.
- Abstention calibration does not materially regress.

Performance:
- p50/p95 retrieval latency measured.
- Embedding/reranking cost measured.
- Candidate counts bounded.

Compatibility:
- Supported Python versions tested.
- Windows/Linux behavior tested where relevant.
- Existing MCP contract preserved or migration documented.

Observability:
- New scoring stage appears in retrieval trace.
- Rejected candidates have diagnosable reasons.
```

Exact numeric thresholds should be established from baseline
measurements rather than invented in advance.

------------------------------------------------------------------------

## 27. Final verdict

\[Inference\] SALTMDB's current architecture is strong enough that a
rewrite of the retrieval subsystem would be premature.

The code already demonstrates mature ideas: - hybrid retrieval, - RRF, -
chunk-aware reranking, - cross-encoder support, - supersession
handling, - explicit abstention, - temporal memory concepts, -
centralized persistence, - broad test coverage.

The central weakness is not lack of retrieval machinery. It is lack of a
sufficiently rich, repeatable **ground truth for what an agent should
have remembered in real work**.

Therefore the recommended strategic order is:

1.  Fix/specify the relation-count ranking behavior.
2.  Add detailed retrieval observability.
3.  Build a real-world gold corpus from SALTMDB's own multi-agent
    development history.
4.  Establish baseline metrics.
5.  Use ablation experiments to test chunk retrieval, RRF weighting,
    engineering-token search, and cross-encoder changes.
6.  Only then introduce larger retrieval architecture changes.
7.  Eventually evaluate **agent outcomes**, not just search relevance.

\[Inference\] SALTMDB's strongest differentiator may ultimately be not
"better semantic search," but **reliable transfer of accumulated
experience between otherwise stateless agents**. Retrieval should be
evaluated against that product objective.

------------------------------------------------------------------------

## 28. Source locations reviewed

Public `rework` branch and relevant source/test locations inspected
during the review:

-   Repository: `https://github.com/zbalint/SALTMDB/tree/rework`
-   Memory service: `src/saltmdb/domain/services/memory_service.py`
-   Reranker service: `src/saltmdb/domain/services/reranker_service.py`
-   Text/FTS utilities: `src/saltmdb/utils/text.py`
-   Relevance-gate tests: `tests/test_relevance_gate.py`
-   Hybrid-search E2E tests: `tests/test_e2e_hybrid_search.py`
-   Topic-rerank E2E tests: `tests/test_e2e_topic_rerank.py`
-   Project configuration/tooling: `pyproject.toml`
-   GitHub Actions workflows under `.github/workflows/`

------------------------------------------------------------------------

## 29. Verification note

This report intentionally distinguishes static observations from
inference.

**Verified in source review:** architectural structure, existence of
retrieval stages/tests/configuration, inspected ranking expressions and
retrieval mechanics.

**Not verified by execution:** test pass/fail status, runtime
concurrency behavior, actual retrieval metrics, model performance,
benchmark claims, latency, production reliability, and whether the
observed relation-count behavior is intentionally a penalty rather than
an implementation error.

Before Codex changes behavior, reproduce each targeted finding locally
and capture a failing or characterization test.
