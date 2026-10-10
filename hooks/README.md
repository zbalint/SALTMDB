# SALTMDB Lifecycle Hooks

Ready-to-use, copy-and-register hook scripts and harness configuration templates that automate
**SALTMDB** operations across AI agent environments (**Claude Code**, **Google Antigravity CLI**,
and **GitHub Copilot CLI**). These are shipped, production hooks, not demos — copy the scripts,
merge the config snippet for your harness, and they work.

## Design principle: agent-agnostic script bodies

Every `saltmdb-*.py` script below has an identical body regardless of which harness runs it. The
**only** harness-specific artifact is *registration* — the `*-settings-example.json` /
`copilot-hooks-example.json` snippets that wire a lifecycle event to a script. Mechanism:

- **Input parsing**: every script tries each known field-name alias in turn (`transcript_path` /
  `transcriptPath`, `tool_name` / `toolName` / `tool` / `name`) rather than assuming one
  harness's naming.
- **Output emission**: harnesses appear to ignore JSON keys they don't recognize, so a script
  emits one payload carrying every harness's expected shape redundantly (Claude Code's
  `{"decision":"block",...}`, Copilot's `{"permissionDecision":...}`, and both nested under
  `hookSpecificOutput` too). **This needs empirical per-harness verification** — if you confirm
  or disprove a harness rejects unrecognized keys, please open an issue.
- **Tool-name vocabulary**: risky-tool matching uses one merged pattern covering all three
  harnesses' own conventions, not per-harness allowlists.
- **Not every harness supports every lifecycle event, and that's fine.** `PreCompact` is
  registered natively only in the Claude Code example; Codex's example has no `PreCompact` entry,
  and the Antigravity/Copilot examples have no such event. The standalone
  `saltmdb-pre-compact-sweep.py` is a manual/cron fallback that shells out to `codex exec` or
  `claude -p`.
- **Prefer structured `PostToolUse` data over transcript-text scanning when a check can be
  expressed that way.** `tool_name`/`tool_input` arrive as parsed JSON on `PostToolUse`
  regardless of harness, so tracking "did X happen" via a small per-session state file set/cleared
  from that structured data needs no assumption about a harness's transcript JSONL shape. Guessing
  at transcript shape instead has caused two confirmed live-only bugs so far (a fixed-window bug,
  `5683870d`; a turn-boundary-detection bug conflating tool-result echoes with real user prompts,
  `74a3b9a2`) — both escaped short fake-transcript smoke tests and only surfaced via live
  dogfooding on a real, long session. `saltmdb-stop-retrieval-outcome-gate.py` no longer scans the
  transcript at all for this reason; where a check genuinely needs "since the last real user
  prompt" (e.g. `saltmdb-stop-critique-gate.py`'s risky-tool-use detection) and no structured
  alternative exists, at minimum exclude tool-result echo lines (`tool_use_id`/`tool_call_id`)
  from the match.
- **Python, not bash**: SALTMDB itself already requires Python (`saltmdb-cli` is how
  `saltmdb-session-start-bootstrap.py` gets the bootstrap digest in the first place), so assuming
  a `python3` on `PATH` is no heavier a dependency than the existing bash scripts already made.
  The stdlib `json` module handles alias-tolerant parsing and multi-schema output construction
  far more robustly than a `jq`-or-regex-fallback would. Shared plumbing (input parsing, output
  emission, transcript scanning, per-session state files) lives in `_saltmdb_hook_common.py` —
  copy it alongside the `saltmdb-*.py` scripts, it's not a hook itself.

## Adapter identity and session lifecycle

Configure `SALTMDB_AGENT_ID` in every MCP server entry before enabling these hooks. The value is
the stable lowercase identity of the agent or worker role (for example `claude`, `codex`, or
`agent_docs`) and must match `^[a-z][a-z0-9_-]{0,63}$`. It is an adapter environment setting, not
an MCP tool argument; hook prompts and tool calls must not supply `agent_id` themselves. Hook
subprocesses that invoke `saltmdb-cli` must inherit the same environment so daemon-side writes
carry the intended owner.

Each MCP adapter mints one immutable `agent_session_id` at startup. The daemon records its working
directory, owner, `started_at`, receipt-time `last_activity_at`, and nullable `ended_at`/`ended_reason`.
Normal EOF cleanup and host-initiated SIGTERM/SIGINT adapter shutdown both send a foreground `goodbye`
and durably write `ended_at` tagged `ended_reason='goodbye'`; only a raw/unclean connection loss
intentionally leaves both unset, because the adapter can reconnect with the same session ID -- a row
still unset when a *later* daemon incarnation starts up instead gets backdated and tagged
`ended_reason='orphaned'`. The Viewer reports live `active` state from the daemon connection registry,
renders a stored `ended_at` as `ended` or `lost` depending on `ended_reason`, and uses `unknown` when
daemon liveness is unavailable or no `ended_at` is set yet. Session rows are retained indefinitely.

## Conversation trace provenance (native `mcp_tool` hooks)

The conversation-trace Phase 1 capture path is registered directly in the harness settings
examples, not through a Python subprocess. `hooks/claude-settings-example.json` and
`hooks/codex-settings-example.json` each register three native `mcp_tool` entries:
- `UserPromptSubmit` calls `capture_trace_start` with the harness-specific literal and
  `${session_id}`, `${prompt_id}`/`${turn_id}`, and `${prompt}`. Claude Code also fires it, with the same `prompt_id`, for each message the
  user sends while a turn is running; `capture_trace_start` attaches those to the turn's trace as
  `mid_turn_messages` instead of discarding them (confirmed live on Claude Code; Codex behavior
  for steering input is not yet verified).
- `PostToolUse` matches only the four memory-write tool names and calls
  `capture_trace_memory_link` with just the turn identifier. The adapter records each successful
  write it performs itself and attaches every not-yet-attached one to that turn, so no
  harness-specific tool name or tool-response shape is interpolated (Claude Code delivers
  `tool_name` prefixed as `mcp__saltmdb__store_memory` and `tool_response` as a list of MCP
  content blocks, so `${tool_name}` and `${tool_response.data.id}` never resolved). Older configs
  that still pass `entity_id`/`just_run_tool_name` keep working; the values are ignored.
- `Stop` calls `capture_trace_complete` with the current turn identifier and
  `${last_assistant_message}`.

- The Codex example also passes `"hook_output": true` to all three. Codex validates an
  `mcp_tool` hook's text result as event hook JSON against a strict schema and rejects SALTMDB's
  `{status, data, warnings}` envelope (`Hook failed: hook returned invalid ... JSON output` on
  every turn, though the capture itself succeeded); with the flag the tools return a bare `{}`,
  which Codex accepts on `UserPromptSubmit`, `PostToolUse` and `Stop` (verified live on Codex CLI
  0.158.0). Claude Code accepts the envelope, so its example omits the flag. In this mode a
  capture failure is not reported to the hook. Existing live Codex configs need the flag added
  and the MCP server restarted onto this version.

Capture is disabled by default; set `SALTMDB_TRACE_CAPTURE_ENABLED=true` in the MCP server's
`env` (the adapter process gates the `capture_trace_*` tools -- the shared daemon has no flag and
always captures) before enabling these registrations.

Native registration preserves the adapter's trusted `agent_session_id`; where a harness supports
`mcp_tool` hook entries (Claude Code, Codex) do not replace them with a CLI or standalone Python
bridge. Copilot CLI has no such entry type, so it alone uses a bridge:
[`saltmdb-stop-trace-capture.py`](saltmdb-stop-trace-capture.py) on `agentStop` and `sessionEnd`.
It reads Copilot's transcript (`interactionId` is the turn key), calls the daemon over loopback RPC
and takes its `agent_session_id` from `~/.saltmdb/copilot_adapter_<copilot pid>.json`, which the MCP
adapter publishes only when `SALTMDB_TRACE_CAPTURE_ENABLED=true` -- without that file the hook does
nothing. It never blocks Copilot; failures go to `~/.saltmdb/hooks/trace-capture.log`. Copilot's
`SALTMDB_AGENT_ID` should be its own value (e.g. `copilot`). Not yet verified on native Windows:
that the hook and the adapter see the same `COPILOT_LOADER_PID`, or the same parent pid. After enabling the examples in a live session,
perform one successful `store_memory` and confirm a `trace_memory_links` row through `get_trace`
or `search_traces(entity_id=...)`. A failed link is non-fatal for the calling hook.

## Answer-side related memories (experimental, opt-in)

[`saltmdb-stop-related-memories.py`](saltmdb-stop-related-memories.py) runs at `Stop` after the
agent has written its answer. It passes the reply (`last_assistant_message`) to the read-only
`saltmdb-cli related-memories` and, when memories turn up that the agent has not seen this
session, blocks the stop once and lists up to three memory ids with their titles, so the agent can
read them with `get_memory` and amend the answer, or reply in one line that none applies.

- **Off by default.** It acts only when `SALTMDB_RELATED_MEMORIES_HOOK=1`; only exactly `1` enables
  it, and every other value (including `0`) disables it. `SALTMDB_RELATED_MEMORIES_MIN_SCORE` sets
  the relevance threshold (default `4.0`, a placeholder until the BL-028 calibration; a value that
  is not a number is ignored and logged). Set both in the harness settings `env` block or the shell:
  a hook inherits the harness environment, not the MCP server's.
- **Caps and guard order.** The three loop guards run before reply presence/length checks, so even a
  short or missing continuation reply clears pending state. Replies under 200 characters are
  skipped. At most one prompt per turn (the Stop right after a block is treated as the agent's
  answer to it), at most three prompts per session, at most three memories per prompt, and an id is
  never listed twice in a session. Ids the transcript already mentions are skipped; a bare all-digit
  8-hex token (usually a date) is ignored, while the 8-character prefix of a full UUID always
  counts. That scan is best effort.
- **Latency.** It adds the CLI's run time to the end of a long reply: the CLI stops itself after
  4 s and the hook gives up after 6 s.
- **Requirements.** A SALTMDB version with `saltmdb-cli related-memories`, found the same way as
  the session-start bootstrap finds the CLI (`SALTMDB_CLI_PATH`, `PATH`, the legacy venv path).
  No CLI, no running daemon or no loaded models make the hook a silent no-op: it never starts a
  daemon and always exits 0. Silent exits are logged once per reason per session to
  `~/.saltmdb/hooks/related-memories.log` (truncated past 100 KB).
- **Scope.** `SALTMDB_AGENT_ID` reaches the CLI only when the harness sets it in the hook's
  environment; without it, only shared-scope memories can surface.
- **Codex.** The Codex example registers it too, as experimental: the Codex example had no command
  hook before, and the Codex `Stop` payload and transcript fields are unverified. Without a
  transcript the hook relies on its own record of listed ids.
- **Known interaction.** A Stop block is stored in the transcript as a user-role line, and the
  self-critique gate counts it as a new user prompt. That can re-arm the gate's Stage 1 inside
  its own per-session cap.

## Naming convention

`saltmdb-<lifecycle-event>-<purpose>[-<harness>].py`, where `<lifecycle-event>` is one of
`session-start`, `pre-tool`, `post-tool`, `stop`, `session-end`, `pre-compact`. A `<harness>`
suffix is added only when a script's actual mechanics differ per harness — never just because
it happens to be referenced from that harness's config (none of the scripts below need one;
every body is fully shared).

## 📁 Included Files

### Configuration templates

| File | Host harness | Description |
| :--- | :--- | :--- |
| [`claude-settings-example.json`](claude-settings-example.json) | Claude Code | Global settings snippet for `~/.claude/settings.json`: `SessionStart`, `PreToolUse`, `PostToolUse`, `PreCompact`, `Stop`, `SessionEnd`. |
| [`codex-settings-example.json`](codex-settings-example.json) | Codex | Native `mcp_tool` settings snippet for the three conversation-trace capture events. |
| [`antigravity-settings-example.json`](antigravity-settings-example.json) | Antigravity CLI (`agy`) | Settings snippet for `~/.gemini/antigravity-cli/settings.json`: `PreInvocation`, `PreToolUse`. |
| [`copilot-hooks-example.json`](copilot-hooks-example.json) | GitHub Copilot CLI | Spec template for `.github/hooks/saltmdb.json`: `sessionStart`, `preToolUse`, `agentStop`, `sessionEnd` (trace capture). |

### Python scripts

| File | Lifecycle event(s) | Description |
| :--- | :--- | :--- |
| [`_saltmdb_hook_common.py`](_saltmdb_hook_common.py) | *(not a hook)* | Shared stdlib-only helpers imported by every script below: alias-tolerant field lookup, transcript scanning, multi-schema JSON emission, per-session state files. |
| [`saltmdb-session-start-bootstrap.py`](saltmdb-session-start-bootstrap.py) | `SessionStart` / `PreInvocation` / `sessionStart` | Injects the canonical core-memory bootstrap digest (`saltmdb-cli bootstrap-digest`), plus a nudge if any core memory is overdue for review (`saltmdb-cli corpus-health`), and the directory-scoped last-session digest when available (`saltmdb-cli session-digest`; with trace capture on it also carries a last-exchange handover, capped by `SALTMDB_HANDOVER_MAX_CHARS`, default 40000, `0` disables). Locates `saltmdb-cli` via (in order) the `SALTMDB_CLI_PATH` env var, `PATH`, then a last-resort `~/.mcp/SALTMDB/.venv/bin/saltmdb-cli` guess — set `SALTMDB_CLI_PATH` if your install lives somewhere `PATH` doesn't reach inside a hook subprocess. Claude Code and Codex take the digest as plain stdout; Antigravity's `PreInvocation` contract requires an `injectSteps` JSON response, so the script serializes the same digest to that shape when its camelCase payload is present. |
| [`saltmdb-pre-tool-search-gate.py`](saltmdb-pre-tool-search-gate.py) | `PreToolUse` / `preToolUse` | Enforces Rule 1 ("Think Before You Leap"): denies a risky edit/bash/file-write call until `search_memory` has been called this session. Does its own read-only-tool check internally (needed for Copilot, whose `preToolUse` fires unfiltered) — replaces the old separate Copilot-only pre-tool script, which reimplemented the same decision logic with drift risk. Its primary "was `search_memory` called this session" signal is a structured per-session flag file set by `saltmdb-post-tool-response-nudges.py` on every `search_memory` call (see Windows notes below for why this replaced a transcript-only check); a transcript scan remains as a fallback for harnesses that do supply `transcript_path`. Tool-name lookup falls back to Copilot's nested `toolCalls[0].name` shape when no flat `tool_name`/`toolName` field is present. |
| [`saltmdb-post-tool-response-nudges.py`](saltmdb-post-tool-response-nudges.py) | `PostToolUse` on `store_memory`/`search_memory` | Inspects the tool *response*, not just the tool name: nudges on unacted `duplicate_candidates`, a `store_memory` with no follow-up `manage_relation`, and an empty `mode="strict"` result. Also sets two per-session flags on every `search_memory` call: the retrieval-outcome-pending flag (for the Stop-time gate below) and the search-memory-called flag (for `saltmdb-pre-tool-search-gate.py` above). |
| [`saltmdb-post-tool-failure-circuit-breaker.py`](saltmdb-post-tool-failure-circuit-breaker.py) | `PostToolUse` on `log_event` | Fingerprints repeated `log_event(event_type="issue")` calls sharing an `error_code`; nudges CLAUDE.md rule 2 (stop after 2 consecutive failures, search memory, replan) instead of relying on the agent remembering it mid-loop. Also clears the retrieval-outcome-pending flag on a matching `log_event(event_type="retrieval_outcome")` call. |
| [`saltmdb-stop-critique-gate.py`](saltmdb-stop-critique-gate.py) | `Stop` / `agentStop` | Two-stage gate: (1) mandatory self-reflection before closing a turn that touched files/commands — the questions come from an optional `stop-critique-questions.json` next to the script (`{"questions": [...]}`), falling back to two built-in defaults if it is missing or malformed; (2) requires that reflection to become a `store_memory` call or an explicit "no durable lesson" acknowledgment — otherwise a genuine finding just evaporates. Stage 1 is capped per session at a randomized 1–4 distinct episodes (rolled once per session id; retrying an unanswered prompt spends no budget), after which the gate goes quiet; Stage 2 is never capped. |
| [`saltmdb-stop-trace-capture.py`](saltmdb-stop-trace-capture.py) | `agentStop` / `sessionEnd` (Copilot CLI only) | Trace capture for Copilot, which has no native `mcp_tool` hook entry: parses the session transcript and sends each interaction to the daemon (see "Conversation trace provenance"). Silent no-op unless the adapter has published its session file; always exits 0. |
| [`saltmdb-stop-retrieval-outcome-gate.py`](saltmdb-stop-retrieval-outcome-gate.py) | `Stop` / `agentStop` | Telemetry enforcement: if `search_memory` was called this turn (per the pending flag above), requires a `log_event(event_type="retrieval_outcome", ...)` call before the turn closes; nudges once, then lets it go rather than block forever. See the `saltmdb-usage` skill for the logging convention. |
| [`saltmdb-stop-related-memories.py`](saltmdb-stop-related-memories.py) | `Stop` (Claude Code; Codex experimental) | Experimental and off by default: lists up to three related memories the agent has not seen after it answers, through `saltmdb-cli related-memories`. See "Answer-side related memories". |
| [`saltmdb-session-end-wrapup-reminder.py`](saltmdb-session-end-wrapup-reminder.py) | `SessionEnd` | One-shot reminder, at true session close (not every turn), to check `get_events` for anything durable that only exists in the ephemeral event ledger. |
| [`saltmdb-pre-compact-sweep.py`](saltmdb-pre-compact-sweep.py) | `PreCompact` (Claude Code native; standalone/manual fallback for Codex and harnesses without a native agent hook; absent from Antigravity/Copilot examples) | Standalone version of the pre-compaction sweep. Claude Code's native `"type": "agent"` PreCompact hook (see `claude-settings-example.json`) is the best mechanism where available; this script is the fallback for manual/cron invocation or harnesses without a native agent-type hook — it shells out to `codex exec` or `claude -p` (tried in that order; set `SALTMDB_HOOK_PREFERRED_AGENT=claude` to try `claude -p` first) since a bare script has no MCP tool context of its own. |
| [`saltmdb-skill-review-sweep.py`](saltmdb-skill-review-sweep.py) | Manual / cron only (no lifecycle event) | Mining and diagnosis sweep for skill/hook improvements. Shells out to `claude -p` or `codex exec` to perform a 5-step telemetry review (mine, diagnose, pair-check, propose, gate). Never auto-applies file edits; outputs proposals as gated memories for human review. |
| [`saltmdb-checkable-fact-drift-sweep.py`](saltmdb-checkable-fact-drift-sweep.py) | Manual / cron only (no lifecycle event) | Periodic sweep verifying checkable fact memories against live repository source code to flag stale citations. Never auto-corrects or edits content; flags surface via `search_memory`'s `drift_flag` field. |

**Explicit non-goals**:
- No hook here nudges or automates `consolidate_memories`. Deciding which memories are cohesive enough to merge stays a deliberate agent judgment call.
- The skill-review sweep (`saltmdb-skill-review-sweep.py`) never auto-applies a file edit either — output is always a review-gated memory proposal.
- The checkable-fact drift sweep (`saltmdb-checkable-fact-drift-sweep.py`) never edits a flagged memory's title/content/tags and never calls lifecycle tools (e.g. `consolidate_memories`, `revise_memory`, `supersede_memory`) — flagging is metadata-only and non-destructive.

---

## 🚀 Quick Setup Instructions

### 1. Claude Code
```bash
mkdir -p ~/.claude/hooks
cp saltmdb-*.py _saltmdb_hook_common.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/saltmdb-*.py
```
Merge the configuration snippet from [`claude-settings-example.json`](claude-settings-example.json) into your `~/.claude/settings.json`.

### 2. Google Antigravity CLI (`agy`)
Place scripts in `$HOME/.mcp/SALTMDB/hooks/` or a custom directory in your `$PATH`, and
add the block from [`antigravity-settings-example.json`](antigravity-settings-example.json) to
`~/.gemini/antigravity-cli/settings.json`.

### 3. GitHub Copilot CLI
Copy hook scripts (including `_saltmdb_hook_common.py`) to `~/.copilot/hooks/` (or repository
`.github/hooks/`), and add `.github/hooks/saltmdb.json` using
[`copilot-hooks-example.json`](copilot-hooks-example.json) as a reference.

---

## 🪟 Windows notes

The three command-based example configs (Claude Code, Antigravity, and Copilot) invoke scripts as
`python <path>` (never a bare `.py` path relying on the POSIX shebang line); Codex uses native
`mcp_tool` entries instead. This is confirmed necessary, not just defensive: native Windows has no shebang
support at all, and `python3` (the POSIX convention this repo otherwise uses) is typically not on
`PATH` on Windows, only `python`/`py` (community-confirmed, e.g.
[claude-plugins-official#85](https://github.com/anthropics/claude-plugins-official/issues/85)).
This costs nothing on macOS/Linux -- `python script.py` runs identically to a shebang+chmod
invocation there.

`copilot-hooks-example.json`'s `powershell` field previously pointed at `saltmdb-*.ps1` files
that were **never shipped** -- a real, confirmed bug (every Windows Copilot CLI hook silently
never fired, since the target file never existed). Per
[GitHub's own hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference), the
`bash`/`powershell` fields are shell command-lines, not required script paths, so the fix invokes
the *same* shared `.py` script via `python "..."` instead of requiring a parallel PowerShell
reimplementation -- keeping the one-shared-implementation principle above intact for Copilot CLI
too.

Four further confirmed-live bugs, all specific to Windows Copilot CLI's actual payload shape
(as opposed to documented/assumed shape -- see the pattern above), all fixed:
1. `RISKY_TOOL_NAMES` (used by `saltmdb-stop-critique-gate.py`) matched literal-cased tool names
   only, but Copilot lowercases tool names in its transcript (`"bash"`/`"edit"` vs Claude Code's
   `"Bash"`/`"Edit"`) -- the risky-call detection silently never matched. Now case-insensitive.
2. Copilot's `preToolUse` payload has no flat `tool_name`/`toolName` field at all, only a nested
   `toolCalls[0].name` -- `saltmdb-pre-tool-search-gate.py`'s read-only-tool fast path never
   engaged. `get_tool_name()` in `_saltmdb_hook_common.py` now falls back to the nested shape.
3. Copilot's `preToolUse` carries **no `transcript_path` field at all** (unlike its own
   `agentStop`, which does) -- the search gate's only signal was a transcript scan keyed off
   `transcript_path`, so it always saw an empty segment and permanently fail-opened, allowing
   every edit/bash/PowerShell call regardless of `search_memory` history. Fixed with a structured
   per-session flag file (`search_memory_called_flag_path`) set by
   `saltmdb-post-tool-response-nudges.py` on every `search_memory` call and checked by the gate
   before falling back to the transcript scan -- no dependency on `transcript_path` being present.
   **Known residual gap**: the very first risky call of a Copilot session, before `search_memory`
   has ever run (so before the flag can exist, with no transcript to fall back on either), still
   fails open -- closing that would mean flipping the "can't verify" default from fail-open to
   fail-closed, a separate strictness trade-off not made here.
4. Bug 3's own fix shipped broken on first pass: `saltmdb-post-tool-response-nudges.py` and
   `saltmdb-post-tool-failure-circuit-breaker.py` still read `tool_name` via the old flat-alias-only
   `get_field(...)` call, not the new `get_tool_name()` from bug 2's fix -- so on Copilot's
   `postToolUse` (same nested `toolCalls[0].name` shape as its `preToolUse`), `tool_name` was
   always `""`, `.endswith("search_memory")` never matched, and `search_memory_called_flag_path`'s
   flag was never actually written in practice. Bug 3's fix was correct in isolation (verified
   against a synthetic flat payload) but never verified against Copilot's real nested
   `postToolUse` shape before shipping -- exactly the gap flagged as a risk in the "audit closed
   vocab / verify against a real sample" lesson from bug 1's fix, and it bit immediately on the
   very next thing that used the same lookup pattern. Both scripts now use `get_tool_name()` too.

Claude Code's own Windows hook execution has open, upstream bugs unrelated to anything in this
repo -- worth knowing if a Claude Code hook still doesn't fire on Windows after the `python`
fix above: shell/PATH resolution
([anthropics/claude-code#73971](https://github.com/anthropics/claude-code/issues/73971)) and
`.sh`/file-association handling
([anthropic-code-mirror/claude-code#24097](https://github.com/anthropic-code-mirror/claude-code/issues/24097)).
Also unverified from this repo: whether a literal `~` in a *global* `~/.claude/settings.json`
hook `command` (as opposed to a project-scoped one) reliably expands on Windows -- left as-is
here since it's Claude Code's own documented convention, not something to silently "fix" with an
unverified guess; if your Windows Claude Code hooks still don't fire, try an absolute path in
place of `~/.claude/hooks/...`.

---

## 📚 Detailed Documentation

For the conceptual overview of what each lifecycle event does, see
**[docs/architecture.md §7 (Automated Session Lifecycle Hooks)](../docs/architecture.md#7-automated-session-lifecycle-hooks)**.
For exact JSON schemas, per-harness payload shapes, and pre-tool decision protocols, read the
scripts themselves — each one documents its own mechanism in its module docstring (see the file
table above).

For the usage discipline these hooks enforce (title/quality standards, search modes, the
retrieval-outcome telemetry convention), see the **`saltmdb-usage`** skill in
[`../skills/`](../skills/).
