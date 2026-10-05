# AGENTS.md

Conventions for anyone (human or agent) changing this repository. Design lives in `docs/`; read
`docs/architecture.md` before touching a module. Coding rules are in
[CONTRIBUTING.md](CONTRIBUTING.md) §2.1 and the tooling gates in §4; this file does not repeat them.

## Commands

Run before calling a change done; it must exit 0:

```sh
./verify    # ruff check, ruff format --check, mypy, bandit, pip-audit, deptry, pytest tests/ hooks/tests/
```

- Use the repo `.venv` (`uv sync --extra dev`), never the system Python.
- Other processes may run test suites on the same host. Do not start parallel full runs.

## Database safety

- Never open `saltmdb.db` with `sqlite3`, a DB browser or an ad hoc script, not even read-only. The
  backend daemon is the only process that opens it. Use the MCP tools, `saltmdb-cli` or the viewer.
- Always parameterized queries; subprocesses as argument lists, never shell strings.

## Modules

| Path | Owns |
| --- | --- |
| `src/saltmdb/daemon/` | The single-owner backend daemon: the only process that opens the DB, plus its RPC protocol and client |
| `src/saltmdb/db/` | Schema, connections, sessions, backup and vector schema |
| `src/saltmdb/domain/services/` | Memory, retrieval, digest and other business logic |
| `src/saltmdb/mcp/` | The MCP server and tool definitions (a thin adapter over the daemon) |
| `src/saltmdb/viewer/` | The web viewer |
| `src/saltmdb/models/` | Bundled embedding and reranker models |
| `hooks/` | Agent lifecycle hooks and their example settings |
| `skills/` | Agent skills shipped with the project |
| `scripts/` | Benchmarking and diagnostics |

## Docs layout

| Where | What |
| --- | --- |
| `docs/architecture.md` | The design. Decisions live here, not in the backlog |
| `docs/BACKLOG.md` | Open items with stable `BL-nnn` ids |
| `docs/specs/` | Every spec and plan: one locked `SPEC-<NAME>.md` per implementation slice (start from `TEMPLATE.md`), and the plans that precede them |
| Repo root | `README`, `INSTALL`, `MIGRATION`, `SECURITY`, `CONTRIBUTING`, `CODE_OF_CONDUCT` |

`reports/`, `plans/` and `scratch*/` are local and gitignored.

## Public repository rules

- Never commit SALTMDB memory content: titles, excerpts, verbatim quotes, or ids and session ids
  copied from memories. This covers specs, backlog rows, commit messages and PR descriptions.
- No personal data, credentials or real hostnames. Use fictional examples.
- When cutting a corner on purpose, leave a `# shortcut:` comment naming the ceiling and the
  upgrade trigger.
