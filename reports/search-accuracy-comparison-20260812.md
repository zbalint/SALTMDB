# Post-Deployment Search-Accuracy Comparison — 2026-08-12

## Decision

**Keep every new search option disabled by default.** The frozen replay completed without search
failures, but no contender satisfied the accuracy/safety promotion contract. Cross-encoder
reranking improved judged semantic rank-1 relevance, yet it materially regressed exact sentence
and keyword retrieval. Chunk candidates provided no semantic exact-ID gain and caused still larger
safety regressions. Supersedes collapse was neutral on semantic relevance but lost exact positive
hits. Retrieval text had zero candidate coverage and therefore supplied no accuracy evidence.

The running live MCP daemon also lacked `SALTMDB_RERANKER_MODEL`: a forced cross-encoder health
query reported `executed=false`, `execution_count=0`, and returned no `cross_encoder_score`.
Direct bundled-model preflight succeeded, isolating this as a live daemon configuration problem.

## Frozen evidence

- Runtime commit: `b703d1da3bce31e6e61eaccbcdbaa36a006ffda8` (`0c5c31a` is an ancestor).
- Disposable snapshot SHA-256: `12687e72f9b344d2c235964eb7641998a14ff5491b5569f139d61f15528de6a9`.
- Query fingerprint: `0ae0e7a72d7a92cb5324cae6f715bb725b1442596906c53c1eabcd0f911ed775`.
- Configuration fingerprint: `475016a31fc62a7d2fb129a43f3b339c1b895207cf157df3e509e0b554c89d6f`.
- Judge-version fingerprint: `a93a024d343e5e7db017383cc932217482d21a84c7f417d3fc639bd4e46e3f3a`.
- Signed replay-manifest fingerprint: `fb5413badb1241f73d65b09036046900b25baf445ace2ba8e8b1ee9f945d6a2c`.
- Corrected result-artifact fingerprint: `1e4a3fe9065c86f772cde52749303d2ee612b54dea7a5a3dfbfb763f41dc284c`.
- Replay size: 600 frozen queries × 8 interleaved configurations, `limit=10`, zero errors.
- Reconstruction: 600/600 exact historical rows recovered; zero missing, ambiguous, or duplicate
  rows; all 150 semantic paraphrases exactly matched the earlier frozen manifest.

## Historical versus deployed control

Historical/new deltas are contextual because the live corpus changed. Paired promotion decisions
use the same-snapshot comparisons in the next section.

| Condition | Historical | New control | Delta |
|---|---:|---:|---:|
| Sentence exact top-1 | 145/150 (96.7%) | 145/150 (96.7%) | 0.0 pp |
| Sentence exact top-10 | 150/150 (100.0%) | 150/150 (100.0%) | 0.0 pp |
| Semantic exact top-1 | 69/150 (46.0%) | 66/150 (44.0%) | -2.0 pp |
| Semantic exact top-10 | 127/150 (84.7%) | 127/150 (84.7%) | 0.0 pp |
| Keyword exact top-1 | 113/150 (75.3%) | 113/150 (75.3%) | 0.0 pp |
| Keyword exact top-10 | 145/150 (96.7%) | 145/150 (96.7%) | 0.0 pp |
| Negative target absent top-10 | 148/150 (98.7%) | 148/150 (98.7%) | 0.0 pp |

## Same-snapshot exact-ID comparison

`W/L/T` is paired exact positive top-1 versus control across the 450 sentence, semantic, and
keyword queries. Direct-service latency is diagnostic only and does not satisfy the persistent
daemon promotion gate.

| Configuration | Sentence top-1 | Semantic top-1 | Keyword top-1 | Negative pass | W/L/T | Direct p95 |
|---|---:|---:|---:|---:|---:|---:|
| Control | 145 | 66 | 113 | 148 | 0/0/450 | 51.8 ms |
| Chunk mid (8/40/1.0) | 118 | 66 | 99 | 149 | 10/51/389 | 75.3 ms |
| Chunk high (12/60/1.5) | 117 | 66 | 99 | 149 | 11/53/386 | 84.9 ms |
| Collapse only | 142 | 66 | 110 | 148 | 0/6/444 | 52.0 ms |
| Chunk mid + collapse | 115 | 66 | 97 | 149 | 10/56/384 | 80.0 ms |
| CE ambiguity-gated | 131 | 78 | 110 | 148 | 37/42/371 | 714.0 ms |
| CE forced | 122 | 79 | 106 | 148 | 40/57/353 | 765.2 ms |
| Retrieval text | 145 | 66 | 113 | 148 | 0/0/450 | 83.6 ms |

## Semantic rank-1 relevance review

Three Luna Max judges labeled all 1,200 semantic query/config rank-1 results using the frozen
four-category rubric. Counts below are across 150 paraphrase queries.

| Configuration | Same specific fact | Same-topic sibling | Broadly related | Irrelevant |
|---|---:|---:|---:|---:|
| Control | 99 | 22 | 15 | 14 |
| Chunk mid | 99 | 20 | 15 | 16 |
| Chunk high | 97 | 21 | 15 | 17 |
| Collapse only | 99 | 22 | 15 | 14 |
| Chunk mid + collapse | 99 | 20 | 15 | 16 |
| CE ambiguity-gated | 114 | 22 | 9 | 5 |
| CE forced | 112 | 25 | 8 | 5 |
| Retrieval text | 99 | 22 | 15 | 14 |

For `SAME_SPECIFIC_FACT`, exact paired McNemar results versus control were:

- CE ambiguity-gated: control-only 11, contender-only 26, raw exact `p=0.0201`.
- CE forced: control-only 14, contender-only 27, raw exact `p=0.0596`.
- With the seven predeclared contender comparisons treated as the Holm family, the gated result
  does not remain below `0.05` (Holm-adjusted `p≈0.1405`).

These labels establish a useful semantic signal for gated CE, but cannot establish the formal
NDCG@10/Recall@20 promotion gates because only rank-1 was judged in this historical replay.

## Optional-channel execution and coverage

- Chunk mid/high and chunk+collapse executed for 600/600 queries with fresh candidates on every
  query and zero candidate shortfall. Their accuracy regressions are therefore substantive, not
  caused by missing chunk coverage.
- Supersedes collapse changed 28 result pools alone and 33 with chunk mid. This proves eligible
  complete chains were present, but the resulting exact-ID changes were net negative and judged
  semantic relevance was unchanged.
- Gated CE executed on 401/600 queries; forced CE executed on 599/600. Zero-execution invalidation
  does not apply to the disposable-snapshot experiment.
- Retrieval text executed on 600/600 queries but produced zero FTS candidates and zero vector
  candidates. Its ranking was byte-equivalent to control; this is zero coverage, not an accuracy
  tie with evidence.
- The chunk diagnostics currently omit `requested=true`, so the aggregate `requested_searches`
  field is misleadingly zero even though configuration and `executed_searches=600` prove request
  and execution. This diagnostics defect does not change rankings or the decision.

## Promotion-gate assessment

- Semantic quality: CE shows rank-1 judged improvement, but full grade-2 Recall@20 and NDCG@10
  evidence is absent; Holm-family significance does not pass.
- Safety: CE sentence top-1 regressed by 9.3–15.3 percentage points and keyword top-1 by
  2.0–4.7 points, far beyond the allowed 1-point regression. Chunk variants regressed more.
- Failures: passed (zero replay errors).
- Persistent-daemon latency: not run because accuracy/safety already fail; direct diagnostic p95
  remained below one second but is not promotion-grade evidence.
- Live readiness: failed for CE because the daemon was restarted without the reranker model
  environment variable.

No runtime defaults were changed.
