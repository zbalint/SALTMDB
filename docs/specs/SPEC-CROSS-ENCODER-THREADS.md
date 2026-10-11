# SPEC-CROSS-ENCODER-THREADS

## 0. Status

LOCKED 2026-10-11.

- Backlog: BL-037 (new, added by this slice, status implemented). Shared `context_id`:
  `perf-cross-encoder-threads-2026-10-11`.
- Baseline: `develop` at the commit that contains this spec; `./verify` exit 0 (record counts).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `saltmdb.domain.services.reranker_service.get_model` (with `TextCrossEncoder`
  monkeypatched to record keyword arguments) and `saltmdb.config`.
- Scope (may edit): `src/saltmdb/config.py` (one constant after `CROSS_ENCODER_MAX_QUERY_CHARS`),
  `src/saltmdb/domain/services/reranker_service.py` (the three `TextCrossEncoder(...)` calls in
  `get_model`), `tests/` (one new file `tests/test_reranker_threads.py`), `docs/BACKLOG.md`.
- Does not touch: `embedding_service.py`, `related_memory_service.py`, any hook, `daemon/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

`saltmdb-cli related-memories` and `search_memory` strict mode spend almost all their time in the
cross-encoder: about 75-86 ms per (segment, candidate) pair, so about 0.65-0.9 s per segment of
10 candidates and up to four segments per reply. Measured 2026-10-11 on this 4-vCPU host (warm
model, 10 pairs of 300-character query and 1000-character candidates, median of 5):

| `threads=` passed to `TextCrossEncoder` | idle host | 2 busy CPU-bound processes running |
| --- | --- | --- |
| not passed (current) | 0.871 s | 0.872 s |
| 1 | 1.333 s | not run |
| 2 | 0.661 s | 0.678 s |
| 4 | 0.424 s | 0.888 s |

Scores are identical to within 1e-4 in every setting, so this changes speed only. The default
leaves ONNX Runtime on its own thread choice, which on this host runs about as fast as two threads.
Embedding one query segment costs 16 ms, so the embedding model is not worth touching.

Batching all segments' pairs into one call was measured too and rejected: 10 pairs 0.86 s, 20 pairs
1.50 s, so batching saves at most about 13%, and `TextCrossEncoder.rerank` takes one query per
call, so it would need a second inference path. Shorter candidate text would be faster but changes
scores, so it needs a quality evaluation and is not part of this slice.

Rejected: leaving it to the environment (no knob exists today), a hard-coded 4.

## 2. Decisions

- D1. `config.CROSS_ENCODER_THREADS: int` is `max(1, min(os.cpu_count() or 1, 4))`. It is a module
  constant, not an environment variable (no new user-facing setting).
- D2. `get_model` passes `threads=CROSS_ENCODER_THREADS` to all three `TextCrossEncoder(...)`
  constructions (bundled, bundled-fallback-online, online).
- D3. The cross-encoder's scores do not change: no change to query/candidate capping, models,
  thresholds or callers.

## 3. Changes per file

- `src/saltmdb/config.py`: add `import os` only if it is not already imported; add the constant with
  a comment giving the measurement summary and `# shortcut: capped at 4, raise if hosts with more
  cores show a measured gain`.
- `src/saltmdb/domain/services/reranker_service.py`: import the constant in the same
  `from saltmdb.config import ...` line style already used inside `get_model`; add the keyword to the
  three constructions. Nothing else.

## 4. Tests

`tests/test_reranker_threads.py`, written failing first:

- Monkeypatch `fastembed.rerank.cross_encoder.TextCrossEncoder` with a recorder, reset
  `reranker_service._model` and `_model_name`, call `get_model` for the bundled model name and for
  another name, and assert `threads == config.CROSS_ENCODER_THREADS` was passed each time.
- Assert `config.CROSS_ENCODER_THREADS == max(1, min(os.cpu_count() or 1, 4))` for the real
  `os.cpu_count()` (no reload, no new helper).

## 5. Documentation

`docs/BACKLOG.md`: add BL-037 (this change, implemented, with the table above summarised in one
sentence) and a note on BL-032 that the cross-encoder first-call load (3.7 s cold start of the model,
0.98 s warm file cache) is separate from this change.

## 6. Verification procedure

Default `./verify`; no parallel full runs.

## 7. Out of scope

- Batching across segments, shorter candidate text, a quantized model, embedding threads: measured
  or judged not worth it above; each needs its own quality evaluation.
- BL-032 warm-up (judged separately).

## 8. Acceptance

1. `git status --short` clean before editing; `./verify` exit 0, counts recorded.
2. New tests red before the change, green after.
3. `./verify` exit 0 after the change, counts recorded.
4. Timing on an isolated script, never the working daemon: with the model loaded through
   `reranker_service.get_model`, median of 5 runs of `model.rerank(query, 10 candidates)` with the
   query and candidates built as in section 1, before and after the change, host idle; scores
   before and after equal within 1e-4. Expected after: at most 0.55 s when the host is idle
   (measured 0.42 s). Report both numbers, `nproc`, and whether other heavy processes were running.
5. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

- Scope list vs acceptance: every changed file appears in section 3 or 5; the test file is named
  once.
- The constant is described once (D1) and referenced by name elsewhere.
- Code claims: three `TextCrossEncoder(` constructions at `reranker_service.py:128,141,146`;
  `ONNX_ENABLE_CPU_MEM_ARENA` already imported at the same place; measurements above were run with
  `TextCrossEncoder(..., threads=N)` on the bundled model.
- Contention result is a limit: under two busy processes `threads=4` matched the default (0.888 s
  vs 0.872 s), so the gain only shows on an idle host; no regression was measured.
- No consultant pass; locked on the gate alone.

## Amendment 1 (2026-10-11)

Pre-lock miss: four existing tests in `tests/test_reranker_service.py` (`test_get_model_uses_bundled_cache_dir_for_bundled_model_name`,
`test_get_model_falls_back_to_online_when_bundle_invalid`,
`test_get_model_falls_back_to_online_when_bundled_load_raises`,
`test_get_model_non_bundled_name_never_touches_bundle_logic`) assert the exact `TextCrossEncoder`
keyword arguments without `threads`, so D2 and a green `./verify` cannot both hold. Those
assertions pin the constructor contract (which model, which cache, offline or online), so they are
kept and updated, not deleted.

- Section 0 scope adds `tests/test_reranker_service.py`, limited to those four tests.
- Change: in each of the four expected-kwargs assertions add `threads=CROSS_ENCODER_THREADS`
  (import it from `saltmdb.config` at the top of the test file in the file's existing import style).
  Nothing else in that file changes.
- Section 4 replaced: the four updated tests are the constructor check (red first: they fail until
  `get_model` passes `threads`). `tests/test_reranker_threads.py` keeps only the formula test
  (`config.CROSS_ENCODER_THREADS == max(1, min(os.cpu_count() or 1, 4))` for the real
  `os.cpu_count()`); the recorder-based constructor test is dropped as redundant.
- Section 8.5 diff-stat list includes `tests/test_reranker_service.py`.
