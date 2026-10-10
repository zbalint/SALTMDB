# SPEC-DB-SNAPSHOT-CLI

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-027. Context id: `answer-side-memory-hook-2026-10-10`.
- Baseline: `4549ee5`, `./verify` exit 0 (2089 passed, 152 subtests).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, `develop`.
- Test seams: `saltmdb.db.backup.create_snapshot`, the daemon admin method `create_snapshot_now`
  through `daemon_client.call_method`, and `saltmdb.cli.main` with the `snapshot` subcommand.
- Scope (may edit): `src/saltmdb/db/backup.py`, `src/saltmdb/daemon/server.py` (one dispatch
  branch and one handler, next to `run_librarian_now`), `src/saltmdb/cli.py` (one subcommand),
  `tests/test_backup_snapshot.py` (new), `tests/test_cli.py` (additions only),
  `tests/test_daemon_server.py` (additions only), `docs/architecture.md` (one sentence).
- Does not touch: the MCP adapter and tool definitions, `daemon/dispatch.py`, `daemon/protocol.py`,
  `daemon/client.py`, `viewer/`, `hooks/`, `scripts/`, schema, `connection.py`, `docs/BACKLOG.md`.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The answer-side memory hook needs a replay on real data. The replay must run on a copy of the
database, because the live file may only be opened by the daemon. Today no sanctioned way makes a
copy: `db/backup.py:create_snapshot` has no caller, and `export-corpus-snapshot` opens the
database inside the CLI process. This slice exposes `create_snapshot` through the daemon, so the
daemon stays the only process that opens the live file, and adds one CLI command to call it.

Rejected: copying the file with `cp` (misses the WAL, can capture a torn state); a CLI that opens
the database itself (breaks the single-owner rule); a `tool_call` tool in `dispatch.py` (it would
join the MCP retry contract and the usage-telemetry wrapper, which queues a background write into
the live database for every call); an MCP tool (an agent would get a bulk-copy tool). Keeping it
off MCP limits the exposure; it does not prevent a shell-capable agent from running the CLI, and
this spec does not claim it does.

## 2. Decisions

- D1. `create_snapshot(db_path=None, dest_dir=None) -> str` returns the absolute snapshot path and
  raises on failure (`OSError`, `sqlite3.Error`, `ValueError`, `FileExistsError`). The old
  message-string return is dropped. It has no callers in `src/`, `scripts/`, `tests/` or `hooks/`.
- D2. The file name keeps `saltmdb_snapshot_<UTC %Y%m%d_%H%M%S>.db` in `dest_dir`, default
  `<db dir>/backups/`. The viewer's stats page reads this layout and its lexical order
  (`viewer/routes/stats.py` `_latest_snapshot`); both stay. A second call in the same second
  raises `FileExistsError` and the CLI exits 1; this is accepted and the name is not extended.
- D3. Order inside `create_snapshot`, fixed because cleanup deletes files:
  1. Resolve `db_path` and the target path; if equal, raise `ValueError` and write nothing.
  2. Create the directory (`os.makedirs(mode=0o700, exist_ok=True)`; the mode applies only to a
     directory this call creates, and an existing directory is never chmodded).
  3. Create the target atomically with `os.open(path, O_CREAT | O_EXCL | O_WRONLY, 0o600)` and
     close the descriptor; an existing file raises `FileExistsError` here and is left untouched.
  4. Only now enter the `try`: connect, back up with the default `pages=-1`, then set the copy's
     `PRAGMA journal_mode=DELETE` so the snapshot is one self-contained file, and close both.
  5. On any failure in step 4, remove the target and its `-journal` sidecar (files this call
     created in step 3), then re-raise. Cleanup never runs for failures in steps 1-3.
- D4. New daemon admin method `create_snapshot_now`, routed in `server.py` next to
  `run_librarian_now` (top-level method, not `tool_call`, not in `READ_TOOLS`, not in
  `DISPATCH_TABLE`). Params: optional `dest_dir`, which must be an absolute path string; anything
  else is a `ValueError` (reported through the existing `INTERNAL_ERROR` envelope). It runs
  `backup.create_snapshot(self.db_path, dest_dir)` and returns `{"path": <absolute path>}`.
  The call runs on the handler thread inside the existing in-flight accounting, so shutdown waits
  for it. # shortcut: a backup longer than the 60 s RPC timeout leaves the CLI exiting 1 and an
  orphan file; add a progress/async path only if the database outgrows that.
- D5. CLI `saltmdb-cli [--db-path P] snapshot [--dest-dir D]`:
  1. If the database file does not exist, print `# Error: no database at <path>` to stderr and
     return 1 without calling the daemon.
  2. `D` is passed through `os.path.abspath(os.path.expanduser(D))` before sending, because the
     daemon runs in another working directory.
  3. Call `daemon_client.call_method(db_path, "create_snapshot_now", params)` (default spawn).
  4. Print the returned path on stdout and return 0.
  5. Catch `Exception` from step 3, print `# Error: <message>` to stderr and return 1.
  The command never opens the database in the CLI process.
- D6. `docs/architecture.md`: one sentence naming `saltmdb-cli snapshot` as the sanctioned way to
  copy the database for offline evaluation.

## 3. Changes per file

- `backup.py`: signature, order, modes, journal mode, path return (D1-D3).
- `server.py`: one `elif method == "create_snapshot_now"` branch and `_handle_create_snapshot`
  that validates `dest_dir`, calls `backup.create_snapshot`, returns `build_ok_response`.
- `cli.py`: `cmd_create_snapshot` and a `snapshot` subparser. `main`'s exit-code tuple is not
  changed (the command returns 1 itself).

## 4. Tests

- T1. Snapshot of a temp database with known rows: the file exists at the returned absolute path,
  opens, has the same `entities` row count, has mode `0o600`, its journal mode is `delete`, and no
  `-wal` or `-shm` file is left beside it.
- T2. Rows committed before the call are present; rows of a transaction still open on another
  connection are absent.
- T3. Existing target file: `FileExistsError`; the file is still present and byte-identical.
- T4. A failure mid-copy (patched backup) removes the target and a `-journal` sidecar and
  re-raises; a failure in steps 1-3 removes nothing.
- T5. A missing `dest_dir` is created with mode `0o700`; an existing directory keeps its mode; an
  explicit `dest_dir` is honoured.
- T6. Target path equal to the live database path: `ValueError`; the database is still present
  and byte-identical.
- T7. Server: `create_snapshot_now` with an absolute `dest_dir` returns `{"path": ...}`; a relative
  `dest_dir` gives an error response; the method name is in neither `READ_TOOLS`, `WRITE_TOOLS`
  nor `DISPATCH_TABLE`.
- T8. CLI through `main()`: success prints the path and exits 0; a missing database prints
  `# Error: no database at` and exits 1 without calling the daemon; the call raising exits 1 with
  `# Error:` on stderr and empty stdout; a relative `--dest-dir` reaches the call as an absolute
  path.

## 5. Out of scope

The replay harness, removing later entities from the copy, retention or pruning of old snapshots,
scheduling, restore, encryption, an MCP tool, a `tool_call` entry, microsecond file names.

## 6. Acceptance

1. `.venv/bin/python -m pytest tests/test_backup_snapshot.py tests/test_cli.py tests/test_daemon_server.py -q` green.
2. `rg -n "create_snapshot" src/saltmdb --glob '*.py'` lists only `backup.py`, `server.py`, `cli.py`.
3. `rg -n "sqlite3|open_read_connection|get_connection" src/saltmdb/cli.py` has no match from this
   change (current baseline: no match).
4. `./verify` exit 0, one full run.
5. `git status --short` lists only the files in section 0 scope.

## 7. Pre-lock gate notes

Run before locking, 2026-10-10:

- Callers grep for `create_snapshot` over `src/`, `scripts/`, `tests/`, `hooks/` and docs: no
  caller; mentions only in `MIGRATION.md`, a `build_diverse_test_db.py` docstring and the viewer's
  directory-layout reader (kept by D2).
- Probe on an isolated temp database (not the live one): `Connection.backup(pages=-1)` from a
  read-only connection to a WAL database into a file created with `O_EXCL` mode `0o600`, then
  `PRAGMA journal_mode=DELETE` on the copy, leaves one self-contained `0o600` file with journal
  mode `delete`.
- `READ_TOOLS` and `DISPATCH_TABLE` are only tested by membership, so the new method name in
  neither breaks no test. The handler seam for T7 is `handle_request` in
  `tests/test_daemon_server.py`.
- The consultant review changed the design (admin method instead of `tool_call`, the D3 order,
  absolute `dest_dir`, the missing-database check, journal mode); the changes were applied and
  D1-D5 re-read against each other. No second review pass.
- Expected-new-code checks (acceptance 1-2) run after implementation.
