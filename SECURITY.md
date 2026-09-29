# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately through GitHub's **[Private Vulnerability Reporting](https://github.com/zbalint/SALTMDB/security/advisories/new)** feature, rather than opening a public issue. This lets us confirm and fix the problem before it's publicly disclosed.

Include as much detail as you can:

* A description of the vulnerability and its potential impact.
* Steps to reproduce it (a minimal repro, if possible).
* The affected version(s) (see `pyproject.toml`'s `version` field, or `SALTMDB_DB_PATH`'s schema `PRAGMA user_version` if relevant).

## Scope

SALTMDB is a local-first MCP memory server: it runs as a local process/daemon and stores data in a local SQLite database. Reports of particular interest include:

* Ways the built-in secrets-redaction middleware (see `docs/architecture.md`) fails to catch a credential pattern it's supposed to catch.
* Ways one agent/adapter could read or write another owner's `private`-scoped memories.
* `scope='private'` is a visibility convenience, not access control: any client attached to the same database can read `private` records by configuring the same `SALTMDB_AGENT_ID`.
* Any path that could execute arbitrary code from untrusted memory content (e.g. via a viewer route or a stored payload).
* Ways conversation-trace capture could store data when it is disabled, or let a caller forge which agent session a trace belongs to (`agent_session_id` is bound from the adapter's own identity, never caller-supplied).

## Conversation traces: what they store

Conversation-trace capture is **opt-in** (`SALTMDB_TRACE_CAPTURE_ENABLED`, default off; the MCP adapter gates the `capture_trace_*` tools). When enabled, SALTMDB stores each turn's user prompt, any messages the user sent mid-turn, and the final assistant message **verbatim and untruncated** in the local SQLite database. Unlike memories, these are not passed through a quality gate, and prompts may contain anything you typed, including pasted secrets.

* **Traces are cross-agent.** `agent_id` on a trace records which agent wrote it; it is attribution, not access control. Any MCP client attached to the same database can read any trace via `search_traces` / `get_trace`, and the web viewer shows them to anyone who can reach it (loopback-only, `127.0.0.1`).
* **Trace text is untrusted historical data.** It is returned flagged (`content_is_untrusted_historical_data`) and injected into the session-start handover with a warning; an agent must never treat it as instructions.
* Treat the database file like a conversation log: back it up and share it accordingly. Leave capture off, or omit the capture hooks, if that is not acceptable.

## Response

This project is currently in active pre-1.0 development (`0.x` alpha versions, working toward a beta release). There is no fixed SLA for a response, but reports will be acknowledged and triaged as soon as possible.
