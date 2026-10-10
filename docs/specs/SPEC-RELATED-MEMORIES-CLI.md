# SPEC-RELATED-MEMORIES-CLI

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-025. Shared `context_id`: `answer-side-memory-hook-2026-10-10`.
- Baseline: `develop` after BL-024 is reviewed and committed; the developer starts from a clean
  tree and records the `./verify` counts first.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `related_memory_service.find_related_memories`, `related_memory_service.split_segments`,
  `reranker_service.score_pairs`, `daemon_client.call` (the `spawn` keyword), and the `saltmdb-cli
  related-memories` entry point (`cli.main` with patched stdin/stdout).
- Scope (may edit):
  - `src/saltmdb/domain/services/related_memory_service.py` (new)
  - `src/saltmdb/domain/services/reranker_service.py` (`score_pairs` only)
  - `src/saltmdb/domain/services/memory_service/search_primitives.py` and
    `src/saltmdb/domain/services/memory_service/duplicates.py` (extract the shared FTS term
    preparation, section 3.3)
  - `src/saltmdb/config.py` (new constants only)
  - `src/saltmdb/daemon/protocol.py`, `src/saltmdb/daemon/dispatch.py`, `src/saltmdb/daemon/client.py`
  - `src/saltmdb/cli.py`
  - tests: `tests/test_related_memory_service.py` (new), `tests/test_cli_related_memories.py` (new),
    plus `tests/test_daemon_client.py` (new tests for the `spawn` keyword, T18-T19), and other
    existing test files only where an existing test must be adjusted (name each in the completion
    report)
  - `docs/architecture.md` (one sentence in the CLI/hooks section)
- Does not touch: `docs/BACKLOG.md`, the hook scripts and hook tests, `search_memory`/orchestrator,
  `ranking.py`, the dedup decision logic, the viewer, `scripts/`.
- Hand-off: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Agents sometimes give advice or status claims that their own stored memories contradict. The plan
(owner-agreed, 2026-10-10) is a stop hook that searches memory with the agent's finished reply and
lists up to three memory ids and titles. The hook needs a read-only command that takes text and
returns related memories, with a relevance signal that is absolute, not rank-based.

Findings that shape this slice:
- The returned search `score` is rank-based (RRF) and cannot be a cutoff. Strict mode failed the known
  stale-advice example (the relevant memory scored below the strict topic threshold, and the
  irrelevant candidates sat just below it), so the strict gate alone is not enough.
- The cross-encoder is the candidate signal. Search only runs it when `SALTMDB_RERANKER_MODEL` is
  set; the dedup path bypasses that gate. This command must also not depend on the deployment flag.
- `daemon_client.call` always starts a daemon when none is running. A hook that runs on every Stop
  must never start one.
- With no caller identity, the search path applies no scope filter, so private memories of every
  agent would be searchable. This command is shared-scope only unless an agent id is passed.

This slice builds the command and the service only. The threshold is not calibrated here: the
replay harness (a later spec) measures it, and the hook (a later spec) uses it. The default
threshold is therefore a placeholder and the command is not wired into any hook by this spec.

Rejected: an MCP tool (agents do not call it; a second agent-facing search surface invites misuse);
folding the logic into `search_memory` (different contract, different gates); a second direct
`get_model(...).rerank` copy like the dedup code (extend `score_pairs` instead).

## 2. Decisions

- D1. New read-only RPC `find_related_memories`, classified in `READ_TOOLS`, dispatched like
  `get_last_session_digest`. No MCP tool.
- D2. Input is one text. The service splits it into segments (section 3.1), retrieves up to
  `RELATED_MEMORIES_POOL_PER_SEGMENT` (10) candidates per segment, scores each (segment, candidate)
  pair with the dedup cross-encoder model, and keeps each candidate's best score over the segments.
- D3. Candidate pool per segment: up to 5 from the OR-only FTS search in bm25 order, then the nearest
  semantic neighbours, skipping ids already taken, until 10 distinct candidates or no more. The
  semantic request asks for `min(RELATED_MEMORIES_POOL_PER_SEGMENT + len(exclude_ids),
  RELATED_MEMORIES_SEMANTIC_MAX_REQUEST)` neighbours, because the vector search applies the scope and
  exclusion filters after it picks the nearest ones, so excluded or private rows would otherwise eat
  the request. Both searches receive the same
  `where_clauses`/`params` (archived excluded, `exclude_ids`, scope), so exclusion and scope act
  before fusion. No other search channel is used.
- D4. Scope: with `agent_id` the clause is `(e.agent_id = ? OR e.scope = 'shared')`; without it, the
  clause is `e.scope = 'shared'`. Archived memories are always excluded. Candidates that are the
  target of a currently valid `supersedes` edge are dropped after retrieval, using
  `ranking._compute_superseded_ids_bitemporal`.
- D5. `score_pairs` gains `model_name: str | None = None`. When given, the call skips the
  `SALTMDB_RERANKER_MODEL` gate and uses that model; every other check (caps, empty candidates,
  output validation, diagnostics) stays. Default behaviour is unchanged.
- D6. Output filter: keep candidates whose best score is at least `min_score`; sort by score
  descending, tie by id ascending; return at most `limit`.
- D7. CLI: `saltmdb-cli [--db-path P] related-memories [--limit N] [--min-score X]
  [--exclude-ids ID,ID] [--timeout-ms N] [--json]`. Text on stdin (at most 100000 characters read).
  Default text output: one `<id>\t<title>` line per hit (title with tabs and newlines replaced by a
  space, cut to 150 characters); no hit gives empty stdout. Exit code 0 on every runtime path (a usage error from argument parsing, such as a non-numeric
  `--min-score`, still exits 2; the hook controls the arguments); problems go to stderr. `--json` prints every scored candidate (no `min_score`, no `limit`; `exclude_ids` still
  applies) as a JSON list of objects with keys `id`, `title`, `score`, `segment` (0-based index of the
  best segment), `fts_rank` (int or null), `semantic_distance` (float or null).
- D8. The CLI never starts a daemon: `daemon_client.call` and `call_method` gain a keyword
  `spawn: bool = True`; with `spawn=False` they use `reachable_daemon_info` instead of
  `ensure_daemon_running`, and neither of the two recursive retries in `call_method` is made (the
  connect-failure retry and the retry on `DAEMON_SHUTTING_DOWN`/`AUTH_FAILED`/`CALLER_SESSION_INVALID`,
  which would re-enter `ensure_daemon_running`); if no daemon answers, or the response is
  `DAEMON_SHUTTING_DOWN`, they raise `DaemonNotRunning` (new exception class in `client.py`); the other
  two codes raise the usual `DaemonRpcError`. The CLI turns any exception into empty
  stdout, a stderr line and exit 0.
- D9. The CLI has a hard time ceiling `--timeout-ms` (default 4000): the call runs in a daemon thread;
  when the wait expires the CLI prints nothing, writes a stderr line and exits 0.
- D10. Defaults live in `config.py`: `RELATED_MEMORIES_MAX_SEGMENTS = 4`,
  `RELATED_MEMORIES_MIN_SEGMENT_CHARS = 40`, `RELATED_MEMORIES_POOL_PER_SEGMENT = 10`,
  `RELATED_MEMORIES_FTS_SHARE = 5`, `RELATED_MEMORIES_SEMANTIC_MAX_REQUEST = 50`, `RELATED_MEMORIES_DEFAULT_LIMIT = 3`,
  `RELATED_MEMORIES_MIN_SCORE = 4.0`. Each has a one-line comment; the last also gets a `# shortcut:`
  comment: uncalibrated placeholder copied from the dedup threshold; calibrate with the replay harness
  before any hook uses it.

## 3. Changes per file

### 3.1 `related_memory_service.py` (new)

`split_segments(text: str) -> list[str]`: remove fenced code blocks (lines from a line starting with
three backticks to the next such line, inclusive; an unclosed fence runs to the end), remove full
UUIDs (regex `[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`), split into
paragraphs on blank lines, drop paragraphs whose stripped text, measured after the removals above and after collapsing
internal whitespace to single spaces, is shorter than `RELATED_MEMORIES_MIN_SEGMENT_CHARS` or consists
only of URLs (every whitespace-separated token starts with `http://` or `https://`). Selection: the last 2 remaining paragraphs plus the 2 longest of the rest (ties broken
by earlier position), at most `RELATED_MEMORIES_MAX_SEGMENTS` in total, kept in original order, each cut
to `CROSS_ENCODER_MAX_QUERY_CHARS`. Pure function.

`find_related_memories(conn, db_path, text, *, limit=None, min_score=None, exclude_ids=None,
agent_id=None, with_all=False) -> list[dict]`: `limit`/`min_score` default to the config constants.
Per segment: build FTS terms with the shared helper (3.3), retrieve per D3/D4, drop superseded ids,
fetch title and content for the pool, score with `score_pairs(segment, texts, model_name=
DEDUP_CROSS_ENCODER_MODEL, candidate_cap=RELATED_MEMORIES_POOL_PER_SEGMENT)` where each text is
`f"{title} {content}"`. A `None` result from `score_pairs` means no signal: that segment contributes
nothing. A failing semantic search is treated as an empty semantic list (log a warning); a failing
FTS search as an empty FTS list. Merge by id (best score; keep the best segment index, `fts_rank`,
`semantic_distance` of that best segment). `with_all=True` skips the `min_score` and `limit` filters
(used by `--json`). Returns dicts with the D7 keys. No exception escapes for ordinary retrieval
failures; programmer errors propagate.

### 3.2 `reranker_service.py`, `score_pairs`

Add keyword `model_name: str | None = None` after `text_cap_chars`. When it is not None: skip the
`is_cross_encoder_enabled()` gate and use it in place of `get_reranker_model_name()` (including the
`model` field of the diagnostics). When None, behaviour is exactly as today. Update the docstring.

### 3.3 `search_primitives.py` and `duplicates.py`

Move the term preparation now written inline in `check_duplicate_memories` (sanitize with
`sanitize_fts_query`, split, drop stop words and repeated terms case-insensitively, fall back to the raw
terms when everything is dropped, cap at a given count) into `search_primitives.build_fts_terms(text,
max_terms) -> list[str]`. `check_duplicate_memories` calls it with `DEDUP_FTS_MAX_TERMS`; its behaviour
and every existing dedup test stay unchanged. `find_related_memories` uses it with the same constant.

### 3.4 `config.py`

The constants of D10, placed near the dedup constants.

### 3.5 `protocol.py`, `dispatch.py`

Add `"find_related_memories"` to `READ_TOOLS` with a comment like its neighbours. In `dispatch.py` add
`_dispatch_find_related_memories(**kw)` (opens the connection like `_dispatch_get_last_session_digest`;
kwargs `text`, `limit`, `min_score`, `exclude_ids`, `agent_id`, `with_all`) and its `DISPATCH_TABLE`
entry. Any existing test that enumerates the read tools or the table must be adjusted in the same
change.

### 3.6 `client.py`

Add class `DaemonNotRunning(Exception)`. Add keyword `spawn: bool = True` to `call` and `call_method`
(pass it through); the behaviour for `spawn=True` is unchanged. For `spawn=False`: `info =
reachable_daemon_info(db_path)`; `None` raises `DaemonNotRunning`; the connect-failure retry raises
`DaemonNotRunning` instead of recursing; the not-ok retry for `DAEMON_SHUTTING_DOWN` raises
`DaemonNotRunning`, and for `AUTH_FAILED`/`CALLER_SESSION_INVALID` raises the usual `DaemonRpcError`
without recursing. Both recursive calls pass `spawn` through when it is True, unchanged. Update the `call` docstring.

### 3.7 `cli.py`

New `cmd_related_memories(args)` and its subparser, following `cmd_session_digest` (db path resolution,
`os.path.exists` check giving empty output). Reads stdin up to 100000 characters; reads
`SALTMDB_AGENT_ID` (stripped; empty means None) and passes it as `agent_id`; calls
`daemon_client.call(db_path, "find_related_memories", kwargs, spawn=False)` in a daemon thread per D9;
prints per D7. Reads `SALTMDB_AGENT_ID` raw (no `SALTMDB_OWNER_ID` fallback and no
`config.get_agent_id`, which raises on empty; this is by design). In `cli.main`, add
`"related-memories"` to the tuple of commands whose exceptions are swallowed to exit code 0 and update
the comment above it. The timeout path (D9) writes its stderr line, flushes stdout and stderr, and
calls `os._exit(0)`; the worker thread never prints (it stores its result or exception for the main
thread).

### 3.8 `docs/architecture.md`

One sentence appended to the first bullet of the daemon section that begins "Every MCP client and
most CLI entrypoints" (not the session-digest bullet, which BL-024 edits): `related-memories` reads text on stdin and
prints up to N related memories; read-only; never starts a daemon; shared scope unless an agent id is
set. No claim that any hook uses it.

## 4. Tests (failing first; literal expected values)

`tests/test_related_memory_service.py` (a temp DB; FTS runs for real; patch the cross-encoder with a fake keyed by
(segment text, candidate title); patch `semantic_search` with a fake that records the
`where_clauses`/`params` it received and returns a fixed list):
- T1. `split_segments`: a text with a fenced code block, a full UUID, a URL-only paragraph, a
  10-character paragraph and six ordinary paragraphs P1..P6 of 80, 300, 90, 120, 200 and 100
  characters returns exactly [P2, P4, P5, P6] (the last two, P5 and P6, plus the two longest of the
  rest, P2 and P4, in original order); the UUID and the code block text appear in none of them. The
  code block, the UUID, the URL-only text and the 10-character text are each their own blank-line
  separated paragraph, so lengths are not changed by the removals. The test writes the paragraphs as
  literals.
- T2. A segment longer than 300 characters is cut to 300.
- T3. Visibility: a private memory of another agent never appears from FTS (no `agent_id`: only
  shared; with `agent_id="a"`: shared plus a's own); and the semantic fake received the clause
  `e.scope = 'shared'` (no `agent_id`) or `(e.agent_id = ? OR e.scope = 'shared')` with the agent id
  among the bound params (with `agent_id`).
- T4. Archived memories and ids in `exclude_ids` never appear from FTS; the semantic fake received
  `e.status != 'archived'` and `e.id NOT IN (...)` with the excluded ids among the bound params; the
  semantic request size equals `min(10 + len(exclude_ids), 50)`.
- T5. A memory that is the target of a currently valid `supersedes` edge (a relation row inserted in the
  test DB with valid bitemporal columns) is dropped.
- T6. Best score over segments: the fake scores the same candidate 2.0 for segment 0's text and 6.0 for
  segment 1's text; the result has score 6.0 and `segment` 1.
- T7. `min_score` and `limit`: scores 7.0, 5.0, 4.5, 3.0 with `min_score=4.0`, `limit=2` return the
  first two; `with_all=True` returns all four sorted.
- T8. `score_pairs` returning `None`: result is `[]`, no exception.
- T9. Failing semantic search: FTS-only candidates still scored.
- T10. `score_pairs(model_name=...)` with `SALTMDB_RERANKER_MODEL` unset reaches the model (patched
  `get_model`); without `model_name` it still returns `None` (existing behaviour).
- T11. (existing tests, unchanged) the dedup tests pass after the term-helper extraction; name the
  files in the completion report.

`tests/test_cli_related_memories.py`:
- T12. Text mode prints `id<TAB>title` lines; a title containing a tab and a newline is flattened and
  cut to 150 characters.
- T13. `--json` prints the D7 key set for every candidate.
- T14. No daemon running (patched `reachable_daemon_info` returning None): empty stdout, a stderr line,
  exit code 0, and `ensure_daemon_running` is never called.
- T15. The call exceeding `--timeout-ms` (patched call that sleeps 2 s, with `--timeout-ms 200`):
  empty stdout, exit code 0, within the timeout plus 1 second (the test runs the CLI in a subprocess,
  because the timeout path calls `os._exit`).
- T16. Missing DB file: empty stdout, exit code 0.
- T17. (existing tests, unchanged) the existing client tests pass with the default `spawn=True`.
- T18. `tests/test_daemon_client.py`: with `spawn=False` and a refused connection, `call` raises
  `DaemonNotRunning` and `ensure_daemon_running` is never called.
- T19. `tests/test_daemon_client.py`: with `spawn=False` and a response with code
  `DAEMON_SHUTTING_DOWN`, `call` raises `DaemonNotRunning` and `ensure_daemon_running` is never called.

## 5. Documentation

Section 3.8 only.

## 6. Verification procedure

`./verify` only. Do not start a second full run in parallel (shared host). The cross-encoder model is
patched in every test; no test downloads or loads the real model.

## 7. Out of scope

The replay harness and the snapshot subcommand (separate specs); the threshold calibration; the stop
hook, the seen-ids state and the loop guard; the `search_memory` diagnostics fixes (BL-026); moving dedup
onto the new `score_pairs` override; any hook wiring or `~/.agents` change; the single-slot model cache in `reranker_service` (when
`SALTMDB_RERANKER_MODEL` names a different model than the dedup model, search and this command would
reload the model alternately; add a `# shortcut:` comment at the `model_name` override saying so).

## 8. Acceptance

Run in order:
1. `./verify` before editing: exit 0, counts recorded.
2. `.venv/bin/python -m pytest tests/test_related_memory_service.py tests/test_cli_related_memories.py -q`:
   T1-T10, T12-T16 fail before the change and pass after (T11 and T17 are existing tests that pass
   both before and after); `.venv/bin/python -m pytest tests/test_daemon_client.py -q` for T18-T19.
3. `.venv/bin/python -m pytest tests -q -k "dedup or duplicate"`: passes unchanged before and after.
4. `rg -n "ensure_daemon_running" src/saltmdb/cli.py` finds nothing (the CLI path never starts a daemon).
5. `./verify` after editing: exit 0, counts recorded.
6. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

Done (architect, 2026-10-10):
- Baseline probe: `pytest tests -k "dedup or duplicate or daemon_client or cli"` gives 146 passed.
- Tests that enumerate `READ_TOOLS`/`DISPATCH_TABLE` are membership-only (`test_phase3_mcp_surface.py`,
  `test_phase4_mcp_surface.py`, `test_mcp_tools.py`, `test_corpus_snapshot_mcp.py`); no MCP tool is added,
  so none needs adjusting.
- Consultant pre-lock review dispositions: B1 (exit-code tuple in `cli.main`), B2 (second recursive
  retry), B3 (semantic fake records the clauses), B4 (fake keyed by segment text and title), B5 (T1
  paragraph layout and URL definition), B6 (acceptance step 2 excludes existing tests), B7
  (`tests/test_daemon_client.py` in scope with T18-T19), A1/A2 (semantic request size), A3 (shortcut
  note), A4 (architecture.md target), A5 (raw agent id) all applied above.
- Not checked and left to the developer to confirm: that a daemon serving a `--db-path` copy resolves
  `get_db_path()` to that copy inside dispatch (the session-digest dispatch relies on the same);
  thread safety of the model under concurrent calls.
- Expected cost: about 40 cross-encoder pairs per call (4 segments x 10), roughly 2-3 s warm by the
  BL-018/BL-022 measurements; the replay harness measures the real number.
