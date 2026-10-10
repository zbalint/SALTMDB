# SPEC-ONNX-MEMORY-ARENA

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-017. Shared `context_id`: `perf-onnx-arena-2026-10-10`.
- Baseline: the commit that adds this spec on `develop`, plus the `./verify` result counts the
  developer records before editing.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `embedding_service.get_model()` and `reranker_service.get_model()` (constructor
  keyword arguments, observed by patching `fastembed.TextEmbedding` and
  `fastembed.rerank.cross_encoder.TextCrossEncoder`), and the process RSS probe in section 6.
- Scope (may edit): `src/saltmdb/config.py` (one new constant after `EMBEDDING_BATCH_SIZE`),
  `src/saltmdb/domain/services/embedding_service.py` (`get_model`, three constructor calls and the
  config import), `src/saltmdb/domain/services/reranker_service.py` (`get_model`, three constructor
  calls), `tests/test_reranker_service.py`, `tests/test_embedding_service.py`.
- Does not touch: `scripts/` (benchmarks build their own models), `hooks/`, the models, the
  daemon, the DB layer, `docs/` (the architect updates the backlog), any other test file.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The daemon is the largest SALTMDB process. On the owner's 8 GB WSL2 host the live daemon held
1,094 MB of RSS, 1,010 MB of it anonymous memory (`/proc/<pid>/smaps_rollup`), against about 65 MB
for each adapter. The two bundled ONNX models are small: about 180 MB for the embedding model and
about 65 MB for the reranker right after loading. The rest is growth during use.

Measured with a throwaway probe through the real services (`embed_texts_in_batches` of 32 documents
of about 600 tokens, twice, then one rerank of 10 candidates; RSS in MB, two runs each where they
differed):

| Setting | after 1st batch | after 2nd batch | after rerank |
|---|---|---|---|
| default (ONNX Runtime CPU memory arena on) | 897 | 1,306 | 1,538 |
| `enable_cpu_mem_arena=False` | 713-809 | 714-810 | 714-810 |
| arena off and `MALLOC_ARENA_MAX=2` | 362 | 386 | 450 |
| arena on and `MALLOC_ARENA_MAX=2` | 901 | 1,310 | 1,533 |

The ONNX Runtime CPU memory arena keeps every buffer it ever needed and keeps growing with each new
batch, so a long-lived daemon only ever ratchets up. Turning it off makes RSS plateau instead
(1,538 down to about 700-800 here). glibc's per-thread allocator arenas then keep a further
250-350 MB; `MALLOC_ARENA_MAX=2` removes that, but only together with this change (alone it does
nothing). The allocator setting is a separate item (BL-022) and is not part of this spec.

fastembed already exposes the switch: `EXPOSED_SESSION_OPTIONS = ("enable_cpu_mem_arena",)` in
`fastembed/common/onnx_model.py` L28, applied at L156-157; unknown extra options trip an `assert`
at L151-154, and this one is accepted. `TextEmbedding` and `TextCrossEncoder` forward extra keyword
arguments to it.

Cost: single short query embedding latency is unchanged (about 6 ms with the host quiet, both ways).
A large background batch can be slower (a separate noisy timing probe showed 7.2 s against 11.7 s for
a workload dominated by one 32-document batch). Embedding jobs run in the background worker, so no
tool call waits on them. Rerank latency was unchanged within noise.

Rejected alternatives (do not re-explore without new evidence):
- Fewer ONNX threads (`threads=N`): saved nothing on RSS in the probe and slows inference.
- Unloading the models when idle: larger design, adds reload latency to the first search; revisit
  only if arena off plus BL-022 are not enough.
- Smaller `EMBEDDING_BATCH_SIZE`: lowers the peak but not the retained high-water mark, and is
  already bounded at 32 for an earlier incident (`config.py` comment above `EMBEDDING_BATCH_SIZE`).

## 2. Decisions

- D1. One module constant `ONNX_ENABLE_CPU_MEM_ARENA = False` in `src/saltmdb/config.py`, with a
  comment stating why (the arena never shrinks) and the upgrade trigger. Architect decision.
- D2. Every constructor call of `TextEmbedding` and `TextCrossEncoder` in the two services passes
  `enable_cpu_mem_arena=ONNX_ENABLE_CPU_MEM_ARENA`: three in `embedding_service.get_model` and three in
  `reranker_service.get_model`, including the online fallbacks.
- D3. No other behaviour changes: model names, cache dirs, `local_files_only`, locking, lazy loading
  and fallback order stay as they are.

## 3. Changes per file

### 3.1 `src/saltmdb/config.py`

Add after `EMBEDDING_BATCH_SIZE = 32`:

```python
# ONNX Runtime's CPU memory arena keeps every buffer it has ever needed and keeps growing with each
# new batch, so the long-lived daemon only ever ratcheted its RSS up (about 1.5 GB after two 32-document
# batches and a rerank, against about 0.7-0.8 GB without it). Disabling it costs some throughput on large
# background embedding batches; query embedding and rerank latency are unchanged within noise.
# shortcut: arena off for both models; set True only if measured background embedding throughput
# becomes a problem.
ONNX_ENABLE_CPU_MEM_ARENA = False
```

### 3.2 `src/saltmdb/domain/services/embedding_service.py`

Add `ONNX_ENABLE_CPU_MEM_ARENA` to the existing `from saltmdb.config import ...` line (L15). In
`get_model`, add `enable_cpu_mem_arena=ONNX_ENABLE_CPU_MEM_ARENA` as a keyword argument to each of the
three `TextEmbedding(...)` calls (the bundled load, the fallback after a load error, and the
not-bundled online load). Nothing else in the file changes.

### 3.3 `src/saltmdb/domain/services/reranker_service.py`

This file has no module-level `saltmdb.config` import; its existing style is a lazy import inside the
function (see `get_reranker_model_name`). Import `ONNX_ENABLE_CPU_MEM_ARENA` inside `get_model` the same
way and add `enable_cpu_mem_arena=ONNX_ENABLE_CPU_MEM_ARENA` to the three `TextCrossEncoder(...)` calls
(the bundled load, the fallback after a load error, and the plain online load). Nothing else changes.

## 4. Tests

Written first; they fail against the baseline for the right reason (a missing keyword argument).
Exactly these are red before the change: the four reranker tests named below and the three new
embedding tests. Nothing else is red.

`tests/test_reranker_service.py` (class containing the constructor-pinning tests, about L191-237;
match by name, not line): update the expected arguments of
`test_get_model_uses_bundled_cache_dir_for_bundled_model_name`,
`test_get_model_falls_back_to_online_when_bundle_invalid`,
`test_get_model_falls_back_to_online_when_bundled_load_raises` (its `kwargs ==` dict gains
`"enable_cpu_mem_arena": False`) and `test_get_model_non_bundled_name_never_touches_bundle_logic`.
Use the literal `False` in these expected values, not the constant. `TestGetModelLazySingleton` only
counts constructor calls and needs no change.

`tests/test_embedding_service.py`: add `TestGetModelArenaOption` with three tests. In `setUp` save the
module's current `embedding_service._model` and set it to `None`; in `tearDown` restore the saved value
(do not leave `None`, other tests share the real model through `token_count`). (a) valid bundle: patch
`embedding_service._is_valid_local_model` to return True and `fastembed.TextEmbedding`; call
`get_model()`; assert the single constructor call has `enable_cpu_mem_arena=False` and keeps
`local_files_only=True`. (b) invalid bundle: same with the validity patch False; assert the call is
`model_name="BAAI/bge-small-en-v1.5"` plus `enable_cpu_mem_arena=False`. (c) bundled load raises:
`side_effect = [RuntimeError("corrupt bundle"), MagicMock()]`; assert both calls carry
`enable_cpu_mem_arena=False`.

## 5. Out of scope

Thread counts, batch sizes, `MALLOC_ARENA_MAX` (BL-022), unloading models, quantised models, changing
the reranker or embedding model, SQLite pragmas, the adapter, the viewer, `scripts/benchmarking/*`
constructor calls, documentation.

## 6. Acceptance

Run in order, from the main checkout, with the repo `.venv`.

1. Before editing: `./verify`; record counts and any pre-existing failure.
2. Write the tests. `.venv/bin/python -m pytest tests/test_embedding_service.py tests/test_reranker_service.py -q`
   fails on exactly the seven tests named in section 4, and nothing else.
3. After implementing: the same pytest command exits 0.
4. Memory probe. On the baseline code it exits 1, printing `steady_rss_mb` of about 1,538 and
   `second_batch_growth_mb` of about 409; after the change it must exit 0 (steady below 1,000 and growth
   below 150; observed 786 and 1 with the option injected by a wrapper). Run it twice and report both outputs:

   ```sh
   .venv/bin/python - <<'EOF'
   from saltmdb.domain.services import embedding_service as es, reranker_service as rs
   def rss_mb():
       return int([l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]) // 1024
   doc = "SALTMDB architecture note about retrieval and ranking. " * 120
   es.embed_texts_in_batches([doc] * 32)
   first = rss_mb()
   es.embed_texts_in_batches([doc] * 32)
   growth = rss_mb() - first
   list(rs.get_model(rs._BUNDLED_MODEL_NAME).rerank("how does startup work", [doc[:2000]] * 10))
   steady = rss_mb()
   print("steady_rss_mb", steady, "second_batch_growth_mb", growth)
   raise SystemExit(0 if steady < 1000 and growth < 150 else 1)
   EOF
   ```
5. `./verify` exits 0 (ruff check, ruff format --check, mypy, bandit, pip-audit, deptry, pytest). Do not
   start a second full run in parallel.
6. `git status --short` lists only the five files in section 0 and nothing else (the architect commits
   this spec and the backlog change before the assignment).
7. `rg -n -U -A6 'TextEmbedding\(|TextCrossEncoder\(' src/saltmdb/domain/services/embedding_service.py src/saltmdb/domain/services/reranker_service.py`
   shows six constructor calls, each with `enable_cpu_mem_arena=ONNX_ENABLE_CPU_MEM_ARENA` in its
   argument list (ruff may wrap the one-line calls; read the wrapped form).

## 7. Pre-lock gate notes

- Scope against acceptance: constructor-argument assertions found by `rg -n 'TextEmbedding|TextCrossEncoder' tests`:
  `tests/test_reranker_service.py` (four pinned-argument tests, in scope) and
  `tests/test_retrieval_adapters.py` (patches `fastembed.TextEmbedding` for the scripts' own constructor,
  unaffected, not in scope). Tests that call the real `embedding_service.get_model()` use it for
  `token_count` only; arena off does not change token counts. `src/` has exactly the six constructor
  calls named in D2.
- The dedup path (`memory_service/duplicates.py`) gets its reranker through
  `reranker_service.get_model`, so it gets the same setting; there is no other call site.
- The constant is stated once (D1 and 3.1 agree on the name `ONNX_ENABLE_CPU_MEM_ARENA` and the value
  `False`).
- Library behaviour: fastembed support proven by `fastembed/common/onnx_model.py` L28, L151-157 in the
  repo `.venv` and by probe runs with the option on both models.
- The section 6.4 probe was run on the baseline (red: 1,538 and 409, exit 1) and, with the option injected
  through a wrapper around the fastembed constructors, green (786 and 1, exit 0); the run on the
  finished code is a post-implementation check.
- A consultant reviewed the first draft and found the unconsumed `rerank` generator in the probe, wrong
  test line ranges, an acceptance that could not pass, and the test teardown that would discard the shared
  model; this version fixes all of them.
