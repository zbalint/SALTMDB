# SALTMDB: Local-First MCP Memory Server

**SALTMDB** (Short And Long-Term Memory DataBase) is a centralized, local-first memory framework designed for AI CLI tools and agents (such as Antigravity, Copilot, and Claude Code). It acts as a shared memory layer, allowing multiple concurrent agents to read, write, and consolidate contextual facts using a lightweight Python package with SQLite and ONNX-based vector embeddings.

> [!TIP]
> * **Installation:** To install and register the MCP server, see the **[Installation Guide](INSTALL.md)**.
> * **Developer Guide:** To learn how to configure your AI agents to utilize this memory system, read the **[Agent Integration & Design Guide](AGENT_GUIDE.md)**.
> * **Architecture:** For the full technical design — storage engine, daemon model, search pipeline, quality gate, and core-memory governance — see **[docs/architecture.md](docs/architecture.md)**.

---

## 🚀 Core Features

* **Hybrid FTS5 + Vector Search:** Parallel BM25 keyword search and `BAAI/bge-small-en-v1.5` dense vector search, fused via weighted Reciprocal Rank Fusion, with an optional ONNX cross-encoder final-reranker stage.
* **Mechanical Text Quality Gate & Duplicate Handling:** Sub-millisecond multi-stage pre-embedding quality evaluation plus SHA-256 exact-hash and cross-encoder near-duplicate detection on every write.
* **Secrets Redaction:** Built-in regex scrubbing pipeline automatically redacts API keys, tokens, and private paths before any write. Custom patterns can be added via `.saltmdb_redact` in the working directory.
* **Folksonomy & Canonical Tags:** Flexible tagging with alias resolution, canonical redirects, and write-time normalization to prevent fragmentation.
* **Immutable Identity & Lineage:** Every memory's `entity_id` is permanent — a genuine content change (`revise_memory`/`supersede_memory`) always creates a new entity linked back to its predecessor (`revises`/`supersedes`), never mutated in place.
* **Lossless Consolidation & Bi-Temporal Relations:** Synthesis soft-archives sources and never hard-deletes; relation edges carry independent system-time and event-time axes.
* **Single-Owner Backend Daemon (memory-core rework, Track B):** Exactly one background daemon process opens SQLite for a given DB path; every MCP client and CLI entrypoint connects as a thin RPC adapter, with the Librarian and web viewer running in-process inside it.
* **Automated Session Lifecycle Hooks:** Native integration with Claude Code, Google Antigravity CLI, and GitHub Copilot CLI session hooks — context-digest injection at startup, a pre-action memory search gate, pre-compaction memory sweeps, and a stop-time self-critique gate.
* **Core-Memory Bootstrap Governance:** A scarce, capacity-capped (`≤5` active, `≤2,500` chars each) bootstrap-delivery mechanism for urgent cross-session hazards, distinct from ordinary searchable memory.

The server exposes **19 MCP tools** over stdio. Per this project's own design principle, SALTMDB is meant to be usable from those tool descriptions alone — read them directly from your MCP client rather than a hand-maintained duplicate here (source of truth: `src/saltmdb/mcp/tools.py`).

See **[docs/architecture.md](docs/architecture.md)** for the full technical detail behind every feature above, including the database schema, the daemon's process model, and the quality-gate/core-governance rules in full.

---

## ⚙️ Configuration & Installation

See the **[Installation Guide](INSTALL.md)** for prerequisites, environment variables, MCP client registration, the database dashboard viewer, and running tests.

---

## 📄 License & Community

* **License:** Distributed under the **[GNU Affero General Public License v3 (AGPLv3)](LICENSE)**.
* **Contributing:** Read the **[Contributing Guidelines](CONTRIBUTING.md)** for details on testing and branch setups.
* **Conduct:** We adhere to the **[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md)**.
