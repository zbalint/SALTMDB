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
| [`saltmdb-session-start-bootstrap.py`](saltmdb-session-start-bootstrap.py) | `SessionStart` / `PreInvocation` / `sessionStart` | Injects the canonical core-memory bootstrap digest (`saltmdb-cli bootstrap-digest`), plus a nudge if any core memory is overdue for review (`saltmdb-cli corpus-health`). |
| [`saltmdb-pre-tool-search-gate.py`](saltmdb-pre-tool-search-gate.py) | `PreToolUse` / `preToolUse` | Enforces Rule 1 ("Think Before You Leap"): denies a risky edit/bash/file-write call until `search_memory` has been called this session. Does its own read-only-tool check internally (needed for Copilot, whose `preToolUse` fires unfiltered) — replaces the old separate Copilot-only pre-tool script, which reimplemented the same decision logic with drift risk. |
| [`saltmdb-post-tool-response-nudges.py`](saltmdb-post-tool-response-nudges.py) | `PostToolUse` on `store_memory`/`search_memory` | Inspects the tool *response*, not just the tool name: nudges on unacted `duplicate_candidates`, a `store_memory` with no follow-up `manage_relation`, and an empty `mode="strict"` result. |
| [`saltmdb-post-tool-failure-circuit-breaker.py`](saltmdb-post-tool-failure-circuit-breaker.py) | `PostToolUse` on `log_event` | Fingerprints repeated `log_event(event_type="issue")` calls sharing an `error_code`; nudges CLAUDE.md rule 2 (stop after 2 consecutive failures, search memory, replan) instead of relying on the agent remembering it mid-loop. |
| [`saltmdb-stop-critique-gate.py`](saltmdb-stop-critique-gate.py) | `Stop` / `agentStop` | Two-stage gate: (1) mandatory 2-question self-reflection before closing a turn that touched files/commands; (2) requires that reflection to become a `store_memory` call or an explicit "no durable lesson" acknowledgment — otherwise a genuine finding just evaporates. |
| [`saltmdb-stop-retrieval-outcome-gate.py`](saltmdb-stop-retrieval-outcome-gate.py) | `Stop` / `agentStop` | Telemetry enforcement: if `search_memory` was called this turn, requires a `log_event(event_type="retrieval_outcome", ...)` call (or an explicit opt-out) before the turn closes. See the `saltmdb-usage` skill for the logging convention. |
| [`saltmdb-session-end-wrapup-reminder.py`](saltmdb-session-end-wrapup-reminder.py) | `SessionEnd` | One-shot reminder, at true session close (not every turn), to check `get_events` for anything durable that only exists in the ephemeral event ledger. |
| [`saltmdb-pre-compact-sweep.py`](saltmdb-pre-compact-sweep.py) | `PreCompact` (Claude-Code-only event; script itself is portable) | Standalone version of the pre-compaction sweep. Claude Code's native `"type": "agent"` PreCompact hook (see `claude-settings-example.json`) is the best mechanism where available; this script is the fallback for manual/cron invocation or harnesses without a native agent-type hook — it shells out to `claude -p` or `codex exec` since a bare script has no MCP tool context of its own. |

**Explicit non-goal**: no hook here nudges or automates `consolidate_memories`. Deciding which
memories are cohesive enough to merge stays a deliberate agent judgment call.

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

## 📚 Detailed Documentation

For a full conceptual guide, JSON schema details, and pre-tool decision protocols, read
**[AGENT_GUIDE.md §7 (Session Automation via Lifecycle Hooks)](../AGENT_GUIDE.md#7-session-automation-via-lifecycle-hooks)**.

For the usage discipline these hooks enforce (title/quality standards, search modes, the
retrieval-outcome telemetry convention), see the **`saltmdb-usage`** skill in
[`../skills/`](../skills/).
