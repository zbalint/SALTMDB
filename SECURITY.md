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
* Any path that could execute arbitrary code from untrusted memory content (e.g. via a viewer route or a stored payload).

## Response

This project is currently in **beta** (`0.x` versions). There is no fixed SLA for a response, but reports will be acknowledged and triaged as soon as possible.
