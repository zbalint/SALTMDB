# SPEC-TAINT-HOOK-SWEEP-STDIN

## 0. Status

LOCKED 2026-10-21 (consultant review folded in).

- Backlog item: none. Sonar `pythonsecurity:S8701` ("Command Injection via faulty LLM-supplied standard input") at `hooks/_saltmdb_hook_common.py` `run_quiet` (`subprocess.run`, about lines 473-478) on `develop` `782b410` or later. Source: `transcript_path` from the hook's stdin JSON in `hooks/saltmdb-pre-compact-sweep.py`, which appends it to the prompt and passes it as an argv element to `codex exec` / `claude -p`. Shared `context_id`: `saltmdb-sonar-fix-2026-10`. Owner decisions: real hardening, no suppression, no Accept/Safe where code can fix; a finding that stays flagged after a correct fix is reported to the owner.
- Baseline: `develop` at `e17fc4c` or later; `./verify` exits 0 (known flaky: `tests/test_daemon_server_main.py`, `tests/test_adapter_signal_shutdown.py`, `tests/test_daemon_server.py::TestDaemonSignalShutdown` (election-port collision), `tests/test_relation_service.py` temp-dir race; rerun a failing one alone once).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `hooks/saltmdb-pre-compact-sweep.py` loaded with `importlib.util.spec_from_file_location` after inserting the hooks directory into `sys.path` (the sweep imports `_saltmdb_hook_common` bare and has no `sys.path` insert of its own; idiom of `hooks/tests/test_skill_review_sweep.py` lines 10-12), with `shutil.which` patched and `run_quiet` and `read_transcript` patched as attributes of the LOADED SWEEP MODULE (it imports them by name); `run_quiet` itself with `subprocess.run` patched.
- Scope (may edit): `hooks/saltmdb-pre-compact-sweep.py` (including its module docstring), `hooks/_saltmdb_hook_common.py` (`run_quiet` only), the NEW file `hooks/tests/test_pre_compact_sweep.py`, `hooks/README.md` (the two spots that describe this script, about lines 24-29 and 200).
- Does not touch: the other two `run_quiet` callers (`hooks/saltmdb-checkable-fact-drift-sweep.py`, `hooks/saltmdb-skill-review-sweep.py`), `read_transcript` and the readers, any existing test, the hook registration examples, any live `claude` or `codex` invocation.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The sweep script puts a path taken from hook input into the argv of a headless agent. Three designs were weighed with the consultant: (A) pass the transcript CONTENT on stdin, bounded; (B) the validated path on stdin; (C) the path in an environment variable. Decision: (A). The argv of the subprocess becomes fully constant (`[*command, SWEEP_PROMPT]`); no path reaches the child at all, so the child needs no permission to read outside its working directory (B and C both need the child to read `~/.claude/projects/...` or to run a shell to learn the path, and B splits the check from the use across two processes, the S5144 lesson). The path is only used inside the hook through `read_transcript` (slice 1), the single validated reader. A bounded tail loses nothing real: content older than the last compaction is already in the session's summary, and no child could take a 47 MB JSONL into its context within the 60 s timeout anyway. Rejected: (B) and (C) as above; restricting the child's tools (`--allowedTools`, Codex equivalents) in this slice, because the Codex flags are unverified (`claude --help` does list `--allowedTools`) and a wrong flag silently breaks the sweep (revisit). Context for the owner: the shipped examples wire Claude's native PreCompact as an agent-type hook that does not use this script, and nothing runs `main()` in the tests, so this script is a fallback entry that may never have run; the hardening keeps its behaviour and the owner may also choose to retire it.

## 2. Decisions

- D1. `run_quiet(cmd, timeout_secs, input_text=None)`: when `input_text` is not `None`, pass `input=input_text.encode("utf-8")` to `subprocess.run`; when it is `None` pass `input=None`, which leaves stdin inherited exactly as today (NOT `stdin=DEVNULL`). Keep `capture_output=True` and bytes (no `text=True`). Everything else unchanged. The other two callers pass `(cmd, timeout)` positionally and are unaffected.
- D2. `saltmdb-pre-compact-sweep.py`: `SWEEP_PROMPT` says the conversation (JSON lines) is supplied on standard input instead of "at the path given below"; the prompt is a constant string and no path is appended. `main()`: read `transcript_path` as today; none: `sys.exit(0)` as today; `text = read_transcript(transcript_path)`; `None` (denied or unreadable): `sys.exit(0)`, no subprocess call. Then `payload = _sweep_input(text)`; if `not payload.strip()` (a 0-byte transcript, or a transcript whose last line is the marker): `sys.exit(0)` with no subprocess call. Then for each command (same order logic with `SALTMDB_HOOK_PREFERRED_AGENT`) `run_quiet([*command, SWEEP_PROMPT], TIMEOUT_SECS, payload)`. The `cmd` list must equal exactly `[*command, SWEEP_PROMPT]` and the transcript path string must appear nowhere in `cmd` or in the input.
- D3. `_sweep_input(text: str) -> str`: (1) boundary: scan the lines for the LAST compaction boundary; a module-level tuple holds entries `(substring, type, subtype)`, here `('"subtype":"compact_boundary"', "system", "compact_boundary")` (Claude Code, verified in a real transcript: the quoted form is immune to quoted text, because a string-quoted copy is serialised with escaped quotes); a line counts only when the substring is present AND `json.loads(line)` is a dict with `obj.get("type") == type` and `obj.get("subtype") == subtype` (a `ValueError` or non-dict skips the line); keep only the text after the last such line. (2) cap: keep at most `_SWEEP_MAX_BYTES` bytes: take `text[-_SWEEP_MAX_BYTES:]` first (characters never exceed bytes, so this still holds at least the cap), encode it as UTF-8, keep the last `_SWEEP_MAX_BYTES` bytes, then cut on a line boundary: drop everything up to and including the first newline of the kept slice, but only when the byte just before the cut is not a newline (a cut exactly at a line start keeps that complete line), and look for that newline only in the slice WITHOUT its final character when the slice ends with a newline (otherwise a single long last line leaves an empty result); when no such newline exists keep the slice as is and decode with `errors="ignore"` so a split multi-byte character is dropped. The result may be empty; D2 handles that. `_SWEEP_MAX_BYTES` defaults to `262144`; the environment variable `SALTMDB_PRECOMPACT_MAX_BYTES` is read at CALL time (inside `main` or `_sweep_input`, so a test can patch it after import), parsed as an integer and ignored (default used) unless it is between 1024 and 4194304 INCLUSIVE. One named constant each for the default, the two bounds and the env name (no repeated literals). Codex has no verified marker: its transcripts rely on the byte cap alone.
- D4. The module docstring and the two README spots say exactly what the script is: a command entry that a harness may invoke as a PreCompact command hook, which sweeps the bounded tail of the transcript named in the hook payload, supplied on stdin; a manual or cron run carries no hook stdin and so no `transcript_path`, and the script then does nothing. Remove the docstring's claim of "Claude Code and Codex" registration and the README's "manual/cron fallback" wording where they contradict this (the README is otherwise authoritative: PreCompact is registered natively only in the Claude example, as an agent hook that does not use this script).
- D5. Complexity and literals: no function above cognitive complexity 15, none of baseline 12 or more raised; no literal repeated 3+ times. Report scores before/after for every function touched. No suppressions or `NOSONAR`. The developer must NOT run `claude` or `codex` (quota and side effects); behaviour with real CLIs is covered by the facts in section 1 (checked in `--help`: `codex exec` appends piped stdin as a `<stdin>` block when a prompt argument is also given; `claude -p` with piped stdin is documented usage) and is reported as not exercised live.

## 3. Changes per file

| File | Change |
|---|---|
| `hooks/_saltmdb_hook_common.py` | `run_quiet` gains `input_text` (D1) |
| `hooks/saltmdb-pre-compact-sweep.py` | constant prompt, `read_transcript`, `_sweep_input`, new `run_quiet` call (D2, D3, D4) |
| `hooks/tests/test_pre_compact_sweep.py` | new (section 4) |
| `hooks/README.md` | one sentence (D4) |

## 4. Tests

Written failing first; literal expected values. With `shutil.which` patched to find both CLIs and `run_quiet` patched to record `(cmd, timeout, input_text)`:

- `cmd == [*command, SWEEP_PROMPT]` exactly, for the default order (`codex exec` first) and with `SALTMDB_HOOK_PREFERRED_AGENT=claude` (`claude -p` first); the transcript path string appears in neither `cmd` nor the input.
- Edge cases that must not run the child on nothing: a 0-byte transcript, a transcript whose last line is the marker line, and a transcript whose single last line is longer than the cap and ends with a newline each produce NO subprocess call (or, for the long last line, a non-empty cut line) and exit 0.
- Marker robustness: a line that contains the exact marker substring inside a nested object but is not `type=system`/`subtype=compact_boundary` does not cut; with at least TWO real marker lines only the text after the LAST one is kept.
- Bytes, not characters: a multi-byte-heavy fixture whose character count is at most the cap and whose byte count is above it yields `len(input.encode("utf-8")) <= cap`; the fixture sizes are computed in the test so the cut falls mid-line deterministically; a cut exactly at a line start keeps that complete line.
- Env bounds are inclusive: `1024` and `4194304` are accepted, `1023` and `4194305` rejected (default used); the variable is read at call time (patch it after the module is loaded).
- Input content: a transcript larger than the cap yields input of at most `_SWEEP_MAX_BYTES` bytes that ends with the transcript's last line and starts at a line boundary; a transcript containing a `compact_boundary` line yields only the text after the LAST one (with a smaller transcript than the cap); a small transcript without a marker is passed whole; a multi-byte character straddling the cut does not raise.
- The env override: a valid value changes the cap; `0`, `abc`, a negative number and a value above 4194304 fall back to the default.
- No transcript path in the payload: no subprocess call, exit 0. A path outside every root or a missing file (HOME isolated, `read_transcript` denies): no subprocess call, exit 0, and the denial trace is printed once (reset the once-per-process flag per test, idiom of `hooks/tests/test_transcript_reader.py`).
- When the first CLI fails (`run_quiet` returns false) the second is tried; when neither exists nothing is called and exit 0 (today's behaviour).
- `run_quiet` with `subprocess.run` patched: `input_text="x"` passes `input=b"x"`; `None` passes `input=None` and the keyword arguments contain no `stdin`; the return value is `returncode == 0`; `OSError` and `TimeoutExpired` give `False`.
- Mutation sample (tester, scratch copy): put the path back into the prompt or argv; drop the cap; ignore the boundary marker; drop `input_text`; keep the first partial line; accept a denied transcript.

## 5. Documentation

D4 only.

## 6. Verification procedure

`./verify`; the new test file alone; `ruff check --select ARG,F,RET` on both scripts against the baseline; complexipy (scratch venv) on both. Manual evidence without a real CLI: put fake `codex` and `claude` executables in the session scratchpad (never in the repo) and prepend that directory to `PATH`; each fake records its argv to a scratch file and reads ALL of stdin with `wc -c` (never printing content). Keep the real `HOME` so `read_transcript` accepts a real current Claude transcript named in the hook payload, and run the script by absolute path (it relies on its own directory being `sys.path[0]`). Three runs: (1) default order: the recorded argv equals `exec <SWEEP_PROMPT>` and a count-only grep shows the transcript path occurs 0 times in the recorded argv; the stdin byte count is at most the cap; (2) fake `codex` exits 1 and the fake `claude` is then called (fallback); (3) `SALTMDB_HOOK_PREFERRED_AGENT=claude` calls `claude -p` first. The real `claude` and `codex` are never invoked.

## 7. Out of scope

Tool restriction flags for the child, retiring the script, the native PreCompact agent hook, the other two `run_quiet` callers, any Gemini/Antigravity behaviour.

## 8. Acceptance

1. `./verify` exits 0. 2. `git status` lists only the files of section 3. 3. The fake-CLI evidence of section 6. 4. Mutation sample of section 4. 5. Architect after CI and the Sonar scan: the S8701 issue closed or still open; if still open, report to the owner with the evidence (the argv is a constant, the data travels on stdin).
