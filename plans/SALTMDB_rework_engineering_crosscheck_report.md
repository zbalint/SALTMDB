# SALTMDB `rework` Engineering Review — Current-State Cross-Check

**Audit date:** 2026-08-11  
**Audited input:** `plans/SALTMDB_rework_engineering_review.md` (1,054 lines; untracked planning input)  
**Current revision:** `rework` at `5002820` (`CLI: Replace markdown digest with XML+YAML envelope format`)  
**Scope:** Source, configuration, test inventory, committed benchmark/evaluation artifacts, and local verification commands. This is an audit report, not an implementation proposal.

## Executive conclusion

The review’s architectural diagnosis is substantially correct: SALTMDB is a daemon-owned, hybrid long-term-memory system with lifecycle-aware retrieval. Its central P0 correctness finding remains **open and confirmed**: `_run_fts_search()` adds a positive relation count to SQLite FTS5 BM25 while sorting ascending, so additional incoming live relations worsen the FTS score. The configuration calls this `RELATION_COUNT_PENALTY`; there is no characterization test that establishes this as an intentional penalty.

The review is also materially stale in one important direction. Since it was written, the branch has gained an evaluation framework: real-corpus manifest/query builders, a frozen-corpus matrix runner, blinded judging packets, statistical analysis, a 24-configuration matrix, golden queries, and tests for that tooling. The framework is not the same as a completed, checked-in empirical baseline: no finished matrix/judgment/analysis result artifact was found in the tracked tree, and several proposed experiments still do not exist.

Quality evidence is mixed. The full unit suite passes, but the current CI quality-gate commands are not clean locally:

| Verification | Result | Evidence |
|---|---:|---|
| Unit/integration tests | Pass | `uv run --no-sync python -m unittest discover -s tests`: **720 tests**, **127.206 s**, `OK` |
| Ruff | Fail | `uv run --no-sync ruff check .`: **23 findings** |
| Mypy | Fail | `uv run --no-sync mypy src`: **49 errors in 9 source files** |
| Bandit baseline command | Fail | `uv run --no-sync bandit -c pyproject.toml -r src -b .bandit-baseline.json -q`: **12 low-severity issues** |

The green suite proves many behavioral invariants, including daemon, chunk-rerank, strict relevance, pagination, temporal, and Windows-mocked paths. It does **not** prove retrieval quality, latency, a real-world gold-corpus outcome, cross-platform runtime behavior, or that lint/type/security gates currently pass.

## Method and evidence limits

Each item below is classified as **confirmed**, **partially confirmed**, **not implemented/not evidenced**, or **inference**. “Not evidenced” means the current source, tracked test suite, and tracked artifacts do not prove the claim; it is not a claim that the idea is invalid.

The audit preserved the pre-existing dirty worktree: `uv.lock` was modified before the audit and the input review itself was untracked. No production behavior was changed.

## Cross-check of the review’s findings

| Review area | Current status | Current evidence and correction |
|---|---|---|
| Product model / cross-agent continuity (§2) | **Confirmed** | Shared ownership/scope/context fields exist in the entity schema and tool dispatch. The daemon design makes adapter MCP processes thin RPC clients; it is a credible cross-session, cross-agent store. The Windows “success case” is historical narrative, not reproducibly evidenced by this repository alone. |
| Centralized daemon ownership (§3) | **Confirmed** | `src/saltmdb/daemon/server.py` owns initialization, dispatch, writer coordination, embedding scheduler, maintenance/session lifecycle; `mcp/server.py` opens only a `SessionConnection`; `mcp/tools.py` uses `RpcBackend` in production. `DirectDispatchBackend` is explicitly limited to tests and daemon-side dispatch. |
| Multi-stage retrieval (§4) | **Confirmed** | `memory_service.search_memory()` performs FTS, vector retrieval, RRF, strict supersession resolution, optional topic rerank/cross-encoder, relevance gating, durable-type preference, supersession/correction demotion, strict overfetch, and snippets. Query-centred FTS snippets are implemented in `_run_fts_search()` via FTS5 `snippet()`. |
| Relation-count ranking semantics (§5) | **Confirmed defect / P0 open** | `_run_fts_search()` orders `bm25(...) * e.weight + rel_count * RELATION_COUNT_PENALTY` **ASC**. SQLite FTS5 BM25’s lower score ranks first, therefore positive `0.1 * rel_count` demotes highly related targets. `config.py` names it `RELATION_COUNT_PENALTY`, which makes the arithmetic internally consistent only if demotion is intended. No relation-count ordering test exists (`tests/test_search_scores.py` tests only non-zero score and RRF output). Resolve the intended policy, add a two-candidate characterization test, then rename/document or change the sign. |
| Equal-channel RRF risk (§6) | **Partially confirmed** | `reciprocal_rank_fusion()` exists and uses rank-position fusion; configuration/docs describe `k=60`. The current implementation has no channel weights or confidence-weighted RRF. The new matrix can compare rerank/mode/type/demotion/cross-encoder settings, but not weighted RRF variants. The stated quality risk is an inference until measured on judged data. |
| Chunk-level retrieval (§7) | **Confirmed capability; default experiment incomplete** | Chunk schema/embeddings are real (`entity_chunk_embeddings`, 1200/200 sliding window). `rerank_by_topic=False` by default, so chunk reranking remains opt-in. `tests/test_e2e_topic_rerank.py::test_rerank_by_topic_fixes_length_dilution_vs_baseline` verifies the buried-needle/length-dilution behavior. The evaluation matrix can compare it, but no tracked completed result establishes that it should become default. |
| Cross-encoder ceilings (§8) | **Confirmed limitation** | Cross-encoder is optional/off unless `SALTMDB_RERANKER_MODEL` is configured; it scores at most 10 candidates, truncates candidate text to 800 characters and queries to 300. It ranks entity text, not selected chunks. Current tests verify gating and precedence, but no chunk-aware cross-encoder is implemented. |
| FTS engineering-token risk (§9) | **Confirmed source behavior; efficacy unknown** | `sanitize_fts_query()` replaces `-+<>:/*\\?^$|#@`~!%&(){}[]` with spaces. That degrades exact punctuation-bearing flags, paths, and identifiers as described. No dedicated engineering-token benchmark or auxiliary identifier index was found. Golden queries include some engineering content, but do not constitute the recommended identifier-focused benchmark. |
| Relevance abstention (§10) | **Confirmed** | `mode="strict"` calls `accept_or_abstain()` on each survivor and returns `[]` for no grounded match. Tests cover dual-channel, true FTS, OR-fallback, semantic-only/topic, and indirect supersession evidence. The documented policy intentionally accepts false rejects for weak semantic-only matches. Per-memory-class calibration is not implemented. |
| Broad vs. strict (§11) | **Confirmed implementation; action/recall API absent** | `mode` exposes `broad`, `strict`, and `history`. Semantically, broad is exploratory and strict is abstention-capable, but neither the MCP contract nor documentation calls them “Recall mode” and “Action mode,” and there is no separate action-mode API. |
| Decision relevance (§12) | **Inference / future research** | The code ranks retrieval evidence, not `P(memory improves next action)`. No outcome model or captured agent-decision metric exists. |
| Structured query context (§13) | **Not implemented** | `search_memory` accepts a single `query_keywords` string plus filters/options. It has no typed task/subsystem/platform/action context object and no query-context analyser. |
| Temporal lifecycle (§14) | **Substantially confirmed** | Supersedes/corrects, bitemporal relation fields, chain resolution, strict substitution, history tagging, and correction demotion exist. Tests cover bitemporal validity, multi-hop resolution, strict defaults, correction handling, and pagination after substitution. The requested explicit cross-agent-conflict retrieval test was not found. |
| Real-world evaluation foundation (§15–16) | **Partially completed** | `scripts/benchmarking/` now has real-corpus manifest/query building, frozen-corpus safeguards, a 24-config matrix (`eval_configs.py`), blinded judge pools, judgement merge, statistics/CI/McNemar analysis, and golden queries whose metadata says they were hand-picked from live history. Unit tests cover the framework. No tracked completed 25–50-case, human-judged matrix output/baseline was found; outcome metrics such as changed plan or avoided recurrence are not captured. |
| Retrieval observability (§17) | **Not implemented** | `explain_mode` returns SQLite query-plan information before retrieval; it is not a per-search ranking trace. Internal debug logs report selected gate events, but no structured trace exposes normalized query, channel candidates/raw scores, RRF contributions, stage latency, final rejection reason, and final rank as requested. |
| Test-suite assessment (§18) | **Confirmed, with a critical qualification** | The inventory covers the stated areas and the full local suite passed (720 tests). The conclusion that unit/seam tests do not replace corpus evaluation remains correct. |
| Type tooling (§19) | **Confirmed and worse than stated** | `requires-python = ">=3.10"`; mypy targets 3.12 for NumPy-stub syntax; GitHub Actions only executes Python 3.11, not a claimed-version matrix. Moreover, the configured mypy command currently fails with 49 errors. Thus mypy is neither proof of 3.10 compatibility nor presently a green source-analysis gate. |
| Security/static analysis (§20) | **Confirmed and expanded** | `pyproject.toml` globally skips B608 with a documented manual audit rationale. The baseline command now exits non-zero and reports 12 low findings; surfaced examples include `subprocess` import/Popen in daemon client and an `except Exception: pass` around vector-extension teardown in `db_write_coordinator.py`. The latter also conflicts with the project’s no-silent-exceptions rule. |

## Backlog reconciliation

| Review backlog item | Status today | Required next proof/action |
|---|---|---|
| P0.1 Relation ranking | **Open** | Decide boost vs penalty; add controlled FTS ordering test; align formula, constant name, docs. |
| P0.2 Retrieval trace | **Open** | Add structured, privacy-aware trace with stage values, reasons, and timings. Do not mislabel `explain_mode` as this. |
| P1.1 Gold corpus | **Partial** | Framework and seed real-history/golden material exist; finish a versioned human-judged corpus with the proposed labels and harmful cases. |
| P1.2 Baseline | **Partial** | Matrix/statistical runner exists; run it reproducibly, commit/version result metadata and decision threshold. |
| P1.3 Windows recall case | **Not evidenced** | Windows-specific mocked process tests exist, but no canonical cross-session retrieval regression case was found. |
| P1.4 Chunk-aware default experiment | **Ready, unexecuted** | Matrix can compare topic rerank, but no results justify a default flip. |
| P1.5 Engineering-token retrieval | **Open** | Build identifier/path/flag benchmark first; no alternative lexical mechanism is implemented. |
| P1.6 RRF calibration | **Open** | No weighted/confidence RRF implementation or comparative result. |
| P1.7 Chunk-aware cross-encoder | **Open** | Current reranker is capped/truncated entity-prefix scoring. |
| P1.8 Memory-type calibration | **Partial** | Type preference exists and matrix candidates retain `memory_type`; no calibrated class-specific policy is implemented. |
| P1.9 Temporal suite | **Substantially done** | Strong unit coverage exists; add explicit cross-owner conflict/current-vs-historical acceptance cases. |
| P2.1–P2.3 Agent-aware context, query expansion, action API | **Open** | Current modes are related but no structured context, expansion, or explicit action contract exists. |
| P2.4 A/B agent outcomes | **Open** | No agent-task outcome experiment artifact found. |

## Cross-check of the review’s remaining recommendations and verdict sections

Sections 1, 21, and 27 are consolidated by the executive conclusion and backlog above. The remaining normative sections are reconciled here so that every section of the input review has an explicit current-state disposition.

| Input section | Current disposition |
|---|---|
| §22, “What not to optimize prematurely” | **Still sound.** The present matrix/evaluation framework makes this advice more actionable, but there is no completed baseline that would safely support default changes or increasingly complex formulas. |
| §23, suggested retrieval direction | **Partially present, not a delivered target architecture.** Current code has lexical + semantic retrieval, fusion, supersession handling, optional chunk scoring/cross-encoder, type/temporal policy, and abstention. It lacks a context analyser, dedicated lexical/code branch, candidate-union abstraction beyond current RRF, chunk-aware cross-encoder, and minimal-useful-set policy. |
| §24, retrieval-success definition | **Appropriate product definition, not an implemented acceptance signal.** Candidate recall/ranking/validity are partially measurable in the evaluation framework; actual agent utility is not recorded. |
| §25, planning questions | **Not enforceable today.** The source and plans document several concerns, but no structured change template or automated gate requires authors to state failure mode, metric, cost, compatibility, observability, migration, and rollback. |
| §26, proposed acceptance criteria | **Partially supported only.** Unit tests and the evaluation machinery support correctness and comparative retrieval work. There is no checked-in gold-corpus regression threshold, p50/p95 budget gate, compatibility matrix, trace requirement, or automated stale-memory-rate policy. |
| §27, final verdict | **Mostly confirmed, but update its ordering.** Avoiding a retrieval rewrite remains correct. However, the new evidence makes restoration of failing Ruff/mypy/Bandit gates a co-equal near-term engineering priority alongside relation semantics, observability, and a completed baseline. |
| §28–29, reviewed locations/verification note | **Superseded by this report’s evidence map and executed checks.** This audit additionally inspected daemon/RPC, evaluation, CI, and baseline artifacts and explicitly records what passed, failed, or remains unproven. |

## Additional current-state findings not fully reflected in the review

1. **CI definition and local gate reality diverge from the review’s positive tooling posture.** Workflow jobs run unit tests and all listed quality commands on Python 3.11, but current local `ruff`, `mypy`, and `bandit -b` fail. Treat the unit pass as meaningful behavioral evidence, not release readiness.
2. **The benchmark framework itself contributes to current Ruff failures.** New benchmark files contain unused imports and complexity-rule failures. This does not invalidate their unit tests, but it prevents the project’s declared lint gate from passing.
3. **Daemon ownership is stronger than the original static review could establish.** Current source explicitly separates production RPC backend from direct test dispatch and protects in-flight work through daemon state/coordination. Full tests exercised daemon behavior, though that remains local rather than a multi-process production-load proof.
4. **Strict retrieval trades recall for grounded precision by design.** The relevance gate’s own source documents a rejected absolute-vector-distance approach after a 21k-entity holdout. This is good evidence of deliberate calibration, but it strengthens—not weakens—the need to measure recall/cost by case class.

## Prioritized recommendation

1. Fix or explicitly specify relation-count ranking before further scoring work.
2. Restore quality-gate truthfulness: resolve current Ruff and mypy failures; investigate/update the Bandit baseline deliberately, including the silent broad exception.
3. Finish and run the existing judged evaluation workflow before adding weighted RRF, engineering-token indexing, default chunk reranking, or chunk-aware cross-encoding.
4. Add structured retrieval traces so each benchmark loss can be assigned to candidate generation, fusion, reranking, temporal resolution, or gating.
5. Extend the evaluation corpus with the Windows/cross-agent regression case, identifier-heavy cases, stale/correction cases, and explicit harmful distractors. Only then make product-default decisions.

## File-level evidence map

- Daemon/RPC boundary: `src/saltmdb/daemon/server.py`, `src/saltmdb/daemon/client.py`, `src/saltmdb/mcp/server.py`, `src/saltmdb/mcp/tools.py`.
- Retrieval/ranking: `src/saltmdb/domain/services/memory_service.py` (`_run_fts_search`, `reciprocal_rank_fusion`, `accept_or_abstain`, `search_memory`).
- Configuration: `src/saltmdb/config.py` (relation penalty, rerank/chunk/cross-encoder/strict constants).
- Text normalization: `src/saltmdb/utils/text.py::sanitize_fts_query`.
- Reranker: `src/saltmdb/domain/services/reranker_service.py`.
- Evaluation: `scripts/benchmarking/run_evaluation_matrix.py`, `eval_configs.py`, `judge_pool.py`, `merge_judgments.py`, `analyze_evaluation_matrix.py`, `eval_stats.py`, `golden_queries.json`.
- Behavioral tests: `tests/test_e2e_topic_rerank.py`, `test_topic_rerank.py`, `test_relevance_gate.py`, `test_supersession_resolution.py`, `test_strict_ranking_defaults.py`, `test_search_pagination.py`, daemon tests, and evaluation-tool tests.
- Tool/CI configuration: `pyproject.toml`, `.github/workflows/python-tests.yml`, `.bandit-baseline.json`.
