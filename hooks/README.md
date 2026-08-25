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
  Claude-Code-only today (confirmed absent from Antigravity's and Copilot's own hook event
  sets) — a harness just doesn't register a hook it has no event for.
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
| [`antigravity-settings-example.json`](antigravity-settings-example.json) | Antigravity CLI (`agy`) | Settings snippet for `~/.gemini/antigravity-cli/settings.json`: `PreInvocation`, `PreToolUse`. |
| [`copilot-hooks-example.json`](copilot-hooks-example.json) | GitHub Copilot CLI | Spec template for `.github/hooks/saltmdb.json`: `sessionStart`, `preToolUse`, `agentStop`. |

### Python scripts

| File | Lifecycle event(s) | Description |
| :--- | :--- | :--- |
| [`_saltmdb_hook_common.py`](_saltmdb_hook_common.py) | *(not a hook)* | Shared stdlib-only helpers imported by every script below: alias-tolerant field lookup, transcript scanning, multi-schema JSON emission, per-session state files. |
| [`saltmdb-session-start-bootstrap.py`](saltmdb-session-start-bootstrap.py) | `SessionStart` / `PreInvocation` / `sessionStart` | Injects the canonical core-memory bootstrap digest (`saltmdb-cli bootstrap-digest`), plus a nudge if any core memory is overdue for review (`saltmdb-cli corpus-health`), and the directory-scoped last-session digest when available (`saltmdb-cli session-digest`). Locates `saltmdb-cli` via (in order) the `SALTMDB_CLI_PATH` env var, `PATH`, then a last-resort `~/.mcp/SALTMDB/.venv/bin/saltmdb-cli` guess — set `SALTMDB_CLI_PATH` if your install lives somewhere `PATH` doesn't reach inside a hook subprocess. |
| [`saltmdb-pre-tool-search-gate.py`](saltmdb-pre-tool-search-gate.py) | `PreToolUse` / `preToolUse` | Enforces Rule 1 ("Think Before You Leap"): denies a risky edit/bash/file-write call until `search_memory` has been called this session. Does its own read-only-tool check internally (needed for Copilot, whose `preToolUse` fires unfiltered) — replaces the old separate Copilot-only pre-tool script, which reimplemented the same decision logic with drift risk. Its primary "was `search_memory` called this session" signal is a structured per-session flag file set by `saltmdb-post-tool-response-nudges.py` on every `search_memory` call (see Windows notes below for why this replaced a transcript-only check); a transcript scan remains as a fallback for harnesses that do supply `transcript_path`. Tool-name lookup falls back to Copilot's nested `toolCalls[0].name` shape when no flat `tool_name`/`toolName` field is present. |
| [`saltmdb-post-tool-response-nudges.py`](saltmdb-post-tool-response-nudges.py) | `PostToolUse` on `store_memory`/`search_memory` | Inspects the tool *response*, not just the tool name: nudges on unacted `duplicate_candidates`, a `store_memory` with no follow-up `manage_relation`, and an empty `mode="strict"` result. Also sets two per-session flags on every `search_memory` call: the retrieval-outcome-pending flag (for the Stop-time gate below) and the search-memory-called flag (for `saltmdb-pre-tool-search-gate.py` above). |
| [`saltmdb-post-tool-failure-circuit-breaker.py`](saltmdb-post-tool-failure-circuit-breaker.py) | `PostToolUse` on `log_event` | Fingerprints repeated `log_event(event_type="issue")` calls sharing an `error_code`; nudges CLAUDE.md rule 2 (stop after 2 consecutive failures, search memory, replan) instead of relying on the agent remembering it mid-loop. Also clears the retrieval-outcome-pending flag on a matching `log_event(event_type="retrieval_outcome")` call. |
| [`saltmdb-stop-critique-gate.py`](saltmdb-stop-critique-gate.py) | `Stop` / `agentStop` | Two-stage gate: (1) mandatory 2-question self-reflection before closing a turn that touched files/commands; (2) requires that reflection to become a `store_memory` call or an explicit "no durable lesson" acknowledgment — otherwise a genuine finding just evaporates. |
| [`saltmdb-stop-retrieval-outcome-gate.py`](saltmdb-stop-retrieval-outcome-gate.py) | `Stop` / `agentStop` | Telemetry enforcement: if `search_memory` was called this turn (per the pending flag above), requires a `log_event(event_type="retrieval_outcome", ...)` call before the turn closes; nudges once, then lets it go rather than block forever. See the `saltmdb-usage` skill for the logging convention. |
| [`saltmdb-session-end-wrapup-reminder.py`](saltmdb-session-end-wrapup-reminder.py) | `SessionEnd` | One-shot reminder, at true session close (not every turn), to check `get_events` for anything durable that only exists in the ephemeral event ledger. |
| [`saltmdb-pre-compact-sweep.py`](saltmdb-pre-compact-sweep.py) | `PreCompact` (Claude-Code-only event; script itself is portable) | Standalone version of the pre-compaction sweep. Claude Code's native `"type": "agent"` PreCompact hook (see `claude-settings-example.json`) is the best mechanism where available; this script is the fallback for manual/cron invocation or harnesses without a native agent-type hook — it shells out to `claude -p` or `codex exec` since a bare script has no MCP tool context of its own. |
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

All three example configs invoke scripts as `python <path>` (never a bare `.py` path relying on
the POSIX shebang line) -- confirmed necessary, not just defensive: native Windows has no shebang
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

For a full conceptual guide, JSON schema details, and pre-tool decision protocols, read
**[AGENT_GUIDE.md §7 (Session Automation via Lifecycle Hooks)](../AGENT_GUIDE.md#7-session-automation-via-lifecycle-hooks)**.

For the usage discipline these hooks enforce (title/quality standards, search modes, the
retrieval-outcome telemetry convention), see the **`saltmdb-usage`** skill in
[`../skills/`](../skills/).
