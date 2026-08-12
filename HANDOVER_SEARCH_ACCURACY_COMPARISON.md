# SALTMDB Handover: Post-Deployment Search-Accuracy Comparison

## User intent

The user will end the current session, pull commit `0c5c31a` into the live SALTMDB installation, restart the live daemon, and begin a new session. In that new session, Codex must repeat the same search-accuracy tests performed before the improvements and compare old versus new behavior. Do not start unrelated implementation work before completing and reporting this comparison.

## Implemented change to evaluate

Commit `0c5c31a` (`feat: add opt-in search accuracy experiments`) adds:

- global fresh chunk-vector candidates with weighted named RRF;
- broad-mode-only collapse of eligible `supersedes` families;
- genuine bundled cross-encoder controls, preflight, execution diagnostics, and fail-closed benchmark validation;
- independent caller-supplied retrieval-text FTS/vector indexes and embedding jobs;
- provenance-bound evaluation fixtures, promotion statistics, and daemon-latency contracts.

All new search channels remain disabled by default. Merely deploying the commit must not change normal broad search. The next comparison therefore needs both an all-flags-off control and explicit opt-in contenders.

## Authoritative pre-change test battery

Use the 2026-08-12 random-memory retrieval audit as the primary old baseline. Its durable memory is `767685d8-8c7b-40d2-9548-e08e9f24e81b`, and the repository evidence is under `reports/retrieval-audit/`.

Three Luna Max workers independently sampled 50 active memories each, producing 150 target selections and 124 distinct targets. Each target received four read-only broad-mode searches with `limit=10` and `include_related=false`:

1. one verbatim body sentence;
2. one vocabulary-shifted semantic paraphrase;
3. three to six discriminative body keywords;
4. one unrelated negative-control query, scored as whether the selected target stayed absent from the top 10.

The historical results were:

| Query condition | Trials | Exact target top-1 / pass | Exact target top-10 |
|---|---:|---:|---:|
| Verbatim body sentence | 150 | 145/150 (96.7%) | 150/150 (100.0%) |
| Semantic paraphrase | 150 | 69/150 (46.0%) | 127/150 (84.7%) |
| Discriminative keywords | 150 | 113/150 (75.3%) | 145/150 (96.7%) |
| Unrelated negative: selected target absent | 150 | 148/150 pass (98.7%) | not applicable |

The exact 150 semantic paraphrases are frozen in `reports/retrieval-audit/semantic-paraphrase-manifest.json`. The exact sentence, keyword, and negative queries are preserved per target in `reports/retrieval-audit/worker-a.md`, `worker-b.md`, and `worker-c.md`. The frozen policy comparison is summarized by memory `06b4983b-6abc-482a-bada-2651f6131d2b` and `reports/retrieval-audit/policy-comparison-summary.md`.

Do not use the previous 800-query evaluation as promotion evidence unless its corpus, configuration, commit, query, and judgment fingerprints validate under the new provenance contract. The current code deliberately rejects provenance-free legacy artifacts.

## Mandatory new-session bootstrap

1. Confirm the live source contains commit `0c5c31a` or a descendant.
2. Confirm the live daemon was restarted after the pull and resolves the intended source/model paths.
3. Run the bundled cross-encoder preflight directly through `reranker_service.score_pairs_preflight()` and require finite, correctly sized scores.
4. Run an ambiguous end-to-end health query with `use_cross_encoder=true` and `force_cross_encoder=true`; require `cross_encoder_score` and diagnostics showing at least one execution.
5. Work only against a snapshot/disposable copy for repeatable evaluation when practical. Never mutate or benchmark destructively against the live database. If the MCP-only replay must query the live service, record the exact database/corpus fingerprint first and keep all operations read-only.
6. Health-check the search pipeline before delegation. The earlier audit observed subagent sessions returning empty results while the main session was healthy; discard any batch whose exact-keyword health query fails.

## Freeze the replay before looking at new results

Before executing the comparison, construct a signed replay manifest from the exact preserved queries and target IDs. It should contain all 600 historical queries if the worker reports can be parsed without ambiguity. At minimum it must contain the already frozen 150 semantic paraphrases. Record:

- commit fingerprint;
- live/snapshot corpus fingerprint;
- query-manifest fingerprint;
- random seed;
- complete contender configuration fingerprint;
- judge rubric/version fingerprint;
- machine fingerprint for latency evidence.

Do not rewrite, improve, or regenerate queries after inspecting new results. If any historical row cannot be reconstructed exactly, mark it missing and keep it out of paired statistics rather than inventing a replacement.

## Contenders to run on the same frozen snapshot

Every query must be interleaved across the following configurations so corpus drift and cache order cannot favor one contender:

1. **Deployed legacy-equivalence control**: broad mode with `rerank_by_topic=false`, `prefer_durable_types=false`, `demote_superseded=false`, `use_cross_encoder=false`, `use_chunk_candidates=false`, `collapse_supersedes_families=false`, and `use_retrieval_text_candidates=false`.
2. **Chunk candidates**: test the predeclared grid for `oversampling_multiplier` in `{4,8,12}`, `candidate_window` in `{20,40,60}`, and `chunk_weight` in `{0.5,1.0,1.5}` while FTS/entity-vector weights remain 1.0. If the full grid is too large for the initial replay, use the predeclared shortlist only; do not choose settings after seeing blind results.
3. **Supersedes collapse**: broad mode with `collapse_supersedes_families=true`, first alone and then with the chosen chunk contender. Report how many result pools actually contained an eligible complete chain; zero eligible chains means the test provides no collapse-quality evidence.
4. **Cross-encoder**: compare ambiguity-gated and `force_cross_encoder=true`, with candidate cap `{10,15,20}` and text cap `{1000,2000}` only through a predeclared shortlist. Record execution count/rate. Zero executions invalidates the contender.
5. **Retrieval text**: do not claim an accuracy result unless the evaluated entities actually have fresh retrieval text and succeeded retrieval embeddings. Immediately after deployment, old live memories normally have no retrieval text, so this channel is expected to have zero coverage. Report zero coverage rather than silently treating it as a failed or baseline-equivalent experiment.

The confidence gap gate must continue to use only FTS/entity-vector corroboration in this version. When all new options are disabled, rankings and tie behavior must match the deployed legacy-equivalence control.

## Metrics and comparison rules

For the exact historical 600-query replay, report paired old historical numbers and new same-snapshot results for:

- exact target top-1 and top-10 for sentence, paraphrase, and keyword queries;
- negative selected-target absence from top 10;
- per-query wins, losses, and ties versus the same-snapshot all-flags-off control;
- candidate shortfall and execution/coverage rates for each optional channel;
- failures and empty responses.

Also repeat the earlier rank-1 relevance review for semantic paraphrases using the same categories:

- `SAME_SPECIFIC_FACT`;
- `SAME_TOPIC_SIBLING`;
- `BROADLY_RELATED`;
- `IRRELEVANT`.

Exact-ID top-1 alone is insufficient because later syntheses or lifecycle-equivalent memories can answer the same fact. Report both exact-ID ranking and judged fact/sibling relevance.

Use the new formal promotion contract when enough frozen judged data exists:

- grade-2 semantic Recall@20 improves by at least 3 percentage points;
- bootstrap 95% lower bound for NDCG@10 improvement is above zero;
- predeclared grade-2 top-1 McNemar comparisons pass Holm-adjusted `p < 0.05`;
- exact, keyword, and negative safety regressions are no worse than 1 percentage point;
- zero benchmark failures;
- persistent-daemon warm p95 is below one second and no more than 15% slower than the same-snapshot runtime baseline.

Historical-versus-new deltas are contextual because the live corpus has changed since the old audit. Promotion decisions must be based primarily on paired contenders run against the same frozen post-deployment snapshot, with the historical figures shown separately.

## Latency protocol

Promotion-grade latency uses one persistent daemon, one fixed corpus and machine, 20 warm-up queries, and five interleaved repetitions of every frozen query/configuration. Direct-service measurements are diagnostic only and cannot satisfy the promotion gate.

For context, implementation-session disposable direct-service smoke timings were approximately 8.4 ms baseline, 28.0 ms chunk, 23.8 ms retrieval text, and 301.8 ms forced cross-encoder. These are not comparable promotion measurements.

## Verification already completed before deployment

- commit: `0c5c31a`;
- Ruff lint: passed;
- `mypy src`: passed for 43 files;
- full unit suite: 779 tests passed;
- bundled cross-encoder: finite/cardinality-valid preflight passed;
- disposable production database: cross-encoder emitted `cross_encoder_score` with execution rate 1.0;
- retrieval-text optional-vector regression was fixed so production/self-opened paths load vec0 while deliberately extension-free SQLite tests retain non-vector storage compatibility.

## Required deliverables from the next session

1. A signed replay manifest and exact configuration manifest.
2. Health/preflight evidence, including cross-encoder execution count.
3. Machine-readable result artifacts for every contender.
4. A concise old-historical versus new comparison table.
5. A paired same-snapshot contender comparison with statistical intervals/tests where admissible.
6. A clear decision: keep every option disabled, promote a specific contender, or gather more evidence.
7. Durable SALTMDB memories linked to implementation decision `945d3df4-700a-48ed-9138-650b65625815`.

Do not change runtime defaults during the comparison session unless the frozen accuracy, safety, and persistent-daemon latency gates actually pass and the user explicitly authorizes the default change.
