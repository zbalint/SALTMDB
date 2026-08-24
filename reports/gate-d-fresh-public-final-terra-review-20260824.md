# Gate-D fresh public dataset — final independent pre-retrieval Terra review

## Current outcome-free frozen-run verdict — `run-82d51b4`

**PASS — ready to freeze and execute the real public runner.** The signed spec, manifest, and freeze receipt validate and are mutually bound. The authorized public, text-free prior assignment artifact was independently verified and its three zero-overlap claims recomputed. The frozen run directory contains no retrieval/result artifacts; the receipt also records `first_outcome_exists: false`.

### Verified

* Signed fingerprints exactly match the supplied freeze identities: spec `4b476ff4…e099`, manifest `211c5d29…441be`, and receipt `49afda6b…3e30`.
* Current clean `HEAD` is `82d51b41f1ed0d2ebd8960fe7e33a0607a43311a`, SHA-1 format; its SHA-256 binding, runtime machine fingerprint, model lock/revision/cache, corpus root/manifest/export checksum, lexical receipt/snapshot checksum, adapter/rule hashes, and rubric fingerprint all match the freeze receipt/spec.
* Manifest validation passes with exactly 400 queries and every frozen facet quota. The signed source attestation and receipt both declare zero prior query-ID, family, and source overlap; `first_outcome_exists` is false and the run directory contains only spec, manifest, and freeze receipt.
* Safe public runner inputs exist: the frozen spec/manifest, public corpus manifest/export, lexical snapshot receipt/database, BGE ModelLock/cache, and a fresh run-private sidecar/output path. The snapshot database path is `lexical_snapshot.db`, not a live SALTMDB database. No database was opened in this review.
* Focused synthetic protocol/runner checks passed: **37 tests in 3.84s**; `git diff --check` and worktree status are clean.

### Independently recomputed prior-overlap proof

Using only the authorized text-free prior projection at `scratch/eval_results/accuracy-bakeoff-20260812/gate_d_devrun1/query_slot_assignments.json` (no source slots or prior query text), I verified its declared artifact fingerprint `40ce81817b1eefac2fff786cd04b107cde2ee75ff7fad6abe03f026fcc633402` and file SHA-256 `e4af5cf95e40b0ba9e61558ab64374334d6fc7949a7627f6efa55353462ad02d`. Its 1,200 assignments contain 1,200 query IDs, 960 topic-family IDs, and 270 source IDs. The fresh public manifest contains 400 query IDs, 248 topic-family IDs, and 178 source IDs. All three intersections are exactly zero, matching the signed freeze receipt.

## Current verdict — ready for commit and spec freeze

**Ready to commit and freeze the preregistered public retrieval workflow.** No open P0 or P1 remains in the deliberately narrowed scope. The final exclusive-persistence correction resolves the last one-time-evidence risk: an existing output is refused rather than overwritten. The only remaining item is the explicitly deferred P2 `PublicTimingExecutor.close()` `try/finally` hardening.

This final recheck was static/synthetic only; it did not invoke real retrieval, load the production model, or access a blind, source-slot, vault, or live database input.

### Resolved items

* Runtime identity now reads the actual clean checkout (`HEAD` plus Git object format) and an actual machine fingerprint; all three must equal the frozen spec. The environment value must be the same lower-case SHA-256 fingerprint ([run_fresh_development_retrieval.py:120-187](../scripts/benchmarking/run_fresh_development_retrieval.py)). A real run cannot start from the current dirty worktree, as intended.
* The ModelLock artifact fingerprint **and** its native resolved revision must agree with the spec ([468-475](../scripts/benchmarking/run_fresh_development_retrieval.py)). The logical FastEmbed model ID and the ModelLock's Qdrant ONNX source repository are intentionally distinct; the signed lock binds the source repository and revision.
* Production timing now accepts only an open `PublicTimingExecutor` whose spec and manifest fingerprints equal the initial retrieval result; the injectable callback path is private/test-only ([767-795](../scripts/benchmarking/run_fresh_development_retrieval.py)). This closes the cached-ranking timing loophole.
* Exact-title output parity is now a persistent dictionary title index that retains at most two IDs per byte-exact title—semantically equivalent to the production unique-or-collision SQL `LIMIT 2` decision ([235-264](../scripts/benchmarking/run_fresh_development_retrieval.py), [491-503](../scripts/benchmarking/run_fresh_development_retrieval.py)). For this fixed dataset, where the root independently verified no non-exact query/title collisions, it gives the same output and is acceptable for the **relative candidate-vs-baseline latency gate**: the lookup and the singleton fast path are identical in both arms. It is not a claim of identical absolute SQL timing.
* Dense-adapter startup is now inside the cleanup block, and its regression test confirms a pre-executor failure closes the lexical connection ([518-544](../scripts/benchmarking/run_fresh_development_retrieval.py)). A minimal non-injected `run_and_persist_public_fresh_retrieval` entry point produces a signed raw retrieval bundle and rejects injected backends or retained resources ([660-716](../scripts/benchmarking/run_fresh_development_retrieval.py)).

### Resolved final P1 — exclusive raw-evidence persistence

`persist_public_retrieval_bundle` now serializes first, opens the output with exclusive binary creation (`"xb"`), flushes and fsyncs it, and rejects a pre-existing path without changing its bytes ([660-714](../scripts/benchmarking/run_fresh_development_retrieval.py)). If writing a newly created file fails, it removes only that partial file. The regression test creates `first-evidence`, verifies persistence raises, and verifies the original bytes are unchanged ([test_run_fresh_development_retrieval.py:413-420](../tests/test_run_fresh_development_retrieval.py)). This is sufficient custody for the specified one-time run; no general replacement framework is required.

### P2 / deferred polish

`PublicTimingExecutor.close()` still does not use `try/finally`, so an exceptional lexical close could skip dense-stack cleanup. This is a low-probability cleanup hardening issue, not a reason to delay the one-time run once overwrite prevention is in place. Adding direct adversarial tests for ModelLock/spec revision mismatch and the exact-title index collision behavior would improve maintenance confidence.

Final focused verification: **37 tests passed in 3.75s** across the fresh protocol and public-runner suites; Ruff, format, and `git diff --check` passed.

## Historical static review — superseded provenance and timing findings

**Verdict: no P0 implementation defect found; do not execute the confirmation run as final evidence until the P1 provenance and timing-boundary issues below are closed.** This review was static plus synthetic tests only. It did not run real retrieval, load a model, open a live database, or open blind/source-slot/vault material.

### What is correct

* Native IDs are now represented correctly in the protocol: a Git commit has an explicit SHA-1/SHA-256 object format and matching 40/64-hex length, while the BGE resolved revision is an immutable 40/64-hex ID; the ModelLock remains a separate SHA-256 fingerprint ([fresh_development_protocol.py:151-175](../scripts/benchmarking/fresh_development_protocol.py), [220-369](../scripts/benchmarking/fresh_development_protocol.py)). The FastEmbed logical model ID (`BAAI/bge-small-en-v1.5`) is intentionally distinct from the Qdrant ONNX source repository carried by the signed ModelLock; the existing adapter maps that source repository back to the logical ID before loading.
* Public inputs are strongly cross-bound: spec/manifest, corpus root/export, lexical-snapshot receipt/database, and ModelLock fingerprint are all checked before execution ([run_fresh_development_retrieval.py:374-411](../scripts/benchmarking/run_fresh_development_retrieval.py)). BM25 uses the raw production adapter and raw lower-is-better scores; dense uses the pinned local ModelLock/cache and the same entity index semantics.
* The persistent `PublicTimingExecutor` does re-run lexical search, dense query embedding/search, identity handling, and the fixed fusion for each non-singleton timed call. The timing wrapper rejects a ranking that differs from the frozen initial evidence ([280-289](../scripts/benchmarking/run_fresh_development_retrieval.py), [565-608](../scripts/benchmarking/run_fresh_development_retrieval.py)).
* Protected-looking paths, symlinks, and named live database paths are rejected before the path loader opens them. Exact-title singleton and fall-through behavior is structurally fail-closed. The query manifest now rejects byte-duplicate query text.
* Focused static/synthetic checks passed: **55 tests in 13.28s**; Ruff and formatting checks passed; `git diff --check` passed. No real retrieval was invoked by this review.

### P1 — execution receipt copies critical runtime claims instead of independently verifying them

The runner validates that the *supplied* spec is well-formed, then embeds `spec["production"]` wholesale in the `ProductionConfigReceipt` ([332-353](../scripts/benchmarking/run_fresh_development_retrieval.py)). It never derives and compares the running checkout's Git ID, lexical-adapter fingerprint, exact-title-rule fingerprint, or machine identity. A different checkout/runtime can therefore produce a receipt that repeats the intended claim.

The same gap exists within dense provenance: the runner verifies the supplied ModelLock against its pinned repository/revision and checks its artifact fingerprint against the spec, but does not compare `spec.production.dense.resolved_revision` with `model_lock.resolved_revision` ([117-130](../scripts/benchmarking/run_fresh_development_retrieval.py), [386-391](../scripts/benchmarking/run_fresh_development_retrieval.py)). The lock fingerprint is strong indirect evidence, but the named spec field can still disagree with it.

Before the real run, add a custody-owned runtime receipt that derives `git rev-parse HEAD` from a clean checkout, hashes the actual adapter/exact-title implementation inputs, captures a canonical environment inventory, and compares all of them to the frozen spec. Require direct equality of the resolved model revision (and ideally record the ModelLock `source_repository` beside its logical model ID). This is a reproducibility/evidence blocker, not a ranking-algorithm defect.

### P1 — timing is not yet enforceably production-faithful

`build_timing_runner` accepts any callable that returns the previously recorded ranking. It checks only ranking equality, so a callback that merely returns cached rankings passes even though its docstring says it must execute full retrieval ([565-608](../scripts/benchmarking/run_fresh_development_retrieval.py)); the current timing test deliberately uses such a callback. Keep injectable callbacks in a test-only helper, but make the real runner accept and bind a retained `PublicTimingExecutor` created by this run, with the executor/spec/manifest/evidence fingerprints recorded in the timing receipt.

The exact-title result semantics are parity-compatible with the frozen eligible corpus, but the timed implementation is not performance-parity-compatible with live production: it linearly scans the in-memory title mapping ([162-187](../scripts/benchmarking/run_fresh_development_retrieval.py)), whereas production uses a bounded, indexed SQL `title = ? ... LIMIT 2` lookup ([memory_service/orchestrator.py:314-352](../src/saltmdb/domain/services/memory_service/orchestrator.py)). Use the same bounded snapshot-SQL query for the public runner, including the frozen visibility predicate. Otherwise p50/p95 are not honest production-path latency measurements, especially for the 16 singleton rows.

`environment_fingerprint` is only checked for string length, not hexadecimal form or derivation ([382-383](../scripts/benchmarking/run_fresh_development_retrieval.py)). It should be a schema-validated fingerprint of a canonical emitted environment receipt, not caller text. There is also no CLI/runbook that makes a real run reproducible, disallows injected test backends, persists the public evidence/timing artifacts, and guarantees executor closure. Add that before invoking the confirmation workflow.

### P2 — cleanup and test-hardening

If `adapter_model_lock(...)` raises at line 427 after the lexical connection opens, it is outside the following `try` block and the lexical connection is not closed ([418-452](../scripts/benchmarking/run_fresh_development_retrieval.py)). Also make `PublicTimingExecutor.close()` use `try/finally` so a lexical-close failure cannot skip dense-stack cleanup. Add adversarial tests for this path, callback ranking drift, ModelLock/spec-revision disagreement, malformed environment fingerprints, non-production exact-title timing parity, and retained-executor closure on all failure paths.

## Historical dataset verdict — superseded by current verdict

**The prior P0 is resolved.** The repaired public dataset has 400 globally unique query texts; all 20 exact-sentence rows now have distinct text, source ID, and topic family. Its current artifact chain is internally consistent and the committed protocol test set passes. This re-review did not access protected material or retrieval outcomes.

The remaining pre-freeze issue is a **P1 provenance-schema correction**. `production_commit` is named and described as a commit but is required to be a 64-character SHA-256 fingerprint ([fresh_development_protocol.py:151-154](../scripts/benchmarking/fresh_development_protocol.py), [207-240](../scripts/benchmarking/fresh_development_protocol.py)). This repository's actual `a0b7702e99b3f28c3a4124fd1a05884cac961d6b` is a 40-character SHA-1 Git object ID. A separately documented SHA-256 of that string is cryptographically binding only if the preimage is preserved, but it is not directly checkoutable/auditable as a Git commit. Preserve the exact Git ID in a field such as `production.git_commit` (allow lower-case 40- or 64-hex IDs, preferably with an explicit object format), and keep any SHA-256 as a separately named fingerprint.

The dense identity has the same defect: `bge_model_revision` / `production.dense.model_revision` is also forced through the generic 64-hex hash validator ([fresh_development_protocol.py:231-240](../scripts/benchmarking/fresh_development_protocol.py), [411-429](../scripts/benchmarking/fresh_development_protocol.py)), whereas the pinned Hugging Face resolved revision is the 40-hex Git-style value `52398278842ec682c6f32300af41344b1c0b0bb2`. That is distinct from the 64-hex ModelLock artifact fingerprint. Preserve three fields: `model_id`, `resolved_revision` (the actual Hugging Face revision), and `model_lock_fingerprint` (the SHA-256 artifact identity). Bind the production/execution receipt to the exact Git and model fields plus their separate fingerprints. This does not alter ranking behavior, but it is necessary for an honest reproducibility claim.

After that schema clarification, the dataset/protocol is ready for the preregistered two-arm retrieval run. No quota, query, candidate weight, or gate may change after the first outcome exists.

## Historical first-review verdict

The following P0 was the first review's prospective design correction. It is retained as audit history and is now resolved by the re-review above.

## Resolved P0 verification

The repaired public chain passed all of the following read-only checks at committed `a0b7702`:

* Signatures and reciprocal links were valid for query artifact `876102a4…3cf9` (SHA-256 `86de50e2…b0df`), assembly receipt `95c59dd6…d906`, records artifact `0dc08a57…9d83` (SHA-256 `05b4ecbb…e479`), and conversion receipt `c58e05dd…b3c`.
* There are 400 unique IDs and 400 unique nonblank query texts, 248 topic families, and 178 distinct source IDs. There are zero duplicate-text groups. These are direct counts of fields in the public records artifact; no source-slot input was opened.
* The prior exact-sentence failure is fixed: its 20 rows have 20 distinct texts, families, and source IDs, computed from those same public record fields.
* Exact-title construction is 16 `unique_byte_exact_singleton` plus four `byte_mismatch_fallthrough`, all with distinct texts/families/sources. The public assembly summary documents each control as a real byte-level title mismatch.
* Every frozen facet and subtype quota exactly matches the protocol; multilingual is seven each Latin non-English, Cyrillic, RTL Arabic, and CJK; lifecycle is seven families of four; every positive row has source IDs; all strict negatives are source-free.
* `fresh-q-0397` is now the clearly out-of-scope question “What is the weather forecast for Mars tomorrow?”, correctly classified as source-free `strict_negative/no_answer_out_of_scope`.
* Focused committed tests passed: `48 passed in 23.52s` for the fresh-protocol and canonical-ranking suites.

`prior_overlap_zero` is present in the public assembly validation receipt. It is an opaque custody attestation and cannot be independently rechecked without opening the prohibited protected set; the present review verifies its public-chain binding, not the hidden set itself.

Scope was limited to the requested unprotected code and public `fresh_dev` artifacts. No blind, vault, unlock, source-slot, private-mapping, or retrieval-result material was opened, enumerated, or generated.

## Historical P0 — resolved exact-sentence replication defect

`records_fresh_dev.json` has 20 `exact_sentence` records (`fresh-q-0077` through `fresh-q-0096`) but exactly ten identical-text groups. Each group has two records with the same `topic_family_id`; there are consequently only ten distinct texts and ten exact-sentence families. The ten duplicate strings are all verbatim same-family pairs, rather than independent query cases.

This violates the intended practical meaning of the 20-case exact-sentence quota. It reduces coverage and independent safety evidence by half. It also double-counts those cases in query-level exact-safety reporting; the family-macro NDCG endpoint does not double-weight them, but it still has only ten exact-sentence clusters. The protocol itself only requires a non-empty query and does not reject duplicate query text ([fresh_development_protocol.py:555-625](../scripts/benchmarking/fresh_development_protocol.py)).

Resolution completed before retrieval: one item in each pair was replaced while preserving the fixed facet/subtype quota, the complete public chain was regenerated, and the re-review above confirmed 20 distinct exact-sentence texts, source IDs, and topic families. Do not reintroduce replicas into decision metrics; if a future experiment needs them, label them explicitly as a non-decisional reproducibility probe and exclude them from all gates.

## Production fidelity, evidence, and statistics

The two arms are exactly the intended fixed comparison: deployed lexical/BGE top-20 rank-RRF (`k=60`) versus one 1.5:1 score rerank over that identical RRF pool ([fresh_development_protocol.py:8-18](../scripts/benchmarking/fresh_development_protocol.py), [266-294](../scripts/benchmarking/fresh_development_protocol.py)). The derivation preserves SQLite-BM25’s lower-is-better sign, channel floor handling, RRF tie order, pool-only score normalization, and candidate tie order ([1067-1134](../scripts/benchmarking/fresh_development_protocol.py)). The focused parity regression deliberately exercises a union larger than 20 and compares against the canonical ranking helpers ([test_fresh_development_protocol.py:637-683](../tests/test_fresh_development_protocol.py)).

Evidence binding is now appropriately fail-closed at the protocol layer: it recomputes rankings from raw channel cells, requires the three judge artifacts and complete union labels, binds arbitration/merged labels, and validates deterministic warmup/interleaved timing and two arms only. The family bootstrap now operates on family means, matching the macro-by-family NDCG endpoint ([fresh_development_protocol.py:1550-1629](../scripts/benchmarking/fresh_development_protocol.py)). There are no performance or accuracy outcomes yet, so no practical/statistical conclusion can be made; the runner-supplied timing trace is the future evidence, not current latency evidence.

## Exact-title diagnostic review

The current field is correctly named `unique_corpus_match`. A singleton exact-title row must have a unique corpus match and be output at rank 1; a byte-mismatch control must have `triggered=false`, empty matches, null output/rank, and `unique_corpus_match=false` ([fresh_development_protocol.py:1018-1058](../scripts/benchmarking/fresh_development_protocol.py)). Non-exact-title cells must also be completely neutral ([1059-1066](../scripts/benchmarking/fresh_development_protocol.py)). Therefore stale exact-title diagnostics cannot pass this validator merely by being carried into another facet. The prior `eligible_corpus_unique` interpretation is no longer present.

This proves consistency of the supplied retrieval evidence, not that a supplied diagnostic came from a live corpus-wide byte-exact scan: the module explicitly documents that fingerprints and caller attestations cannot authenticate a producer ([fresh_development_protocol.py:14-18](../scripts/benchmarking/fresh_development_protocol.py)). The actual run must include the production/corpus receipt already required by the protocol and be reviewed as such.

## Non-blocking follow-ups

* Add a regression test that rejects duplicate query text (or requires an explicit non-decisional replication designation). This prevents the current data-quality failure from recurring silently.
* Report `exact_title` and `exact_sentence` safety separately in the final result in addition to the combined exact-safety gate. The combined non-inferiority check is logically safe here because exact-title parity is identical between arms and counts are equal, but separate reporting is more intelligible.
* Do not interpret the signed JSON chain as a security boundary. The caller can re-sign content-addressed artifacts; custody/authentication and human-confirmation remain external operational controls.

## Exact safe next step

First correct the Git/model provenance schema described in the current verdict, update its focused tests, and freeze a spec containing the real production Git ID, model ID, resolved model revision, and separate artifact fingerprints. Then bind the already-reviewed public artifact fingerprints in that immutable two-arm spec. Run exactly the two preregistered arms with the existing interleaved schedule, obtain independent judged labels and bound timing evidence, and apply the frozen gates. Do not change quotas, query text, candidate weights, or gates after the first retrieval outcome exists.
