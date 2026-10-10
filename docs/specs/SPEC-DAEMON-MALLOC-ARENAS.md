# SPEC-DAEMON-MALLOC-ARENAS

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-022. Shared `context_id`: `perf-malloc-arenas-2026-10-10`.
- Baseline: the commit that adds this spec on `develop`, plus the `./verify` counts the developer records first.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `client._spawn_daemon_process` (observed through the `env` passed to a patched `subprocess.Popen`) and
  `service.render_unit` (the rendered systemd unit text).
- Scope (may edit): `src/saltmdb/daemon/client.py` (`_spawn_daemon_process`, env block about L285-287),
  `src/saltmdb/daemon/service.py` (unit renderer, about L52-70), `src/saltmdb/config.py` (one constant, next to
  `DAEMON_SPAWN_MIN_INTERVAL_S`), `tests/test_daemon_client.py`, `tests/test_daemon_service.py`.
- Does not touch: `daemon/server.py`, the models, the DB layer, `docs/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

With the ONNX memory arena off (BL-017, committed), the daemon still holds glibc per-thread malloc arenas that it rarely returns.
Measured with the BL-017 probe through the real services (RSS in MB after two 32-document batches and one rerank): arena off
and `MALLOC_ARENA_MAX` unset 714-810; with `MALLOC_ARENA_MAX=2` 362 after the first batch, 386 after the second, 450 after the
rerank. Alone, `MALLOC_ARENA_MAX=2` did nothing for the old arena-on code (1,533 MB), which is why this item follows BL-017.
glibc reads the variable when the process starts, so it must be in the daemon's environment from the spawn; setting it inside
`main()` is too late.

Every POSIX spawn (adapter, CLI, lazy start, `daemon start`, the win32 second hop) goes through `_spawn_daemon_process`, which
builds `env = dict(os.environ)` for both of its `Popen` branches. The systemd unit renders its own environment. A hand-started
`python -m saltmdb.daemon.server` does not get the setting; that is accepted.

Risk, accepted and measured: the variable only caps how many glibc arenas the daemon's ~26 threads share, so it can add malloc lock
contention during concurrent inference and RPC. There is no correctness risk. Acceptance 4 measures latency with and without it.

Rejected (do not re-explore without new evidence): `mallopt` through `ctypes` at the top of `main()` (glibc only, unsafe on musl,
platform code); `malloc_trim` after batches (needs a hook in the embedding worker and a timing decision). Revisit only if the plain
variable is not enough.

## 2. Decisions

- D1. One constant `DAEMON_MALLOC_ARENA_MAX = "2"` in `config.py`, with a comment naming the measurement above.
- D2. `_spawn_daemon_process` does `env.setdefault("MALLOC_ARENA_MAX", DAEMON_MALLOC_ARENA_MAX)` right after
  `env = dict(os.environ)`, on every platform (the variable is ignored outside glibc). A value the owner already exports wins.
- D3. The rendered systemd unit gets the line `Environment=MALLOC_ARENA_MAX=2`, built from the same constant, directly after
  the existing `Environment=SALTMDB_DB_PATH=...` line.

## 3. Changes per file

Exact edits in the three source files as D1 to D3.

## 4. Tests

Written first; red against the baseline.

- `tests/test_daemon_service.py` `test_render_unit_exact_text` (L56-72) pins the whole unit text: update it for D3. The test at
  about L96 compares the installed file to `render_unit` itself and stays consistent.
- `tests/test_daemon_client.py`: add a test that the spawn env contains `MALLOC_ARENA_MAX=2` when the parent environment does not
  set it, and one that a pre-set value (for example `"4"`) is kept. Follow the neighbouring env tests at about L737 and L749
  (patch `os.environ` and `subprocess.Popen`, call `_spawn_daemon_process`). Those two existing tests assert single keys and stay
  unchanged.

## 5. Out of scope

`mallopt`, `malloc_trim`, thread counts, the model arena (done in BL-017), hand-started daemons, a drift check for installed
units. Owner note for the hand-off: an already installed systemd unit does not pick up D3 until `saltmdb-cli daemon install`
is run again (`service.py` renders and writes it on install only); a daemon spawned by an adapter picks up D2 as soon as the new
code is installed and the daemon restarted.

## 6. Acceptance

1. Before editing: `./verify`; record counts and pre-existing failures.
2. Tests written first, red for the stated reasons; after implementing,
   `.venv/bin/python -m pytest tests/test_daemon_client.py tests/test_daemon_service.py -q` exits 0.
3. RSS measurement on the committed BL-017 code. Save this script as a file in the scratchpad directory (not in the repo) and run it
   twice with `MALLOC_ARENA_MAX` unset and twice with `MALLOC_ARENA_MAX=2`; report all four `steady_rss_mb` values (expected about
   700-800 unset and about 450 with the variable):

   ```python
   from saltmdb.domain.services import embedding_service as es, reranker_service as rs
   def rss_mb():
       return int([l for l in open("/proc/self/status") if l.startswith("VmRSS")][0].split()[1]) // 1024
   doc = "SALTMDB architecture note about retrieval and ranking. " * 120
   es.embed_texts_in_batches([doc] * 32)
   es.embed_texts_in_batches([doc] * 32)
   list(rs.get_model(rs._BUNDLED_MODEL_NAME).rerank("how does startup work", [doc[:2000]] * 10))
   print("steady_rss_mb", rss_mb())
   ```
4. Latency evidence with the variable unset versus `=2` (same script style, a fresh process each, each measured twice): the median
   over 30 calls of embedding one short query (`es.embed_text("how did we fix the slow mcp startup")` after a warm-up call), and the
   wall time of three rerank calls of 10 candidates. Report all numbers; a regression of more than 25% in either median with the
   variable set is a finding to report to the architect before handing off, not a reason to edit anything.
5. `./verify` exits 0. One full suite at a time.
6. `git status --short` lists only files named in section 0.

## 7. Pre-lock gate notes

- Content search of `Environment=SALTMDB_DB_PATH` and `SALTMDB_DAEMON_SPAWNED_AT` in `src` and `tests`: the unit text is pinned only
  in `tests/test_daemon_service.py` L65 and the env keys only in `tests/test_daemon_client.py` L737 and L749.
- Consultant pre-lock review done: spawn paths, pinned tests, the unit-install caveat and the latency evidence were added from it.
