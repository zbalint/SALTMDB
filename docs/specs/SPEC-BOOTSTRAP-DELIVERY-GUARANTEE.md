# SPEC-BOOTSTRAP-DELIVERY-GUARANTEE

## 0. Status

LOCKED 2026-10-11.

- Backlog item: BL-043. Shared `context_id`: `bootstrap-delivery-2026-10-11`.
- Baseline: `a3daa33` on `develop`. `./verify` at the previous task: 2212 passed, 1 skipped,
  177 subtests. `.venv/bin/python -m pytest hooks/tests -q`: 80 passed, 12 subtests (measured at
  lock time, 2026-10-11).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: the hook scripts' `main()` and `bootstrap_text()` loaded with the existing
  `_load_module` helper; the helpers in `hooks/_saltmdb_hook_common.py`; the example settings JSON;
  an end-to-end run of each script as a subprocess against a fake CLI and a temporary `HOME`.
- Scope (may edit):
  - `hooks/_saltmdb_hook_common.py`
  - `hooks/saltmdb-session-start-bootstrap.py`
  - `hooks/saltmdb-prompt-submit-digest-retry.py` (new, mode 0755 like its siblings)
  - `hooks/claude-settings-example.json`
  - `hooks/README.md`
  - `hooks/tests/test_session_start_bootstrap.py`
  - `hooks/tests/test_digest_retry.py` (new)
  - `hooks/tests/test_capture_hook_config.py`
- Does not touch: everything under `src/`, `docs/BACKLOG.md`, `docs/architecture.md`,
  `hooks/codex-settings-example.json`, `hooks/antigravity-settings-example.json`,
  `hooks/copilot-hooks-example.json`, every other hook script, and anything under `~/.agents` or
  `~/.claude` (the architect syncs and registers after acceptance).
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The bootstrap digest carries session continuity, and the owner wants it delivered every time, not
most of the time. It was lost again on 2026-10-11 after a WSL VM restart, even though BL-042 had
already made the daemon's own startup fast (`total_s=3.1`).

Evidence (daemon log, Claude transcript, hook source): the SessionStart hook ran for 15,015 ms and
was killed by the harness (`timedOut`, limit 15 s). The daemon was ready at about the same second.
A killed hook prints nothing, so even the existing `<saltmdb-digest-pending>` notice never reached
the agent. Spawn-to-ready was about 15 s on that cold start, made of a `startup_lag_s` of 6.2 s
before the daemon's first log line, 3.1 s of daemon phases and the CLI's own cold start. The lag
before the first log line is not explained by code (importing the daemon module takes 0.2 s warm,
BL-035 measured 1.8-2.2 s cold), so the cold-start time cannot be relied on to shrink below the
harness limit. Delivery must therefore not depend on it.

The hook's `BUDGET_S = 13.0` also counts from the script's own first line, so the interpreter start
and harness latency on a cold VM eat the 2 s margin to the 15 s kill.

A second, independent loss: Claude Code caps a hook's plain stdout at 10,000 characters and replaces
anything longer with a `<persisted-output>` stub holding a preview of the first 2,000 characters and a
file path (documented limit; confirmed in this machine's transcripts, where every SessionStart output
over about 10,000 characters was delivered as a stub of 1,450-2,200 characters). The current
`session-digest` is 24.8 KB, and the handover alone is allowed 40,000 characters, so a delivered
digest is routinely cut to a preview. A guarantee of delivery has to cover this too.

Decision (owner, 2026-10-11): make delivery independent of timing with two layers, and leave a
persistent daemon at boot (BL-013 service) out of scope:

1. The SessionStart hook never dies silently. It records a pending marker before any slow work,
   runs under a shorter deadline, and removes the marker only after the output is written.
2. A new `UserPromptSubmit` hook finds the marker on the first prompt, by which time the daemon is
   ready, fetches the missing parts itself and injects them. The harness does the delivery; the
   agent does not have to notice and obey a notice.

Rejected: a longer budget (the harness kill is fixed at 15 s); killing nothing and leaving the CLI
running to write its output to a file (the CLI gives up after about 10 s of waiting for the daemon,
so a cold daemon would still produce nothing); relying on the pending notice alone (it depends on
the agent obeying it, and it is lost whenever the hook is killed).

## 2. Decisions

- D1. `BUDGET_S` in the bootstrap hook becomes 10.0 (was 13.0). The remaining 5 s covers the
  interpreter start, harness latency and process exit before the 15 s kill. The 10 s per-call cap
  and the 1.0 s minimum per call stay.
- D2. The pending marker is a JSON file `digest-pending-<session_id>.json` in `state_dir()` with
  `{"missing": [<cli command names>], "attempts": <int>, "running_since": <epoch seconds or null>}`. The path formula lives in one function in
  `_saltmdb_hook_common.py`. Marker writes and removals are best effort and never raise (same
  pattern as `write_count` and `clear_state`).
- D3. The tracked parts are the CLI commands `bootstrap-digest` and `session-digest`.
  `session-digest` is tracked only when `source != "compact"` (matching the existing skip).
  `corpus-health` is not tracked.
- D4. SessionStart flow (at-least-once delivery), with a non-empty session id and a resolved CLI:
  1. `expected` = the tracked parts of D3. `pending_before` = the union of `expected` and the
     `missing` list of an existing marker (read with `read_digest_pending`), in the order
     `bootstrap-digest`, `session-digest`. Write the marker with `missing = pending_before`,
     `attempts = 0` and `running_since = time.time()`, before computing the text.
  2. Compute the text (D6 notice included) and write it to stdout, then flush.
  3. `fetched` = the `expected` parts that returned non-empty output. Re-read the marker and the
     `.claimed` file (D8) as they are now, take the union of `pending_before` and their `missing`
     lists in the order of step 1, and rewrite the marker with
     `missing = [p for p in that union if p not in fetched]`,
     `attempts = 0`, `running_since = null`; remove the marker instead when that list is empty. The
     re-read keeps a retry hook's failure write (a retry running past the grace of D7) from being
     overwritten; the remaining window is a few milliseconds and can at worst duplicate a part,
     never lose one (leave a `# shortcut:` comment there).
  A hook killed anywhere leaves the marker of step 1, with `running_since` set. A kill between the
  flush and step 3 can duplicate a digest on the first prompt, never lose one. A `compact` run never
  drops an undelivered `session-digest` recorded by an earlier run of the same session. Each
  SessionStart run resets `attempts` to 0 (intended: a compact restarts the retry budget, at the cost
  of one bounded call per prompt); a part dropped after `MAX_RETRY_ATTEMPTS` is not tracked again.
- D5. No marker is written, and no pruning is done, when the session id is empty or the CLI did not
  resolve. On Codex, Antigravity and Copilot nothing consumes the marker (Antigravity's
  `conversationId` also counts as a session id); the file is a few bytes and the daily prune
  (D10) removes it. That is intended, not a defect.
- D6. The pending notice keeps its `<saltmdb-digest-pending>` tags. Its second line
  (`Run {CLI} bootstrap-digest with Bash now and read the result before continuing.`) is replaced by
  a line that says: if no `<saltmdb-digest>` block has appeared by your first reply, run
  `{CLI} bootstrap-digest` with Bash and read the result before continuing. The wording must hold
  on harnesses that have no retry hook (it must not promise delivery). The substring
  `{CLI} bootstrap-digest` stays in the text.
- D7. The retry hook is a `UserPromptSubmit` command hook, plain stdout as context, with its own
  `START` and `BUDGET_S = 12.0`. It reads the payload with `read_stdin_json` and `get_session_id`.
  Hot path: no session id, or neither a marker nor a stale claimed file (D8), means return at once
  with no CLI resolution, no subprocess and no output (`state_dir()` still creates its directory on
  each call; that is accepted). A marker whose `running_since` is set and younger than
  `RUNNING_GRACE_S = 12` seconds (the SessionStart `BUDGET_S` of D1 plus 2 s) belongs to a
  SessionStart hook that is still running: leave it untouched and return, so the next prompt
  retries. The value must stay below the 15 s harness kill: a hook killed at 15 s leaves a marker
  older than 12 s, which the retry hook then processes at once. A hook that is still alive past 12 s
  (it should not be) can at worst cause a duplicate delivery, never a loss.
- D8. Claim before work: the retry hook renames the marker to `<name>.claimed` with `os.replace`
  and returns quietly when the rename raises `OSError` (the losing process of two concurrent
  prompts), so only one delivers. Right after a successful claim it calls `os.utime` on the claimed
  file, because `os.replace` keeps the marker's old mtime and the claim time is what the stale check
  reads. A claimed file older than `CLAIM_STALE_S = 30` seconds (by mtime) with no marker belongs to a
  retry hook that was killed: rename it back to the marker path and claim it as usual (an `OSError`
  on either rename returns quietly; leave a `# shortcut:` comment that the reclaim rename can
  overwrite a marker a compact run wrote in the same few milliseconds). It then runs the missing parts in order through the shared bounded runner and
  stops at the first part that returns empty (the later parts stay in the marker without being
  tried, so a down daemon costs one bounded call, not two). It prints what it got joined by a blank
  line, passed through D12, and removes the claimed file. The parts not fetched go back into a
  fresh marker with `attempts + 1` and `running_since = null`, unless `attempts + 1` reaches
  `MAX_RETRY_ATTEMPTS = 3`, in which case nothing is written. If the CLI does not resolve, the claim
  is released (renamed back) and nothing is printed.
- D9. One shared bounded runner `run_cli_bounded(cli, args, remaining_s)` in
  `_saltmdb_hook_common.py` holds the subprocess logic that both scripts need (cap 10 s, minimum
  1.0 s, `None` on a non-zero exit, `OSError` or timeout). The bootstrap script's `run_cli` calls
  it; the retry script calls it directly.
- D10. Stale markers (`digest-pending-*`, which also matches `.json.claimed`) older than one day are
  pruned by the SessionStart hook with the existing `prune_stale_state`.
- D11. Registration in this repo: the Claude example gets one more `UserPromptSubmit` group
  (matcher `*`, one `command` hook, timeout 15). The existing capture group is not edited. The
  Codex, Antigravity, Copilot and OMP registrations and the live configs are out of scope.
- D12. Output cap and section order. `HOOK_OUTPUT_MAX_CHARS = 9800` (the bound for the whole output, notice included) in `_saltmdb_hook_common.py` and
  a shared function `cap_hook_output(text: str, cli: str | None) -> str` that returns `text`
  unchanged when `len(text) <= HOOK_OUTPUT_MAX_CHARS`, otherwise the first
  `HOOK_OUTPUT_MAX_CHARS - len(notice)` characters followed by one notice line, so the result is never
  longer than `HOOK_OUTPUT_MAX_CHARS` whatever the length of the CLI path. The notice says the output
  was cut to fit the harness's 10,000-character limit and that the full text is available with
  `{cli or "saltmdb-cli"} bootstrap-digest` and `{cli or "saltmdb-cli"} session-digest`.
  The bootstrap hook's section order becomes: core digest (or the pending notice), the overdue-core
  notice, then the session digest, so the cut falls in the long handover and never in the notice
  that warns about the `store_memory` hard block (the CLI calls keep their current order and budget
  priority; only the order of the joined sections changes). The retry hook joins `bootstrap-digest`
  then `session-digest`, in that order. Both scripts apply the cap to their plain-stdout output; the
  Antigravity `injectSteps` branch is not capped (no documented limit).

## 3. Changes per file

### 3.1 `hooks/_saltmdb_hook_common.py`

Add after `search_memory_called_flag_path` (L226-242):

- `digest_pending_path(session_id: str) -> Path`: `state_dir() / f"digest-pending-{session_id}.json"`,
  with a docstring that names the two scripts that use it. This is the only place the filename
  formula appears.
- `read_digest_pending(path: Path) -> dict | None` and `write_digest_pending(path: Path, missing:
  list[str], attempts: int = 0, running_since: float | None = None) -> None`: JSON read (returns
  `None` on `OSError` or `json.JSONDecodeError`, or when `missing` is not a list of strings) and
  best-effort write of the three keys of D2 (catch `OSError` only).
- `run_cli_bounded(cli: str | None, args: tuple[str, ...], remaining_s: float) -> str | None`:
  returns `None` when `cli` is falsy; `limit = min(10, remaining_s)`; returns `None` when
  `limit < 1.0`; `subprocess.run([cli, *args], capture_output=True, text=True, timeout=limit,
  check=False)`; `None` on `OSError` or `subprocess.TimeoutExpired`; stdout when the return code is
  0, else `None`. The module already imports `subprocess`.
- `HOOK_OUTPUT_MAX_CHARS = 9800` and `cap_hook_output(text, cli)` as in D12.

Reuse `state_dir`, `clear_state`, `prune_stale_state`, `get_session_id`, `read_stdin_json`.

### 3.2 `hooks/saltmdb-session-start-bootstrap.py`

- `BUDGET_S = 10.0` (the constant after `START`).
- The `from _saltmdb_hook_common import resolve_cli` line: also import `digest_pending_path`,
  `read_digest_pending`, `write_digest_pending`, `run_cli_bounded`, `cap_hook_output`,
  `get_session_id`, `clear_state`, `prune_stale_state`.
- `run_cli` becomes: `if not CLI: return None` then `return run_cli_bounded(CLI, args, remaining())`.
  `remaining()` is called exactly once per call, and only after the `CLI` check (the existing tests
  count those calls).
- `bootstrap_text(source=None)` gains a second parameter `missing: list[str] | None = None`. When
  given, the names of the tracked parts that were tried and returned empty (`bootstrap-digest` and,
  unless `source == "compact"`, `session-digest`) are appended in that order. The return value and
  the existing call shape are unchanged.
- The pending notice text changes as in D6. In `bootstrap_text` the overdue-core notice section is
  placed before the session-digest section (D12); the order of the CLI calls does not change.
- `main()` follows D4 and D12: `session_id = get_session_id(payload)`; only when `session_id and
  CLI`: prune with `prune_stale_state("digest-pending-*", max_age_days=1)`, compute `pending_before`
  and write the first marker; pass `missing: list[str] = []` to `bootstrap_text`; the plain-stdout
  branch writes `cap_hook_output(text, CLI)`; after the existing output (both the `injectSteps` and
  plain stdout branches) call `sys.stdout.flush()`; then rewrite or clear the marker per D4 step 3.
  Output for the empty `text` case is unchanged; the marker step still runs.

### 3.3 `hooks/saltmdb-prompt-submit-digest-retry.py` (new)

Shebang `#!/usr/bin/env python3`, module docstring in the style of the bootstrap script (lifecycle
event: `UserPromptSubmit`), `START`, `BUDGET_S = 12.0`, `MAX_RETRY_ATTEMPTS = 3`,
`RUNNING_GRACE_S = 12`, `CLAIM_STALE_S = 30`, `sys.path.insert(0, ...)` and imports from
`_saltmdb_hook_common` as in the bootstrap script. `CLI = resolve_cli()` is called inside `main()`
only after the marker was claimed. `main()` implements D7, D8 and D12. Output is plain stdout (a
trailing newline). `if __name__ == "__main__": main()`.

### 3.4 `hooks/claude-settings-example.json`

Add, after the existing `UserPromptSubmit` group, a second group:
`{"matcher": "*", "hooks": [{"type": "command", "command": "python
~/.claude/hooks/saltmdb-prompt-submit-digest-retry.py", "timeout": 15}]}`.

### 3.5 `hooks/README.md`

Add a table row for the new script (near L182, same columns as its siblings), change the bootstrap
row's last sentence (it uses a 10 s overall budget, records a pending marker, and caps its output at
9,800 characters because Claude Code replaces hook output over 10,000 characters with a preview), and
fix the Claude Code row of the first table (L172), which lists the events the example registers and
lacks `UserPromptSubmit`.

### 3.6 Tests

Put new tests in `hooks/tests/test_digest_retry.py` (reuse the `_load_module` style; patch
`pathlib.Path.home` with `patch.object(Path, "home", return_value=tmp)` so `state_dir()` lands in a
temporary directory; every test that calls `main()` also patches `sys.stdin` with an
`io.StringIO` JSON payload carrying `session_id`, because under pytest `json.load(sys.stdin)` raises
`OSError` and the payload would be `{}`; set `bootstrap.CLI` after loading the module, since it is
resolved at import). Edit `hooks/tests/test_session_start_bootstrap.py` only if an existing
assertion breaks (its `main()` tests run with an empty payload, so they write no marker and prune
nothing). Add to `hooks/tests/test_capture_hook_config.py` a test that the Claude example has exactly
one `UserPromptSubmit` command hook running the retry script with timeout 15, and that the Codex
example has no command hook running that script. Required tests (names are suggestions):

1. Marker exists, with `running_since` set, when the first CLI call happens (fake `run_cli` asserts it).
2. All parts returned: output written, marker removed.
3. `bootstrap-digest` empty, `session-digest` present: marker `missing == ["bootstrap-digest"]`,
   `running_since` null.
4. `source == "compact"`: tracked parts are only `bootstrap-digest`; an earlier marker holding
   `session-digest` keeps it after the compact run.
5. CLI unresolved, or empty session id: no marker file.
6. Deadline: with `BUDGET_S` set to 2.0 on a freshly loaded module and a fake CLI script that
   does `exec sleep 30` (a plain `sleep` under a shell keeps the output pipe open after the kill and
   the run would block for 30 s), `main()` returns in under 6 s wall, prints
   `<saltmdb-digest-pending>`, and the marker holds both parts.
7. Retry, no marker: no CLI call, no output.
8. Retry, marker with both parts: both printed, marker and claimed file gone.
9. Retry, CLI failing: marker rewritten with `attempts == 1` and only the first part tried; after
   the third failure it is gone.
10. Retry run twice in a row after one success: the second does nothing (claim).
11. Stale `digest-pending-*` files older than a day are removed by the SessionStart hook.
12. `run_cli_bounded`: returns `None` below 1.0 s remaining and passes `min(10, remaining)` as the
    timeout (the existing `test_run_cli_uses_remaining_budget_as_subprocess_timeout` keeps passing
    through `bootstrap.run_cli`).
13. Cap: a 25,000-character fake session digest comes out of the SessionStart plain-stdout branch and
    of the retry hook at most 9,800 characters long, starts with the original text and ends with the
    notice naming both commands; a 9,000-character one is unchanged. With an overdue-core notice
    present together with the 25,000-character digest, the notice is in the output and precedes the
    session digest.
14. Retry skips a marker whose `running_since` is younger than 12 s (marker untouched, no CLI call)
    and processes one older than 12 s (including one aged 14.9 s, the killed-hook case).
15. Retry reclaims a `.claimed` file with an mtime older than 30 s (no marker present) and does not
    touch a younger one; claiming a marker whose own mtime is old and then running a second retry
    immediately does not reclaim it (the claim refreshed the mtime).
16. SessionStart step 3 keeps a part that a retry hook wrote back into the marker during the run
    (simulate with a fake `run_cli` that writes the marker) and does not lose an undelivered part.

## 4. Out of scope

- Any change under `src/` (CLI, daemon, startup instrumentation), and the unexplained 5-9 s
  `startup_lag_s` (a separate measurement item).
- A persistent daemon at boot (BL-013 service).
- Retry registration for Codex, Antigravity, Copilot and OMP (its adapter is
  `omp-hook-sources/saltmdb-lifecycle.ts`), and the live `~/.claude/settings.json` and
  `~/.agents` configs and sync.
- The `corpus-health` overdue notice, the digest content, and `docs/architecture.md`.
- Refactoring the other hook scripts onto `run_cli_bounded`.

## 5. Acceptance

Run from the main checkout; the pre-implementation baseline is in section 0.

1. `./verify` exits 0 (counts above plus the new tests; no existing test removed or weakened).
2. `.venv/bin/python -m pytest hooks/tests -q` passes, including the sixteen test groups of 3.6.
3. Manual probe, after implementation, with a temporary `HOME` and two fake CLI scripts (`slow`
   does `exec sleep 30`, `fast` prints `<saltmdb-digest>fake</saltmdb-digest>`; both executable):
   `echo '{"source":"startup","session_id":"probe"}' | HOME=$T SALTMDB_CLI_PATH=$T/slow python
   hooks/saltmdb-session-start-bootstrap.py` exits in under 12 s with `<saltmdb-digest-pending>`
   in its output and `$T/.saltmdb/hooks/.state/digest-pending-probe.json` present with `running_since` null; then
   `echo '{"session_id":"probe"}' | HOME=$T SALTMDB_CLI_PATH=$T/fast python
   hooks/saltmdb-prompt-submit-digest-retry.py` prints the fake digest and leaves no marker.
4. `rg -n "digest-pending-" hooks/ --glob '*.py' --glob '!tests/**'` finds the filename formula in
   one place (`_saltmdb_hook_common.py`) plus the prune pattern in the bootstrap script.
5. `git status --short` lists only the files in the section 0 scope; `git diff` shows no change to
   the pre-existing capture hooks in the example settings.
6. The retry script is executable (`test -x hooks/saltmdb-prompt-submit-digest-retry.py`).

## Amendment 1 (2026-10-11, architect review of the first implementation)

Evidence: the tester's probe (message m_1356) measured 9,836 characters of output with a 119-character
CLI path, because the notice embeds the path twice and D12 counted it outside the cap; the architect's
diff review found that the implementation of D4 step 3 also folds `pending_before` into the union.

- A1.1. D12 is amended in place: `HOOK_OUTPUT_MAX_CHARS` is 9800 and bounds the whole output, notice
  included; the text is cut to `HOOK_OUTPUT_MAX_CHARS - len(notice)` characters. Sections 3.1 and 3.5
  and test 13 follow (test 13 already asserts at most 9,800; add a case with a 300-character fake CLI
  path asserting the same bound, and update the README sentence and the BL-043 text to 9,800).
- A1.2. D4 step 3 stands as implemented: the union is of `pending_before`, the marker's `missing` and
  the `.claimed` file's `missing`, minus `fetched`. This never loses a part when the marker and the
  claimed file are both gone, and can at worst deliver a part twice. The earlier wording ("union of
  their `missing` lists") is replaced by this one.
- A1.3. Accepted as is, no change: an empty `session-digest` (a directory with no earlier session) is
  treated as undelivered and retried by up to three prompts, one bounded call each; the retry hook
  leaves an unreadable marker in place until the daily prune or the next SessionStart.
