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
   $env:PYTHONPATH="."
   # On Unix:
   export PYTHONPATH="."
   ```
2. Run the hybrid search test suite:
   ```bash
   python -m pytest scratch/test_hybrid_search.py -v
   ```
3. Inspect and verify the live outputs inside the local browser viewer by running:
   ```bash
   python -m saltmdb.viewer.server
   ```

---

## 4. Submitting Pull Requests

1. **Create a Feature Branch:** Choose a descriptive name (e.g. `feature/add-log-rotation`).
2. **Commit clearly:** Follow standard commit message guidelines detailing *why* the change was made.
3. **Open a PR:** Describe your implementation decisions and link any related issues.
