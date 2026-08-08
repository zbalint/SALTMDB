# SALTMDB Installation Guide

Follow these steps to set up and configure the SALTMDB Model Context Protocol (MCP) server on your local machine.

---

## 1. Prerequisites

* **Python:** Version 3.10 or higher.
* **pip:** Python package manager.
* **SQLite:** Standard library dependency (pre-bundled with Python).

---

## 2. Dependencies

Install all dependencies (including `mcp`, `sqlite-vec`, and `fastembed`) via editable install from the repo root:

```bash
pip install -e .
```

This installs:
- `mcp` — Model Context Protocol JSON-RPC layer
- `sqlite-vec` — SQLite extension for `vec0` vector tables
- `fastembed` — Lightweight ONNX embedding runtime (no PyTorch); uses `onnxruntime` internally

*(Optional)* If you prefer a virtual environment:
```bash
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Unix:
source .venv/bin/activate
pip install -e .
```

### Environment Variables

- `SALTMDB_DB_PATH`: Custom path to the SQLite database file (default: `~/.saltmdb/saltmdb.db`).
- `SALTMDB_ENABLE_SEMANTIC`: Hybrid FTS5 + Dense Vector RRF search is enabled by default (`true`). Set to `false` (or `0`/`off`/`no`) to disable vector search -- note that with it disabled, `search_memory` calls that pass `query_keywords` return an error (`[{"error": "..."}]`) rather than falling back to FTS-only results; filter/tag-only browsing without `query_keywords` still works.
- `SALTMDB_VIEWER_PORT`: Custom port for the database dashboard viewer (default: `8080`), read when the backend daemon starts the in-process viewer thread (see §5 — the `saltmdb-viewer` CLI no longer takes a `--port` flag).
- `SALTMDB_VIEWER_HOST`: **Currently not consumed.** As of the Track B backend-daemon rework, the daemon binds the viewer directly to `127.0.0.1` (`src/saltmdb/daemon/server.py`) regardless of this variable — a known gap introduced by that change, not yet wired through. Loopback-only is the safe default in the meantime; there is no supported way to expose the viewer on the local network right now.
- `SALTMDB_VIEWER_ENABLED`: Set to `false` (or `0`/`off`/`no`) to disable the backend daemon's in-process web viewer thread (default: `true`).
- `SALTMDB_DISABLE_LIBRARIAN`: Set to any non-empty value to suppress all Librarian maintenance-pass triggers (runs in-daemon as of the Track B backend-daemon rework; useful for debugging or controlled environments).
- `SALTMDB_TEST_MODE`: Set to any non-empty value in automated test environments to suppress Librarian maintenance-pass triggers without affecting other behavior.

> **Mechanical Text Quality Gate & Store-Time Disposition:** All writes (`store_memory`) and merges (`commit_consolidation`) undergo sub-millisecond multi-stage pre-embedding quality evaluation (idempotent auto-formatting, prose extraction, Shannon character entropy bounds \[2.5, 5.3\], Word 3-gram/5-gram sequence repetition, Type-Token Ratio, Coleman-Liau readability bounds \[2.0, 26.0\], and MSDI structural density scoring) and Stage A SHA-256 exact hash deduplication before ONNX embedding execution. As of the memory-core rework (Track A, `v0.1.0-alpha.71`), a synchronous preflight then runs on every brand-new `store_memory` write: a candidate at $\ge 0.75$ cosine similarity with compatible `memory_type`/`scope` is flagged only if it also shows correction language, crosses the stricter $\ge 0.85$ duplicate band, or is a stale consolidated node — flagging returns a `REVIEW_REQUIRED` response instead of writing, requiring the caller to resolve the candidate and resubmit with a `review_token`. This replaced the older behavior (reviewable `supersession_candidate` event logging plus automatic `similar_to` auto-linking above $\ge 0.85$) entirely — see `AGENT_GUIDE.md`'s `store_memory` entry for the full disposition flow.

> **Note on bundled model:** The `BAAI/bge-small-en-v1.5` ONNX model weights (~66 MB) are pre-bundled directly within the `saltmdb` package for offline execution out of the box. If bundled model files are missing or modified, `fastembed` will fall back to downloading them from Hugging Face automatically.

---

## 3. Database Configuration

By default, SALTMDB initializes and stores the SQLite database in a centralized folder under your user home directory:
* **Default Path:** `~/.saltmdb/saltmdb.db`

### Environment Override
To point the server to a different database path, set the `SALTMDB_DB_PATH` environment variable:
* **Windows (PowerShell):** `$env:SALTMDB_DB_PATH="C:\custom\path\memory.db"`
* **Unix:** `export SALTMDB_DB_PATH="/custom/path/memory.db"`

---

## 4. Registering the MCP Server

> [!IMPORTANT]
> MCP clients launch server processes in a minimal environment and **do not inherit your terminal's PATH**. Using bare `python` or `saltmdb-server` often fails silently. Always use the **full absolute path** to your Python executable.

### Step 1 — Find your Python path

Run this in the terminal where you installed saltmdb:

```bash
# On Windows (PowerShell):
python -c "import sys; print(sys.executable)"

# On macOS / Linux:
python3 -c "import sys; print(sys.executable)"
```

Copy the output path (e.g. `C:\Users\you\AppData\Local\Python\python.exe` or `/home/you/.venv/bin/python`).

---

### A. Google Antigravity CLI (`agy`)
MCP server configuration directory: `~/.gemini/antigravity-cli/mcp/saltmdb/` (or global `~/.gemini/antigravity-cli/settings.json`).

```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Python\\python.exe",
      "args": ["-m", "saltmdb"]
    }
  }
}
```

Replace `C:\\Users\\YOU\\AppData\\Local\\Python\\python.exe` with your own path from Step 1.
Use double backslashes (`\\`) on Windows.

**Alternative — point directly to the launch script** (if running directly from source tree without package installation):
```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Python\\python.exe",
      "args": ["C:\\path\\to\\SALTMDB\\saltmdb_server.py"]
    }
  }
}
```

---

### B. Claude Desktop
Config location:
* **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
* **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the following to the `mcpServers` block (replace paths with your own from Step 1):

**Windows:**
```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Python\\python.exe",
      "args": ["-m", "saltmdb"]
    }
  }
}
```

**macOS / Linux:**
```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "/home/you/.venv/bin/python",
      "args": ["-m", "saltmdb"]
    }
  }
}
```

> [!TIP]
> If `-m saltmdb` still fails (e.g. `ModuleNotFoundError`), switch to the script path approach:
> `"args": ["/path/to/SALTMDB/saltmdb_server.py"]`

---

## 5. Running the Database Viewer

As of the Track B backend-daemon rework, the web dashboard is no longer something you separately start — it runs as an in-process thread inside the single backend daemon, and comes up automatically as soon as any MCP client causes the daemon to spawn (gated by `SALTMDB_VIEWER_ENABLED`, default on). `saltmdb-viewer` is now a read-only status check, not a launcher: it reports whether a daemon is running for the resolved DB path and whether its viewer thread is up, or a clear "no daemon running" message if not (it does **not** spawn one itself, and takes no `--port` or other flags):

```bash
# If installed via pip install -e .:
saltmdb-viewer
# Or directly:
python -m saltmdb.viewer.server
```

The daemon reads `SALTMDB_VIEWER_PORT` (default `8080`) when it starts the viewer thread — set it before the daemon first spawns, not per `saltmdb-viewer` invocation.

Once a daemon is running with the viewer enabled, open your browser and navigate to:
👉 **[http://localhost:8080](http://localhost:8080)**

---

## 6. Verification & Tests

To verify that the database schemas, triggers, and lock rules operate correctly, run the unified unit tests:

```bash
python -m unittest discover tests
```

---

## 7. Troubleshooting & Logs

Since MCP servers run over standard I/O, error output is consumed by the client host. Check the client log files directly to debug connection and python runtime issues:

* **Claude Desktop Log Paths:**
  * **Windows:** `%APPDATA%\Claude\logs\mcp.log` and `%APPDATA%\Claude\logs\mcp-server-saltmdb.log`
  * **macOS/Linux:** `~/Library/Logs/Claude/mcp.log` and `~/Library/Logs/Claude/mcp-server-saltmdb.log`
* **Google Antigravity CLI Logs:**
  * View task logs inside the conversation folder: `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/tasks/`

---

## 8. Setting Up Lifecycle Hooks for Agent Automation

To maximize reliability, SALTMDB supports automated lifecycle hooks for **Claude Code**, **Google Antigravity CLI (`agy`)**, and **GitHub Copilot CLI**.

Lifecycle hooks automate:
1. **Context Bootstrap:** Automatically inject core rules and project memory digests into your session on start.
2. **Pre-Action Enforcement:** Require memory searches (`search_memory`) before executing code edits or shell commands.
3. **Pre-Compaction Memory Sweeps:** Preserve unpersisted architectural decisions and bug fixes before conversation truncation.
4. **Post-Turn Quality Self-Critique:** Mandatory reflection on confidence and unknown risks before finishing complex turns.

### Quick Setup Summary

- **Claude Code:** Add hook definitions to `~/.claude/settings.json` pointing to scripts in `~/.claude/hooks/`.
- **Google Antigravity CLI (`agy`):** Configure `PreInvocation` and `PreToolUse` hooks in `~/.gemini/antigravity-cli/settings.json`.
- **GitHub Copilot CLI:** Add `.github/hooks/saltmdb.json` or `~/.copilot/hooks/saltmdb.json` using the `preToolUse` permission JSON protocol (`{"permissionDecision": "allow" | "deny"}`).

👉 **For complete script source listings and JSON configurations, check the [`examples/hooks/`](examples/hooks/) directory and refer to [AGENT_GUIDE.md §7 (Session Automation via Lifecycle Hooks)](AGENT_GUIDE.md#7-session-automation-via-lifecycle-hooks)**.

