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
- `SALTMDB_ENABLE_SEMANTIC`: Set to `true` (or `1`) to enable Hybrid FTS5 + Dense Vector RRF search (default: `false`, read-path only).

> **Note on first run:** When `store_memory` is first called, `fastembed` automatically downloads the `BAAI/bge-small-en-v1.5` ONNX model weights (~130 MB) from Hugging Face and caches them locally. Internet access is required on first use only.

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

Depending on the development client you are using, add the server registration to the client's configuration file.

### A. Google Antigravity CLI (`agy`)
Global MCP config location: `~/.gemini/config/mcp_config.json` (Create this file if it does not exist).

Add the following config:
```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "python",
      "args": [
        "/path/to/SALTMDB/saltmdb_server.py"
      ]
    }
  }
}
```

### B. Claude Desktop
Config location:
* **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
* **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add the following config to the `mcpServers` block:
```json
{
  "mcpServers": {
    "saltmdb": {
      "command": "python",
      "args": [
        "/path/to/SALTMDB/saltmdb_server.py"
      ]
    }
  }
}
```

---

## 5. Running the Database Viewer

Start the local dashboard to monitor events, tag relations, and locks in your web browser:

```bash
python saltmdb_viewer.py
```

Open your browser and navigate to:
👉 **[http://localhost:8080](http://localhost:8080)**

---

## 6. Verification & Tests

To verify that the database schemas, triggers, and lock rules operate correctly, run the unified unit tests:

```bash
# On Windows:
$env:PYTHONPATH="C:\path\to\SALTMDB"
python scratch/test_db.py

# On Unix:
PYTHONPATH="." python scratch/test_db.py
```

---

## 7. Troubleshooting & Logs

Since MCP servers run over standard I/O, error output is consumed by the client host. Check the client log files directly to debug connection and python runtime issues:

* **Claude Desktop Log Paths:**
  * **Windows:** `%APPDATA%\Claude\logs\mcp.log` and `%APPDATA%\Claude\logs\mcp-server-saltmdb.log`
  * **macOS/Linux:** `~/Library/Logs/Claude/mcp.log` and `~/Library/Logs/Claude/mcp-server-saltmdb.log`
* **Google Antigravity CLI Logs:**
  * View task logs inside the conversation folder: `~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/tasks/`
