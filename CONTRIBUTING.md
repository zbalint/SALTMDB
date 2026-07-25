# Contributing to SALTMDB

We welcome contributions from the open-source community! Follow these steps to set up your environment, write tests, and submit your pull requests.

---

## 1. Local Development Setup

1. **Fork the Repository:** Create your own fork of this repository on your version control hosting provider.
2. **Clone locally:**
   ```bash
   git clone https://github.com/your-username/SALTMDB.git
   cd SALTMDB
   ```
3. **Set up Virtual Environment:**
   ```bash
   python -m venv .venv
   # On Windows (PowerShell):
   .venv\Scripts\Activate.ps1
   # On Unix:
   source .venv/bin/activate
   ```
4. **Install Dependencies:**
   ```bash
   pip install -e .
   ```

---

## 2. Writing Code & Guidelines

* **Preserve Docstrings:** Maintain code documentation, type hints, and comment structures where possible.
* **Database Safety:** Ensure that all changes to database schemas or routines do not disrupt sqlite3 concurrency features (WAL mode, transactions, timeout structures).
* **Minimal External Dependencies:** Only add third-party packages when strictly necessary and when they ship prebuilt wheels for all supported platforms (Windows, Linux, macOS). New dependencies must be justified in the PR description.

---

## 3. Testing Changes

Every modification must pass the unit test suite before submission.

1. Set the PYTHONPATH environment variable:
   ```bash
   # On Windows:
   $env:PYTHONPATH="src"
   # On Unix:
   export PYTHONPATH="src"
   ```
2. Run the unit test suite (matches the command CI enforces in `.github/workflows/python-tests.yml`):
   ```bash
   python -m unittest discover -s tests
   ```
3. Inspect and verify the live outputs inside the local browser viewer by running:
   ```bash
   python -m saltmdb.viewer.server
   ```

---

## 4. Documentation Sync Checklist (Version Bumps & Signature Changes)

Docs and skill files here don't auto-sync with the code or with each other — a 2026-07-25 audit found `MIGRATION.md` and `MULTI_AGENT_ORCHESTRATION.md` had both drifted silently for multiple releases before anyone noticed. If your change touches any of the following, check the matching box before opening a PR:

* [ ] **Bumped `pyproject.toml` version, OR changed the `entities`/`events` schema, OR changed a public MCP tool's parameters** → add a row to `MIGRATION.md`'s Version Schema Registry describing the change and required migration action (or explicitly note "No Action Required").
* [ ] **Changed a public MCP tool's name, parameters, or return shape** (anything in `src/saltmdb/mcp/tools.py`) → update the matching entry in `AGENT_GUIDE.md` §2 (Available Tools Overview) so the documented signature still matches the code.
* [ ] **Changed the Core Operating Commandments list, the multi-agent orchestration protocol, or the worker prompt template** → these exist as *two independent copies* with no auto-sync: the repo root (`ORCHESTRATOR.md`, `MULTI_AGENT_ORCHESTRATION.md`, `WORKER_TEMPLATE.md`) and the bundled skill copies (`~/.claude/skills/saltmdb-subagent-orchestration/references/`). Update both, or explicitly note in the PR which one is the source of truth and that the other needs a manual follow-up.

---

## 5. Submitting Pull Requests

1. **Create a Feature Branch:** Choose a descriptive name (e.g. `feature/add-log-rotation`).
2. **Commit clearly:** Follow standard commit message guidelines detailing *why* the change was made.
3. **Open a PR:** Describe your implementation decisions and link any related issues.
