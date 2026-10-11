# SPEC-CORPUS-HEALTH-CORRECTIONS

## 0. Status

LOCKED 2026-10-11.

- Backlog: BL-039 (new, added by this slice). Shared `context_id`:
  `corpus-health-corrections-2026-10-11`.
- Baseline: `develop` at the commit that contains this spec; `./verify` exit 0 (record counts).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `saltmdb.cli.cmd_corpus_health` against a real `init_db()` temp database (the
  existing `TestCorpusHealthCli` pattern in `tests/test_cli.py`); no mocks.
- Scope (may edit): `src/saltmdb/cli.py` (`_corpus_health_data` only), `tests/test_cli.py` (new
  tests inside `TestCorpusHealthCli` only), `docs/architecture.md` (one sentence at the
  `corpus-health` mention, section 6 area), `docs/BACKLOG.md`.
- Does not touch: the daemon, MCP tools, hooks (the session-start hook reads only
  `overdue_core_reviews`), the schema, any other CLI command.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Nobody can tell whether the memory corpus is going stale: memories are written less often and
corrected rarely. `saltmdb-cli corpus-health` has no signal for corrections or freshness, and the
corpus export carries no `parent_ids`, so the correction rate cannot be measured from outside. The
data exists: `revise_memory` and `supersede_memory` each write one `revises` or `supersedes` edge
from the new entity to the old one (they create a new entity, they do not edit in place), and
agents link `corrects` edges to inactive memories. On a 2026-10-10 snapshot the `relations` table
held 309 `revises`, 311 `supersedes` and 262 `corrects` edges, with ISO-8601 UTC `created_at`
timestamps (`2026-10-10T18:57:17.215486+00:00`), and 864 of 2170 non-archived entities had
`updated_at` after 2026-09-10.

The report stays a CLI read, never an MCP tool, with no new persisted state (the existing design of
`_corpus_health_data`): the agent runs `saltmdb-cli corpus-health`, no SQL on its side.

Rejected: a new persisted counter table or events (new state to keep in sync); a weekly cron
report (needs the owner's explicit request); a staleness verdict or threshold (this slice reports
numbers only).

## 2. Decisions

- D1. `_corpus_health_data` returns one new top-level key `"corrections"` and one new top-level key
  `"freshness"`. No existing key changes.
- D2. `corrections` is computed over the existing `--days` window (`days` argument, default 7) with
  the same Python cutoff the telemetry block uses (`(now(UTC) - timedelta(days=days)).isoformat()`,
  compared to the stored ISO strings; never a SQLite `datetime('now')`):
  `{"note": str, "window_days": days, "revises": int, "supersedes": int, "corrects": int,
  "total": int, "memories_created": int, "by_week": [...]}`.
  - `revises`/`supersedes`/`corrects`: count of `relations` rows with that predicate and
    `created_at >= cutoff`, regardless of `valid_to` (a correction stays a correction after the edge
    is later closed).
  - `total` is the sum of the three.
  - `memories_created`: count of `entities` with `created_at >= cutoff`, all statuses. The note
    says revisions and supersessions create new entities, so they are included here.
  - `by_week`: a list of `{"week_start": "YYYY-MM-DD", "corrections": int, "memories_created": int}`
    ordered oldest first, one item per ISO week (Monday, UTC) from the week containing the cutoff
    to the current week, including weeks with zero counts. Bucketed in Python from the fetched
    `created_at` strings (parse with `datetime.fromisoformat`; skip a row whose value does not
    parse, and count skipped rows in a `"unparsed_rows"` integer on the block). `days` is clamped to
    at most 365 for this block only so `by_week` has at most 54 items; `window_days` reports the
    requested value.
- D3. `freshness`: `{"note": str, "window_days": 30, "active": int, "updated_in_window": int,
  "share": float | None}` where `active` is entities with `status != 'archived'`,
  `updated_in_window` those among them with `updated_at >= now-30d` (same cutoff style, fixed 30
  days, independent of `--days`), and `share` is `round(updated/active, 3)` or `None` when
  `active == 0`. The note says `updated_at` changes on write and revise, not on read.
- D4. `days <= 0`: counts are zero and `by_week` is an empty list (the existing test already uses
  `days=0` for telemetry; keep that behaviour consistent).
- D5. No new CLI flags, no output format change beyond the two keys.

## 3. Changes per file

- `src/saltmdb/cli.py`: in `_corpus_health_data`, after the telemetry computations and before the
  return dict, add the queries (parameterized; two `SELECT predicate, created_at FROM relations
  WHERE predicate IN ('revises','supersedes','corrects') AND created_at >= ?` and `SELECT created_at
  FROM entities WHERE created_at >= ?` fetched and aggregated in Python for both totals and
  `by_week`, so the totals and the weekly sum agree) and the freshness counts; add the two keys to
  the returned dict. Reuse the `datetime`/`timedelta`/`UTC` names already imported in that function.
  Per the repo rule, no new helper unless a second caller appears; keep the weekly bucketing inline
  or in one private function in `cli.py` if the function becomes unreadable (state which in the
  report).
- `docs/architecture.md`: extend the sentence that names `saltmdb-cli corpus-health` (the one ending
  "cross-owner administrative report") with: it also reports correction counts per week and the
  share of active memories updated in the last 30 days.

## 4. Tests

In `TestCorpusHealthCli`, behaviour through `cmd_corpus_health`, expected values written as literals
(timestamps built as fixed offsets from `datetime.now(UTC)`, e.g. 1 day and 20 days ago), written
failing first:

1. Empty database: `corrections.total == 0`, `corrections.by_week` has the weeks of the window with
   zero counts (length 1 or 2 for `days=7`), `freshness.active == 0` and `freshness.share is None`.
2. Insert 2 entities and 3 relations (`revises` 1 day ago, `supersedes` 2 days ago, `corrects` 20
   days ago) via the existing `_mk_entity` helper plus a direct `relations` insert (look at how
   neighbouring tests insert relations; reuse that): with `days=7` the counts are
   `revises 1, supersedes 1, corrects 0, total 2`; with `days=30` `corrects` is 1 and `total` 3; the
   `by_week` corrections sum equals `total` and `memories_created` sum equals the top-level value.
3. Freshness: of 3 non-archived entities with `updated_at` 1 day, 40 days and 50 days ago plus 1
   archived entity, `active == 3`, `updated_in_window == 1`, `share == 0.333`.
4. `days=0`: `total == 0` and `by_week == []`.
5. A relation row with an unparseable `created_at` is counted in `unparsed_rows` and does not raise
   (only if the schema lets the test insert it; otherwise state why it was dropped).

## 5. Documentation

`docs/architecture.md` as in section 3. `docs/BACKLOG.md`: add BL-039 (this metric, implemented,
awaiting review; evidence summary one sentence) in the Open table.

## 6. Verification procedure

Default `./verify`; no parallel full runs. For acceptance 4 use a copy of a database, never the
live one (`saltmdb-cli snapshot` or a file under `~/.saltmdb/backups/`, copied into a gitignored
`scratch-cold/` on the real disk, deleted afterwards).

## 7. Out of scope

A staleness threshold or alert; a drift-sweep schedule; a per-author breakdown; exposing the metric
through MCP; fixing `export-corpus-snapshot` to carry `parent_ids` (separate item).

## 8. Acceptance

1. Clean tree before editing; `./verify` exit 0 with counts.
2. New tests red before the `cli.py` change, green after.
3. `./verify` exit 0 after, counts recorded.
4. Run `saltmdb-cli --db-path <copy> corpus-health --days 30` on a snapshot copy and report the
   `corrections` and `freshness` blocks (expected shape per D2/D3; on the 2026-10-10 snapshot
   `revises`/`supersedes`/`corrects` over 30 days should be non-zero) and the command's wall time
   (it also runs in every session start, so it must stay well under 1 s on the copy).
5. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

- Existing users of the touched function checked: `tests/test_cli.py` `TestCorpusHealthCli` asserts
  specific keys only (new keys cannot break it); the session-start hook reads only
  `overdue_core_reviews`; `docs/architecture.md` mentions the command once.
- Data claims: relation predicates and timestamp format measured on the 2026-10-10 snapshot copy.
- Developer test policy (behaviour only, no mock echoes, no re-pinning) is honoured by section 4.
- No consultant pass; locked on the gate alone.
