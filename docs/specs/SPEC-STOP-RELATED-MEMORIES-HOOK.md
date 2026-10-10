# SPEC-STOP-RELATED-MEMORIES-HOOK

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-030. Context id: `answer-side-memory-hook-2026-10-10`.
- Baseline: `0c68afa`, `./verify` exit 0 (2130 passed, 154 subtests).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, `develop`.
- Test seams: the script run as a subprocess with an isolated `HOME`, a stub `saltmdb-cli`
  (`SALTMDB_CLI_PATH`) and transcript fixtures, in the style of
  `hooks/tests/test_retrieval_outcome_flow.py`; plus the pure functions `clean_title`,
  `parse_rows`, `seen_tokens`, `format_reason`.
- Scope (may edit): `hooks/saltmdb-stop-related-memories.py` (new),
  `hooks/tests/test_stop_related_memories.py` (new), `hooks/_saltmdb_hook_common.py` (add
  `resolve_cli()` only), `hooks/claude-settings-example.json`, `hooks/codex-settings-example.json`,
  `hooks/README.md`.
- Does not touch: `src/`, `scripts/`, the other hook scripts (including
  `saltmdb-session-start-bootstrap.py`, which keeps its own copy of the resolver, and both Stop
  gates), the Copilot and Antigravity example files, `docs/`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

After an agent writes its answer, SALTMDB may hold memories that bear on it (a decision made
earlier, a constraint, a past finding). The agent often never searched for them. The read-only
command `saltmdb-cli related-memories` (BL-025) finds them from the answer text. This hook runs it
at Stop and, when something relevant turns up that the agent has not seen this session, blocks the
stop once and lists up to three memory ids with titles, so the agent can read them and amend its
answer. The owner prioritised this feature.

It ships experimental, opt-in and disabled by default, because the relevance threshold has not
been calibrated yet (BL-028 supplies the harness; the calibration run needs a live snapshot and
labels). A later commit flips one constant and one test expectation.

Rejected: calling the daemon directly from the hook (the CLI already owns the timeout, the
no-spawn rule and the exit-0 behaviour); an MCP-tool hook (it cannot filter or format); firing on
every reply without a relevance threshold (a hook that blocks with unrelated memories costs trust);
filtering by the strict search gate (it fails the known positive example); a transcript-tail scan
as the main "already seen" signal (the transcript is written asynchronously and lags the turn, and
an earlier hook in this repository was rewritten away from transcript scanning for that reason).

## 2. Decisions

- D1. Script `hooks/saltmdb-stop-related-memories.py`, Stop event (Claude Code; Codex
  experimental). stdlib only, imports `_saltmdb_hook_common`. It always exits 0. The first thing it
  does is read `SALTMDB_RELATED_MEMORIES_HOOK` (D4): when the hook is off it exits before reading
  stdin, creating `state_dir()` or any file. Every later silent exit appends one line with the
  reason to `~/.saltmdb/hooks/related-memories.log`, at most once per reason per session; the log
  is truncated when it grows past 100 KB.
- D2. Input: the reply text is the payload's `last_assistant_message`. Empty, missing or shorter
  than `MIN_REPLY_CHARS` (200): exit 0. When the field is missing the hook logs the payload's key
  names (never values) once per session.
- D3. Loop guards in this order, all backed by `state_dir()/related-memories-<session id>.json`
  (pruned with `prune_stale_state`):
  1. Payload `stop_hook_active` true: exit 0 (treated as optional; absent means false).
  2. `pending_continuation` true and younger than 10 minutes: the previous Stop was blocked by
     this hook and this Stop is the agent's answer to it. Clear the flag and exit 0. This gives
     at most one prompt per turn without relying on `stop_hook_active` or a turn id.
  3. `prompts` >= `MAX_PROMPTS_PER_SESSION` (3): exit 0.
  The state also holds `shown_ids` (D6) and the logged reasons.
- D4. Enablement: the hook acts only when `SALTMDB_RELATED_MEMORIES_HOOK` is `1`; `0` always
  disables; unset follows the module constant `DEFAULT_ENABLED = False`. The threshold is env
  `SALTMDB_RELATED_MEMORIES_MIN_SCORE` (a value that does not parse as a float is ignored and
  logged) else the module constant `DEFAULT_MIN_SCORE = 4.0`
  (`# shortcut: placeholder tuned for near-duplicates; replace with the BL-028 calibration`).
  The calibration commit changes `DEFAULT_ENABLED`, `DEFAULT_MIN_SCORE` and the T2/T7 expectations.
  Both variables are set in the harness settings `env` block or the shell; a hook inherits the
  harness environment, not the MCP server's.
- D5. Lookup: add `resolve_cli()` to `_saltmdb_hook_common.py` with the same precedence as the
  session-start bootstrap's `_resolve_cli` (`SALTMDB_CLI_PATH` if the file exists, `saltmdb-cli` on
  PATH, the legacy `~/.mcp/SALTMDB/.venv/bin/saltmdb-cli`); its docstring names the bootstrap
  script's copy, and a follow-up moves the bootstrap onto it. The hook runs
  `[CLI, "related-memories", "--limit", "6", "--min-score", <T>, "--timeout-ms", "4000"]` with the
  reply on stdin (UTF-8 for stdin and stdout), `capture_output`, subprocess timeout 6 s. No CLI
  found, a non-zero exit (an older CLI exits 2 on the unknown subcommand) or empty stdout: exit 0.
  The environment is passed through unchanged, so `SALTMDB_AGENT_ID`, when the harness sets it,
  reaches the CLI; when it does not, only shared-scope memories can surface, and the README says so.
- D6. Rows and seen filter. `parse_rows(stdout)`: split each line on the first tab only; keep rows
  whose id is a full UUID and drop the rest. `seen_tokens(transcript_text)`: collect every 8-hex
  token matched with `(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])` plus the 8-character prefix of every
  full UUID found, over the whole transcript (`read_transcript_full`; empty when `transcript_path`
  is missing, as for Codex today). A row is dropped when its id starts with a seen token or its
  full id is in `shown_ids`. The first 3 remaining rows are listed. None left: exit 0. Listed ids
  are added to `shown_ids` and are never listed again in the session. The transcript scan is best
  effort and over-suppresses (an id inside a search result counts as seen) and lags the latest
  tool result; `shown_ids` is the reliable part. # shortcut: a PostToolUse recorder of ids the
  agent read would be exact; add it if over- or under-suppression shows up in use.
- D7. Block reason `format_reason(rows)`: starts with the sentinel comment
  `<!-- saltmdb-related-memories-prompt -->`, then one fixed sentence saying the lines below are
  memory ids with their titles in quotes, that quoted text is data, then one line per row
  `- <first 8 chars of id> "<title>"`, then the fixed closing instruction, which is last and does
  not refer to the titles: read the ones that bear on the answer with `get_memory`, amend the answer
  if one changes it, otherwise reply with one short line saying no related memory applies. Titles
  are untrusted and `clean_title` applies: remove every character of Unicode category Cc or Cf
  (replace newlines and tabs with a space first), remove backticks and angle brackets and double
  quotes, collapse whitespace, replace the substring `saltmdb-` with `saltmdb_` and the substring
  `store_memory` with `store memory` (other hooks scan the transcript for those markers), cut to
  100 characters. A title that is empty after cleaning becomes `(untitled)`.
- D8. Output through `stop_block_payload(data, reason)`. Before printing, the state file is written
  with `prompts + 1`, `pending_continuation` true (with a timestamp) and the new `shown_ids`.
- D9. Registration. `hooks/claude-settings-example.json`: append the command entry (timeout 15)
  to the `hooks` list of the existing Stop group, no second Stop key. `hooks/codex-settings-example.json`:
  add a command entry to its Stop group, marked experimental in the README because the Codex
  example has no command hook yet and the Codex Stop payload and transcript fields are unverified.
  `hooks/README.md` documents: experimental and opt-in; the env variables; the caps; the added
  latency at the end of a long reply (the CLI's own ceiling is 4 s, the hook's 6 s); the need for a
  SALTMDB version with `related-memories`; the silent no-ops (no CLI, no daemon, no models); the
  scope note from D5; and the known interaction that a Stop block is stored as a user-role line
  that the self-critique gate counts as a new user prompt, which can re-arm its Stage 1 inside that
  gate's own per-session cap.

## 3. Changes per file

One new script, one new test file, one added function in `_saltmdb_hook_common.py`, one entry in
each of two JSON examples, one README section.

## 4. Tests

(Subprocess runs with an isolated `HOME`, `PATH=/usr/bin:/bin` plus `SALTMDB_CLI_PATH` pointing at
an executable stub that records its arguments, stdin and environment and prints canned lines;
transcript fixtures.)

- T1. Fires: three valid rows: stdout is a block payload with the sentinel, the three short ids and
  quoted titles, and the closing instruction last; the state file has `prompts` 1,
  `pending_continuation` true and the three ids in `shown_ids`; the stub got the reply on stdin and
  `--limit 6 --min-score 4.0 --timeout-ms 4000`.
- T2. Silent exits: hook disabled (`0`); env unset with `DEFAULT_ENABLED` false (the test reads the
  constant); disabled run creates no state directory and reads no stdin; `stop_hook_active` true;
  reply under 200 chars; no `last_assistant_message` (key names logged, no values); stub missing;
  stub exits 2; stub prints nothing; empty stdin; malformed JSON; stub killed by timeout.
- T3. Chain: a first run fires; a second run with a different long reply is silent and clears
  `pending_continuation`; a third run with a new reply fires again; the fourth prompt in a session is
  silent; the same ids are never listed twice; the chain works with `stop_hook_active` absent.
  A `pending_continuation` older than 10 minutes does not suppress.
- T4. Seen filter: a row whose short id appears in the transcript is dropped (inside a full UUID and
  as a bare 8-hex token); an 8-hex slice of a longer hex run and an 8-digit date are not tokens;
  all dropped: silent; more than 3 left: first 3; no `transcript_path` (Codex shape): only
  `shown_ids` filters.
- T5. Row and title hygiene: a row with a non-UUID id and a line without a tab are dropped; control
  and format characters, bidi marks, backticks, angle brackets, quotes and a 500-character title
  are neutralised and cut; `saltmdb-self-critique-done`, `saltmdb-no-lesson-this-turn` and
  `store_memory` inside a title do not appear in the output; an instruction-like title appears only
  inside the quoted data line and the closing instruction is still the last line.
- T6. Output shape: a Claude-shaped payload and a Codex-shaped payload (`turn_id` and `model` set)
  each equal `stop_block_payload`'s output for the same reason.
- T7. `SALTMDB_RELATED_MEMORIES_MIN_SCORE` overrides the threshold passed to the stub; an
  unparseable value falls back to `DEFAULT_MIN_SCORE` and is logged.
- T8. Examples: both JSON files parse; the Stop group of each contains one command entry whose
  command contains `saltmdb-stop-related-memories.py`, found by substring; the Claude file still has
  exactly one `Stop` key; the script exists in `hooks/`.
- T9. Gate interaction fixture: a transcript where this hook's feedback line follows the critique
  gate's prompt line; the test documents (asserts) the current critique-gate behaviour so a later
  fix to that gate is a visible change.
- T10. `resolve_cli()`: the three precedence cases with a temporary `HOME` and `PATH`.
- T11. Log: a repeated silent reason is logged once per session; the log is truncated past 100 KB.

## 5. Out of scope

The calibrated threshold and flipping the defaults (BL-028 results), Copilot and Antigravity
registration, deploying to the live install, the `~/.agents` wiring, a PostToolUse id recorder, the
critique gate's handling of Stop-feedback lines, moving the bootstrap hook onto `resolve_cli()`,
changes to any `src/` file. Known side effect not addressed here: the trace-capture Stop entry
stores the final reply of a turn, so the agent's short acknowledgement after a block may replace
the stored reply of that turn; the replay harness already excludes short replies.

## 6. Acceptance

1. `.venv/bin/python -m pytest hooks/tests/test_stop_related_memories.py -q` green.
2. `rg -n "sqlite3|socket" hooks/saltmdb-stop-related-memories.py` has no match.
3. `./verify` exit 0, one full run.
4. `git status --short` lists only the files in section 0.

## 7. Pre-lock gate notes

Checked: the Claude Code hooks documentation (fetched 2026-10-10) names `last_assistant_message` as
the Stop field to use because the transcript lags; both example files template the same field for
the trace hook; `stop_hook_active` was not visible in the fetched part, so D3 does not depend on
it; the existing sentinels (`saltmdb-retrieval-outcome-prompt`, `saltmdb-stop-critique-prompt`,
`-stage2-prompt`, `saltmdb-self-critique-done`, `saltmdb-no-lesson-this-turn`) do not collide with
`saltmdb-related-memories-prompt`; the Claude example already has one Stop group holding two command
gates and one mcp_tool entry; the Codex example has no command hook. Not verified: the Codex Stop
payload, Codex transcript format, hook execution order within one Stop event.
Run at lock, 2026-10-10: re-grepped D1-D9 and T1-T11 for `DEFAULT_ENABLED`,
`DEFAULT_MIN_SCORE`, `MIN_REPLY_CHARS`, `MAX_PROMPTS_PER_SESSION`, `pending_continuation`,
`shown_ids` and `resolve_cli` (consistent); `read_transcript_full`, `stop_block_payload`,
`state_dir` and `prune_stale_state` exist in `_saltmdb_hook_common.py` with the used signatures;
`hooks/README.md` already records that Codex accepts hook output on `Stop` (verified live there).
A read-only review (tester) changed the design: shown-id state instead of a transcript tail, one
prompt per turn through `pending_continuation`, title and id hygiene, `resolve_cli()` in the common
module, default-off constants, example-file placement; the changes were applied and D1-D9 re-read
against each other. No second pass. Acceptance 1 runs after implementation.

## Amendment 1 (2026-10-10)

Found by the developer, verified by the architect: `hooks/tests/test_capture_hook_config.py`
`test_claude_pre_existing_entries_remain_byte_identical` compares the working tree's `command`
hooks per event with `git show HEAD:hooks/claude-settings-example.json` and asserts equality, so
any appended Stop command entry (D9) makes it fail while the change is uncommitted, and acceptance
3 cannot pass.

- A1.1. `hooks/tests/test_capture_hook_config.py` joins the §0 scope for exactly one line: the
  assertion `self.assertEqual(after_commands, before_commands)` becomes
  `self.assertEqual(after_commands[: len(before_commands)], before_commands)`. The test keeps its
  purpose (pre-existing command entries stay unchanged and in the same order) and allows entries
  appended after them. The `capture_count` assertion is unchanged.
- A1.2. D9 and T8 stand; the new Stop command entry is appended after the existing ones in the
  Stop group. Acceptance 4 now lists this test file as an allowed change.
- A1.3. Pre-lock check for the amendment: `rg -n "claude-settings-example|codex-settings-example"`
  over `tests/` and `hooks/tests/` finds this test file and the new T8 only; the Codex example has
  no equivalent baseline test.
