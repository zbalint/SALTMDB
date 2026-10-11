# SPEC-DAEMON-MEMORY-LOG

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-022 (follow-up: the owner asked on 2026-10-11 for the daemon's memory use to be
  visible in `daemon.log`). Shared `context_id`: `daemon-memory-log-2026-10-11`.
- Baseline: `c265010` on `develop`; `./verify` exit 0, 2179 passed, 172 subtests.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Platforms: Linux and Windows only. macOS is out of scope (owner, 2026-10-11).
- Test seams: `saltmdb.utils.process_memory.format_process_memory()` and
  `saltmdb.utils.process_memory.read_process_memory()`; the daemon log lines through `caplog`.
- Scope (may edit):
  - `src/saltmdb/utils/process_memory.py` (new)
  - `src/saltmdb/daemon/server.py` (two call sites, section 3.2)
  - `src/saltmdb/domain/services/embedding_service.py` (one call site, section 3.3)
  - `src/saltmdb/domain/services/reranker_service.py` (one call site, section 3.3)
  - `src/saltmdb/domain/services/librarian_service.py` (one call site, section 3.4)
  - `tests/test_process_memory.py` (new)
  - `tests/test_daemon_server.py` (assertions added to one existing test, section 4)
  - `tests/test_embedding_service.py`, `tests/test_reranker_service.py`,
    `tests/test_librarian_service.py` (section 4)
  - `docs/architecture.md` (one sentence, section 5)
- Does not touch: `pyproject.toml` and any dependency list, `daemon/dispatch.py`,
  `daemon/protocol.py`, the viewer, the MCP adapter, `config.py`, `db/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The daemon is the one long-lived process that holds the ONNX models. Its resident memory was the
subject of the arena and `MALLOC_ARENA_MAX` work (BL-017, BL-022), and the only way to see it today
is to run `ps` or read `/proc/<pid>/status` by hand on the live host. The owner wants the number in
`daemon.log` so a regression shows up in the log next to the events that cause it.

Rejected alternatives:

- `psutil`: not a dependency, `deptry` would flag it, and the standard library covers both
  supported platforms.
- A periodic timer thread: adds a thread to a process that already runs about 26. Librarian
  maintenance already runs after each write-triggered pass (`store_memory` and `log_event` trigger
  it, and `run_librarian_now` submits `_run_librarian_maintenance` unconditionally, even when the
  mutation pass is skipped by its cooldown), so it provides a sample after nearly every write for
  free. An idle daemon logs no samples; that is accepted.
- macOS support (`resource.getrusage` gives peak only): the project supports Linux and Windows for
  now.

## 2. Decisions

- D1. One helper, `format_process_memory()`, returns the text appended to a log line. It never
  raises.
- D2. Linux reads `/proc/self/status` (`VmRSS` for current, `VmHWM` for peak; values are in kB).
- D3. Windows calls `GetProcessMemoryInfo` through `ctypes` (`WorkingSetSize` for current,
  `PeakWorkingSetSize` for peak; values are in bytes). "rss" on Windows therefore means working
  set; the log field names stay `rss_mb` and `peak_rss_mb` on both platforms.
- D4. Any other platform, or any read failure, yields `rss_mb=unavailable peak_rss_mb=unavailable`.
  Each failing call logs one `debug` message inside the helper (no once-per-process flag), and the
  helper never raises. The helper's own messages must not start with `Process memory (`.
- D5. Output format is exactly `rss_mb=<float, 1 decimal> peak_rss_mb=<float, 1 decimal>`, MB being
  1024 * 1024 bytes.
- D6. Five log events carry it, each as its own `logger.info` line starting `Process memory (`:
  daemon ready, embedding model loaded, reranker model loaded, librarian pass, and daemon shutdown.

## 3. Changes per file

### 3.1 `src/saltmdb/utils/process_memory.py` (new)

- `read_process_memory() -> tuple[float, float] | None`: returns `(rss_bytes, peak_bytes)` as
  floats, or `None` when unavailable. Branches on `sys.platform`, read at call time, not at import:
  `linux*` parses `/proc/self/status`; `win32` uses `ctypes`; anything else returns `None`.
- A private `_parse_proc_status(text: str) -> tuple[float, float] | None` does the Linux parse so it
  can be tested with literal text. It reads the `VmRSS:` and `VmHWM:` lines (`<n> kB`), and returns
  `None` if either is missing.
- The Windows binding follows `src/saltmdb/db/viewer_sessions.py` L10-22, not
  `_log_windows_job_diagnostics` (which sets no `argtypes`/`restype`). At module level, inside
  `if sys.platform == "win32":`, import `ctypes` and `wintypes`, create a private
  `kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)` (never `ctypes.windll`, whose cached
  function objects are process-wide), and define the structure `_PROCESS_MEMORY_COUNTERS` with these
  fields and types, in order: `cb` (`wintypes.DWORD`), `PageFaultCount` (`wintypes.DWORD`), then
  `PeakWorkingSetSize`, `WorkingSetSize`, `QuotaPeakPagedPoolUsage`, `QuotaPagedPoolUsage`,
  `QuotaPeakNonPagedPoolUsage`, `QuotaNonPagedPoolUsage`, `PagefileUsage`, `PeakPagefileUsage`, all
  `ctypes.c_size_t`. Set `kernel32.GetCurrentProcess.restype = wintypes.HANDLE` with
  `argtypes = []`, and `kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL` with
  `argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]`.
- A private `_read_windows() -> tuple[float, float] | None` sets `cb = ctypes.sizeof(counters)`,
  calls `K32GetProcessMemoryInfo(GetCurrentProcess(), byref(counters), counters.cb)`, returns `None`
  when it returns false, and otherwise `(float(WorkingSetSize), float(PeakWorkingSetSize))`.
- `format_process_memory() -> str`: calls `read_process_memory()` inside a `try/except Exception`
  that logs at `debug` and falls back to the D4 text.

### 3.2 `src/saltmdb/daemon/server.py`

- Directly after the `Daemon startup phases:` `logger.info` (L1078-1086), add
  `logger.info("Process memory (daemon ready): %s", format_process_memory())`.
- Directly before `logger.info("SALTMDB daemon shutdown complete.")` (L1231), add
  `logger.info("Process memory (daemon shutdown): %s", format_process_memory())`.
- Import `format_process_memory` at module level beside the other `saltmdb.utils` imports if there
  are any, otherwise beside the other `saltmdb` imports.

### 3.3 `embedding_service.py` and `reranker_service.py`

Both loaders are named `get_model` and have three assignment branches (bundled, bundled-raises
fallback, online). Add one log call per loader, not one per branch, still inside the existing lock:
- `embedding_service.get_model` (L655-690): inside the inner `if _model is None:` block, after the
  outer `if/else` that assigns `_model`, log `Process memory (embedding model loaded): ...`.
- `reranker_service.get_model` (L110-150): directly after `_model_name = model_name` (L149), log
  `Process memory (reranker model loaded): ...`. The reranker reloads, and logs again, when the
  requested model name changes; that is intended.
If a constructor raises, no line is logged. Do not change the existing `Loading bundled ONNX`
messages.

### 3.4 `librarian_service.py`

In `_run_librarian_maintenance` (L272-290), after the `PRAGMA optimize` try block (so it runs
whether or not the optimize succeeded), add
`logger.info("Process memory (librarian pass): %s", format_process_memory())`.

## 4. Tests

Written failing first.

`tests/test_process_memory.py`:

1. `_parse_proc_status` on a literal text containing `VmRSS:\t  204800 kB` and
   `VmHWM:\t  307200 kB` returns `(209715200.0, 314572800.0)`.
2. `_parse_proc_status` returns `None` when `VmHWM` is missing, and when the text is empty.
3. With `read_process_memory` patched to return `(209715200.0, 314572800.0)`,
   `format_process_memory()` returns exactly `"rss_mb=200.0 peak_rss_mb=300.0"`.
4. With it patched to return `None`, `format_process_memory()` returns exactly
   `"rss_mb=unavailable peak_rss_mb=unavailable"`.
5. With it patched to raise `OSError`, `format_process_memory()` returns the same unavailable text
   and does not raise.
6. With `sys.platform` patched to `"darwin"`, `read_process_memory()` returns `None`.
7. On Linux only (skip when `sys.platform` does not start with `linux`): the real
   `read_process_memory()` returns two positive floats with `peak >= rss`.
8. On Windows only (`skipif(sys.platform != "win32")`): the real call returns two positive floats
   with `peak >= rss`. This test cannot run on the Linux host; see section 8.

`tests/test_daemon_server.py`: extend the existing real-subprocess SIGTERM test that already
parses `Daemon startup phases:` from the daemon's captured `output` (L808-818). After the phases
assertions and before `finally`, assert that `output` matches both
`Process memory \(daemon ready\): rss_mb=\d+\.\d peak_rss_mb=\d+\.\d` and
`Process memory \(daemon shutdown\): rss_mb=\d+\.\d peak_rss_mb=\d+\.\d`. On a non-Linux, non-Windows
host the values would be `unavailable`, but this repo's CI is Linux, so the numeric pattern holds.
Add no new test method.

On Windows only, `tests/test_process_memory.py` also asserts `ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
== 72` (the 64-bit Windows size; `wintypes.DWORD` is 8 bytes on Linux, so the struct is 80 there and
the assertion must be skipped off Windows).

The three service call sites are covered by unit tests that need no model files, because the
existing tests already patch `fastembed.TextEmbedding` and `fastembed.rerank.cross_encoder.
TextCrossEncoder`:
- `tests/test_embedding_service.py`: extend one existing `get_model()` test (around L195-237) with
  `assertLogs("saltmdb.domain.services.embedding_service", "INFO")` and assert one record whose
  message starts with `Process memory (embedding model loaded): rss_mb=`.
- `tests/test_reranker_service.py`: the same for one existing `get_model()` test (around L191-271),
  expecting `Process memory (reranker model loaded): rss_mb=`.
- `tests/test_librarian_service.py`: a new test calling `_run_librarian_maintenance` with a
  `MagicMock` connection under `assertLogs("saltmdb.domain.services.librarian_service", "INFO")`,
  expecting a record starting `Process memory (librarian pass): rss_mb=`.
These three files join the section 0 scope.

## 5. Documentation

`docs/architecture.md`, in the paragraph about the maintenance pass (L212): add one sentence that the
daemon logs `Process memory (...)` lines at ready, after each model load, after each write-triggered
maintenance pass and at shutdown, with `rss_mb` meaning resident set on Linux and working set on
Windows. Do not edit the rest of that paragraph or L206 (they predate this spec and are not
corrected here).

## 6. Verification procedure

`./verify` alone on the host. Do not start parallel full runs.

## 7. Out of scope

- macOS, and any platform other than Linux and Windows.
- A timer thread, a viewer panel, an RPC or CLI command that reports memory, and thresholds or
  alerts.
- Per-thread or per-allocator breakdowns, and `Private_Dirty`/`PrivateUsage` fields.
- Changing `MALLOC_ARENA_MAX`, the ONNX arena setting or any other memory behavior.

## 8. Acceptance

1. `./verify` exits 0. Expected counts: baseline 2179 passed plus the new tests, 172 subtests.
2. `rg -n "Process memory \(" src` lists exactly five `logger.info` call sites: daemon ready,
   daemon shutdown, embedding model loaded, reranker model loaded, librarian pass.
3. On an isolated daemon (temp home, a copy of the database in a gitignored scratch directory on the
   real disk, a free viewer port; never the live daemon or database), started with
   `SALTMDB_RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-6-v2` in its environment, run one
   `search_memory` (loads both models) and one `store_memory` or `log_event` (triggers a librarian
   pass). Show the `Process memory (` lines from its `daemon.log`: ready, embedding, reranker,
   librarian pass; then stop it and show the shutdown line.
4. `git diff --stat` lists only the files in section 0 scope.
5. The Windows test (section 4, item 8) was not run. The report states that plainly; the architect
   decides whether to run it on a Windows host before release.

## 9. Pre-lock gate notes

- Step 5: `rg -n "Process memory" src tests` returned no hits at `c265010`; the strings are new,
  so no fixture holds an old copy.
- Step 9: the L808 test is a real-subprocess run whose `output` already contains the startup-phases
  line, so it also contains the ready and shutdown lines (the shutdown line is logged before the
  process exits and the test reads `output` after `proc.wait`).
- Step 11: a throwaway probe of the section 3.1 shape (module-level `if sys.platform == "win32":`
  block, private `WinDLL`, the structure with `DWORD`/`c_size_t` fields, `restype`/`argtypes`), run
  with the repo `.venv` mypy, reported "no issues found" both for the default Linux platform and with
  `--platform win32`. An earlier probe showed the struct is 80 bytes on Linux (`wintypes.DWORD` is 8
  bytes there) against 72 on 64-bit Windows, which is why the size assertion is Windows-only. The
  Windows call itself is not executed pre-lock; it is a post-implementation check on a Windows host.
- Consultant review (m_1281): F1-F6 applied: acceptance 8.3 now sets the reranker env var and
  triggers a write; the Windows binding follows `db/viewer_sessions.py` with explicit field types,
  `restype` and `argtypes`; the librarian is described as write-triggered; loader log placement
  named; three unit tests added for the service call sites; D4 and the helper's debug-message
  prefix made unambiguous; line nits fixed.
- Step 7: the log-event list appears in D6 and sections 3.2-3.4, 8.2; all name the same five events.
- Re-grep after the review edits (steps 7, 10): `rg -n "periodic|getattr|windll"` on this spec
  shows only the rejected-alternative bullet and the "never `ctypes.windll`" instruction.
- No tester pass is possible pre-implementation; the consultant review (one pass) is done.
