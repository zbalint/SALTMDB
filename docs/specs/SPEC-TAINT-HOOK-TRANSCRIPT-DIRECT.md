# SPEC-TAINT-HOOK-TRANSCRIPT-DIRECT

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none. Sonar `pythonsecurity:S8707` ("Path Traversal via faulty LLM-supplied standard input") on `develop` `782b410` or later at: `hooks/saltmdb-post-tool-failure-circuit-breaker.py:70`, `hooks/saltmdb-post-tool-response-nudges.py:124`, `hooks/saltmdb-stop-critique-gate.py:138` (`find_last_user_line`) and `:277` (`_line_count`), `hooks/saltmdb-stop-trace-capture.py:245` (`open(transcript)`). Source: `transcript_path` / `transcriptPath` from the hook's stdin JSON. Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decisions: real hardening, no suppression, no Accept/Safe where code can fix; a finding that stays flagged after a correct fix is reported to the owner.
- Baseline: `develop` at `782b410` or later; `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_relation_service.py` temp-dir race).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the hook modules loaded the way the existing hook tests load them (`importlib.util.spec_from_file_location`, see `hooks/tests/test_stop_critique_gate.py`), and `saltmdb-stop-trace-capture.py` `run(payload, home, environ, ppid)`; transcripts live under an isolated `HOME` at `<home>/.claude/projects/...` (or `<home>/.copilot/session-state/...` for trace capture).
- Scope (may edit): the four hook scripts above; the new test file `hooks/tests/test_transcript_direct_sites.py`; and, SETUP / PATH-MOVE ONLY (adding a fixture parameter, an in-process `HOME`/`USERPROFILE`/`CLAUDE_CONFIG_DIR`/`CODEX_HOME` patch, or moving a transcript path under an allowed root; no assertion change, no deleted test) these existing hook tests, which the consultant's review found feed the scripts a transcript outside the allowed roots: `hooks/tests/test_stop_related_memories.py` (the in-process `find_last_user_line` tests at about lines 552, 565 and 588: patch `HOME` and clear the two config-dir vars in process; for 565 and 588 also move the file from `self.root` under `self.home/.claude/projects`), `hooks/tests/test_retrieval_outcome_flow.py` (about line 59: add a `tmp_path` parameter, copy `FIXTURE` to `<tmp>/.claude/projects/` and patch `HOME`; line 64's own direct read of `FIXTURE` stays), `hooks/tests/test_trace_capture_copilot.py` (class `TestRun`: in `setUp` set `HOME` and `USERPROFILE` to `self.home`, clear `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, and move `self.transcript` to `self.home/.copilot/session-state/cs-1/events.jsonl`; this also restores the meaning of the tests at about lines 256, 288 and 295, which would otherwise pass for the wrong reason). `hooks/tests/test_stop_trace_capture_discovery.py` and `hooks/tests/test_stop_critique_gate.py` need NO change. The developer reports every further test found in this class; a test that needs more than a setup or path move is a `BLOCKED`.
- Does not touch: `hooks/_saltmdb_hook_common.py` (the choke point), `run_quiet` and the sweep (slice 3), any assertion.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

After slice 1 the four common readers go through `read_transcript`, but five hook sites still open `transcript_path` themselves. Decision: they all call `read_transcript` (never `open` / `Path.read_text` on hook input), so one function validates every transcript read; a denied or unreadable path behaves exactly like today's missing file (the hook does nothing, or exits 0) and the single stderr trace of slice 1 makes a wrongly configured root visible. Rejected: duplicating the root check in each hook (violates the single choke point rule); passing `Path` objects down (`read_transcript` returns text on purpose, the validation and the open stay in one function).

## 2. Decisions

- D1. `saltmdb-post-tool-failure-circuit-breaker.py`: remove the early `not transcript_path or not Path(transcript_path).is_file()` return (it validated nothing) and, at the point where the transcript is read today (after the event and error-code checks), use `text = read_transcript(transcript_path)`; `None` returns (no nudge); then `lines = text.splitlines()[-200:]`. Behaviour for readable transcripts is unchanged.
- D2. `saltmdb-post-tool-response-nudges.py` `handle_store_memory`: replace the `is_file()` guard plus `read_text` by `text = read_transcript(transcript_path)`; `None` returns; `lines = text.splitlines()`. The rest unchanged.
- D3. `saltmdb-stop-critique-gate.py`: in `main` replace `not transcript_path or not Path(transcript_path).is_file()` by `if read_transcript(transcript_path) is None: sys.exit(0)` (one decision for the whole run; the later readers re-validate the same value and return empty values only if the file disappears mid-run, which equals today's behaviour); `find_last_user_line` iterates `read_transcript(transcript_path)` (return `None` when it is `None`); `_line_count` returns `len(read_transcript_full(transcript_path).splitlines())`. No other function of the file changes. The module keeps its imports unchanged (`Path` stays in use).
- D4. `saltmdb-stop-trace-capture.py` `run`: replace `with open(transcript, encoding="utf-8") as handle: interactions = parse_interactions(handle.read().splitlines())` by `text = read_transcript(transcript)`; if `None`, `raise RuntimeError("transcript path denied or unreadable")` so the existing broad `except Exception` logs it through `_log` (no path in the message); then `parse_interactions(text.splitlines())`. Import `read_transcript` next to the existing `read_stdin_json` import. The existing missing-transcript test asserts only the exit code and that no daemon call was made, so NO assertion changes anywhere in this spec. Behaviour change to accept: the file is now decoded with `errors="replace"` instead of strict UTF-8, so a non-UTF-8 events file parses instead of logging `UnicodeDecodeError`.
- D4b. Session id in the default path: `run()` builds `home/.copilot/session-state/<session_id>/events.jsonl` when the payload has no `transcriptPath`. Use that default only when `session_id` fully matches `[A-Za-z0-9._-]{1,128}` and is not `.` or `..`; otherwise raise the same `RuntimeError("transcript path denied or unreadable")` and log a line that does not contain the raw session id (the existing `_log` line embeds `session_id` unescaped, a pre-existing log-injection path: log `session <invalid>` for an invalid id). A valid id logs as today.
- D5. Complexity and literals: no function above cognitive complexity 15, no existing score raised (report before/after for the functions touched); the new message string is used once.
- D6. A rejection test per site in the new test file (section 4).

## 3. Changes per file

| File | Change |
|---|---|
| `hooks/saltmdb-post-tool-failure-circuit-breaker.py` | D1 |
| `hooks/saltmdb-post-tool-response-nudges.py` | D2 |
| `hooks/saltmdb-stop-critique-gate.py` | D3 |
| `hooks/saltmdb-stop-trace-capture.py` | D4 |
| `hooks/tests/test_transcript_direct_sites.py` | new (section 4) |
| existing hook tests named in section 0 | setup / path moves only |

## 4. Tests

Written failing first. For each of the four scripts, with `HOME` isolated: (a) a transcript under the allowed root behaves as today (existing tests prove it; add one positive case per script in the new file); (b) a transcript path OUTSIDE every root (a real file that holds a line the script would act on, for example a repeated `error_code` issue for the circuit breaker, a `store_memory` without `manage_relation` for the nudges, a user turn for the critique gate, a valid Copilot events file for trace capture) is NOT read: the circuit breaker and nudges emit no nudge, the critique gate exits 0 without output, trace capture logs the denial message and sends nothing (patch the daemon call and assert it was not called); (c) the stderr trace line appears once and does not contain the path; the once-per-process flag lives in the cached `_saltmdb_hook_common` module and is shared across the pytest process, so reset it per test with the idiom of `hooks/tests/test_transcript_reader.py` (about line 46) or run the hook as a subprocess. Every rejection test asserts exit code 0 / no exception AND empty stdout (otherwise "remove the `None` check" in the circuit breaker and nudges, which raises `AttributeError`, is caught only by accident); the trace-capture rejection test asserts the log contains `transcript path denied or unreadable` and the daemon was not called. The trace-capture positive and rejection cases use the `TestRun` setup (fake adapter and daemon, `HOME` set to the test home). Add trace-capture cases for D4b: an id with `../`, an absolute id, an id with a newline and an empty id are refused with no daemon call and no raw id in the log. Mutation sample (tester, scratch copy): restore the direct `open` / `read_text` at each of the five sites (each must fail its rejection test), remove the `None` check at each site, make `_line_count` read the file directly.

## 5. Documentation

None expected (`hooks/README.md` already documents the roots after slice 1).

## 6. Verification procedure

`./verify`; the new test file alone; `ruff check --select ARG,F,RET` on the four scripts against the baseline; complexipy (scratch venv) on the four scripts. Live-hook evidence before the commit (read-only, never print transcript contents): the real `saltmdb-post-tool-failure-circuit-breaker.py`, `saltmdb-post-tool-response-nudges.py` and `saltmdb-stop-critique-gate.py` with a synthetic payload whose `transcript_path` is a real current Claude session file: normal decision, no denial line; the same with a path outside every root: no output, one denial line. Do NOT run trace capture live (it talks to the daemon); its tests are the evidence.

## 7. Out of scope

Cost, named: the critique gate's `main` now reads the whole transcript one more time per Stop hook, on top of the several full reads it already does (each capped at 256 MiB); reading once per hook is a follow-up. `run_quiet` and the sweep prompt (slice 3), the shared reader, reading the transcript once per hook instead of per call, the Gemini/Antigravity hooks.

## 8. Acceptance

1. `./verify` exits 0. 2. `git status` lists only files named in sections 0 and 3. 3. Live-hook evidence of section 6. 4. Mutation sample of section 4. 5. Architect after CI and the Sonar scan: the five S8707 lines closed or still open; if still open, report to the owner with the evidence (every site now calls `read_transcript`; the residual flow is the choke point itself).
