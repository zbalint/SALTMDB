# SALTMDB Lifecycle Hooks Reference & Examples

This directory contains production-ready reference hook scripts and harness configuration templates to automate **SALTMDB** operations across AI agent environments (**Claude Code**, **Google Antigravity CLI**, and **GitHub Copilot CLI**).

---

## 📁 Included Reference Files

### Configuration Templates
| File | Host Harness | Description |
| :--- | :--- | :--- |
| [`claude-settings-example.json`](claude-settings-example.json) | Claude Code | Global settings template for `~/.claude/settings.json` specifying `SessionStart`, `PreToolUse`, `PreCompact`, and `Stop` hooks. |
| [`antigravity-settings-example.json`](antigravity-settings-example.json) | Antigravity CLI (`agy`) | Workspace or global settings template for `~/.gemini/antigravity-cli/settings.json` specifying `PreInvocation` and `PreToolUse` hooks. |
| [`copilot-hooks-example.json`](copilot-hooks-example.json) | GitHub Copilot CLI | Spec file template for `.github/hooks/saltmdb.json` specifying `sessionStart`, `preToolUse`, and `agentStop` hooks. |

### Shell Scripts
| File | Hook Event | Description |
| :--- | :--- | :--- |
| [`saltmdb-session-bootstrap.sh`](saltmdb-session-bootstrap.sh) | `SessionStart` / `PreInvocation` / `sessionStart` | Extracts `cwd` from `stdin` JSON, determines project keywords, and runs `saltmdb-cli bootstrap-digest` to auto-inject core persona rules and project memory digests into context. |
| [`saltmdb-pre-action-gate.sh`](saltmdb-pre-action-gate.sh) | `PreToolUse` | Enforces Rule 1 ("Think Before You Leap") by checking the session transcript and denying code edit / bash execution until at least one `search_memory` call is logged. |
| [`saltmdb-copilot-pre-tool.sh`](saltmdb-copilot-pre-tool.sh) | `preToolUse` (Copilot CLI) | Intercepts tool calls in GitHub Copilot CLI, checking session transcript history and outputting JSON permission decisions (`{"permissionDecision": "allow"}` or `"deny"`) on `stdout`. |
| [`saltmdb-self-critique-gate.sh`](saltmdb-self-critique-gate.sh) | `Stop` / `agentStop` | Triggers a mandatory 2-question quality self-reflection before closing turns that involved code or file modifications. |

---

## 🚀 Quick Setup Instructions

### 1. Claude Code
1. Copy script files to `~/.claude/hooks/`:
   ```bash
   mkdir -p ~/.claude/hooks
   cp saltmdb-*.sh ~/.claude/hooks/
   chmod +x ~/.claude/hooks/*.sh
   ```
2. Merge the configuration snippet from [`claude-settings-example.json`](claude-settings-example.json) into your `~/.claude/settings.json`.

---

### 2. Google Antigravity CLI (`agy`)
1. Place scripts in `$HOME/.mcp/SALTMDB/examples/hooks/` or a custom directory in your `$PATH`.
2. Add the configuration block from [`antigravity-settings-example.json`](antigravity-settings-example.json) to `~/.gemini/antigravity-cli/settings.json`.

---

### 3. GitHub Copilot CLI
1. Copy hook scripts to `~/.copilot/hooks/` (or repository `.github/hooks/`).
2. Add `.github/hooks/saltmdb.json` using [`copilot-hooks-example.json`](copilot-hooks-example.json) as a reference template.

---

## 📚 Detailed Documentation

For a full conceptual guide, JSON schema details, and pre-tool decision protocols, read **[AGENT_GUIDE.md §7 (Session Automation via Lifecycle Hooks)](../../AGENT_GUIDE.md#7-session-automation-via-lifecycle-hooks)**.
