# SPEC-HANDOVER-SESSION-WINDOW

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-024. Shared `context_id`: `handover-window-2026-10-10`.
- Baseline: commit `88bb3d5` on `develop`; `./verify` green (2044 tests at that commit, to be re-run
  by the developer before editing and recorded in the completion report).
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `agent_sessions.get_recent_sessions_for_cwd` and
  `session_digest_service.render_session_digest` (the same two seams the existing tests use).
- Scope (may edit):
  - `src/saltmdb/db/agent_sessions.py` (`get_recent_sessions_for_cwd` only)
  - `src/saltmdb/domain/services/session_digest_service.py` (handover part only, lines 72-250;
    import `datetime` locally inside the functions that need it, as the file already does for
    other imports, so the top import block stays unchanged)
  - `tests/test_agent_sessions.py`, `tests/test_session_digest_service.py` (including an
    `ended_at=` keyword on the `_session` helper at `test_session_digest_service.py:281`, and the
    comment in `test_truncation_keeps_head_and_tail_and_hints_trace_id`)
  - `docs/architecture.md` (the one bullet that starts "Directory-Scoped Last-Session Digest")
- Does not touch: `docs/BACKLOG.md` (the BL-024 row already exists; the architect updates its status), `render_last_session_digest` and its output, the hook scripts, `cli.py`,
  `daemon/dispatch.py`, `config.py`, the `_agent_sessions` schema, any other file.
- Hand-off: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

The session-start handover shows the last trace of the two newest prior sessions in the working
directory, newest by `started_at`. With several sessions running in one directory (architect,
developer, tester and consultant peers, or two terminals) that stop within milliseconds of each
other, the next start receives whichever two started latest, which may be a peer's work instead
of its own. `agent_id` is bound per harness, so it cannot tell such sessions apart, and a
per-session label is not available (the peer messaging tool exposes no stable name).

Decision (owner, 2026-10-10): sessions that ended close together are shown together, ordered by
end time. A long session followed by a quick one-question session also benefits: both stay visible.

Rejected alternatives: a session label or identity column (nothing to fill it from yet);
ordering by `ended_at` alone without a window (cannot separate sessions that stop in the same
millisecond); a chained-gap window (grows without bound across a slow shutdown sequence); raising
the total budget above 40000 characters (the hook-output size limit is not pinned down).

## 2. Decisions

- D1. Handover candidates are split into two groups: ended sessions (`ended_at` set) and running
  sessions (`ended_at` NULL), both restricted to sessions that have a trace in this cwd.
- D2. Ended group: order by `ended_at` descending, tiebreak `session_id` descending. Take the
  `HANDOVER_MIN_SESSIONS` (3) newest. Add each further session, in order, if its end is at most
  `HANDOVER_WINDOW_SECONDS` (60, inclusive) before the newest end (window anchored at the end of
  the newest session whose `ended_at` parses, not chained). Stop at `HANDOVER_MAX_SESSIONS` (6)
  total. A session whose `ended_at` cannot be parsed counts only toward the floor: after the floor
  it is skipped (not a stop), and it is never the anchor.
- D3. Running group: only sessions whose `COALESCE(last_activity_at, started_at)` is within
  `HANDOVER_RUNNING_MAX_AGE_HOURS` (24) of now; order by that value descending, tiebreak
  `session_id` descending; take at most `HANDOVER_MAX_RUNNING` (2). Running sessions do not count
  toward the floor or the cap of D2.
- D4. Render order: ended group first (newest end first), then the running group.
- D5. Budget: replace the even split `max_chars // message_count` with water-filling. Messages
  no longer than the current share keep their full text; the budget they do not use is shared among
  the longer ones, repeated until stable. The total stays `max_chars`.
- D6. The `<session>` tag gains `ended_at="..."` when the session has ended; running sessions get
  no `ended_at` attribute.
- D7. Only the handover path changes. `render_last_session_digest` keeps ordering by `started_at`.
- D8. The five numbers are module constants in `session_digest_service.py`, not environment
  variables. The hook and CLI are unchanged.

## 3. Changes per file

### 3.1 `src/saltmdb/db/agent_sessions.py`, `get_recent_sessions_for_cwd`

Extend the signature (no second near-identical function):
`get_recent_sessions_for_cwd(conn, cwd, limit=10, *, with_content=None, ended=None)`.

- `ended=None`: current behaviour exactly (`ORDER BY s.started_at DESC`).
- `ended=True`: add `AND s.ended_at IS NOT NULL`; `ORDER BY s.ended_at DESC, s.session_id DESC`.
- `ended=False`: add `AND s.ended_at IS NULL`;
  `ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC, s.session_id DESC`.
- Any other value is not validated (callers pass a bool or None).
- Each returned dict gains the key `"last_activity_at"` (select `s.last_activity_at`).
- The docstring states the three orderings.

### 3.2 `src/saltmdb/domain/services/session_digest_service.py`

Replace the `HANDOVER_MAX_SESSIONS = 2` line and its `# shortcut:` comment (lines 72-74) with the
five constants of D2/D3, each with a one-line comment, plus a `# shortcut:` comment: fixed
constants, not configurable; make them environment-configurable (read in the CLI like `max_chars`)
if real use shows the defaults are wrong.

New pure helper `_select_handover_sessions(ended, running, now)` (used once, but it is the unit
the tests target, so it stays separate from the DB code): takes the two lists as returned by
`get_recent_sessions_for_cwd`, parses timestamps with `datetime.fromisoformat` (a naive value is
treated as UTC; a value that does not parse is "unparseable"), applies D2/D3 and returns the
chosen sessions in D4 order. `now` is an aware `datetime`.

`render_session_digest(conn, cwd, max_chars=None, *, now=None)`: `now=None` means
`datetime.now(timezone.utc)`. It fetches the ended group with
`get_recent_sessions_for_cwd(conn, realpath(cwd), limit=HANDOVER_MAX_SESSIONS, with_content="traces", ended=True)`
and the running group with `limit=HANDOVER_MAX_SESSIONS, with_content="traces", ended=False`,
passes both through `_select_handover_sessions`, and gives the chosen list to `_render_handover`.
`dispatch.py` keeps calling it with two arguments, so `now` is a test seam only.

`_render_handover(conn, candidates, max_chars)`: remove the `HANDOVER_MAX_SESSIONS` break (the
selection now happens before it). Keep the per-session trace lookup and the skip of a session
without a trace row. Replace `cap = max(1, max_chars // message_count)` with the water-filling cap
of D5, computed over the lengths of every message that will be rendered (user prompt, shown
mid-turn messages, anchor prompt, and the final assistant message only when it is not None; the
old count added 2 per session even for a None response, the new one does not). Water-filling, stated exactly: sort the
lengths ascending; `remaining = max_chars`, `n = len(lengths)`; for each length `l` in order, if
`l <= remaining // n` then `remaining -= l`, `n -= 1`, else stop; the cap is `max(1, remaining // n)`
if `n > 0`, else the largest length. Update the function docstring.

`_render_session`: add `ended_at` to the opening `<session ...>` tag per D6 (after `started_at`).
The session dict now carries `ended_at` already.

### 3.3 `docs/architecture.md`

In the bullet that starts "Directory-Scoped Last-Session Digest", replace the wording "of the two
newest prior sessions in that directory" with a short description of D2/D3: the three most
recently ended sessions with traces, plus any that ended within 60 seconds of the newest end (up
to six), plus up to two still-running sessions active in the last 24 hours, each with its end
time. Keep the rest of the bullet.

### 3.4 `docs/BACKLOG.md`

No change by the developer. The architect already added the BL-024 row and updates its status
after review.

## 4. Tests (failing first; literals, not recomputed)

`tests/test_agent_sessions.py`:
- T1. `ended=True` returns only ended sessions ordered by `ended_at` DESC; with two sessions
  started in the opposite order, the order follows the end time.
- T2. Equal `ended_at` values are ordered by `session_id` DESC.
- T3. `ended=False` returns only sessions with NULL `ended_at`, ordered by last activity DESC.
- T4. Returned dicts contain `last_activity_at`.
- T5. (existing tests, unchanged) `ended=None` still orders by `started_at`.

`tests/test_session_digest_service.py` (class `TestSessionHandover`):
- T6. Floor: five ended sessions with traces, ends one hour apart: exactly the three newest
  appear (this replaces `test_only_two_newest_sessions_with_traces_and_skips_traceless`, keeping
  its traceless-session check).
- T7. Window: six sessions ending at 12:00:00 (E1), 11:59:50 (E2), 11:59:30 (E3), 11:59:00 (E4),
  11:58:50 (E5), 11:00:00 (E6): shown E1, E2, E3 (floor) and E4 (exactly 60 s before E1, so the
  inclusive boundary is pinned); not E5 (70 s) and not E6. The `_session` helper takes a new
  `ended_at=` keyword so end times are independent of start times.
- T8. Cap: eight sessions ending within 59 s of the newest end: exactly six appear, newest first.
- T9. Milliseconds: three sessions with identical `ended_at` and different `started_at`: all appear,
  `session_id` DESC order.
- T10. An unparseable `ended_at` string counts toward the floor when it is among the newest three,
  is skipped (not a stop) after the floor, and is never the window anchor.
- T11. Running: a running session active 1 hour before `now` appears after the ended group with
  `state="running"`; one active 25 hours before `now` does not; three recent running sessions give
  two. Pass `now=` explicitly; existing running-session tests that use 2024 timestamps must set
  `last_activity_at` near the `now` they pass.
- T12. Water-filling: one session with a user prompt of 100 characters, one mid-turn message of 100
  characters and a final assistant message of 5000 characters, `max_chars` 1000: the prompt and the
  mid-turn message are untruncated; the assistant message contains the literal
  `[... 4200 chars truncated ...]` (cap 800) and `truncated="true"`.
- T13. `<session>` tag has `ended_at="..."` for an ended session and no `ended_at` attribute for a
  running one.
- T14. The memory index (`render_last_session_digest`) still picks by `started_at` (existing tests
  cover it; keep them unchanged).

## 5. Documentation

Section 3.3 only.

## 6. Verification procedure

`./verify` only. Do not start a second full run in parallel (shared host).

## 7. Out of scope

Environment variables for the constants; a per-session label or identity; changing
`render_last_session_digest`; the hook scripts; per-tier budgets for floor versus window sessions;
the 40000 default budget; the viewer; the daemon's session lifecycle.

## 8. Acceptance

Run in order:
1. `./verify` before editing: exit 0, counts recorded.
2. `.venv/bin/python -m pytest tests/test_agent_sessions.py tests/test_session_digest_service.py -q`:
   T1-T4 and T6-T13 fail before the change and pass after; T5 and T14 are existing tests that pass
   both before and after.
3. `rg -n "HANDOVER_MAX_SESSIONS = 2|max_chars // message_count" src tests` finds nothing.
4. `./verify` after editing: exit 0, counts recorded.
5. `git diff --stat` lists only the files in section 0 scope.

## 9. Pre-lock gate notes

Done so far (architect, 2026-10-10):
- Baseline probe on `88bb3d5`: `pytest tests/test_agent_sessions.py tests/test_session_digest_service.py -q`
  gives 54 passed. The full `./verify` is the developer's step 1 (a full run on the shared host is
  not repeated here).
- Grep of the old text: `HANDOVER_MAX_SESSIONS` appears only in the service file and in the older
  spec `SPEC-BOOTSTRAP-EMPTY-SESSION-WINDOW.md` (a record, not edited). `max_chars // message_count`
  appears only in the service file. Docs mention "two newest" only in `docs/architecture.md`.
- Callers: `get_recent_sessions_for_cwd` is called only from `session_digest_service.py`
  (lines 34 and 245) and the tests; adding a keyword and a dict key breaks neither.
- Water-filling example T12 checked with a standalone script: budget 1000, lengths 100/100/5000
  gives cap 800.
- Existing tests that will need their data adjusted (running sessions use 2024 timestamps, which
  the 24 h rule would drop): `test_running_session_gets_concurrency_hint_not_unfinished_hint`;
  `test_only_two_newest_sessions_with_traces_and_skips_traceless` is replaced by T6.
- Activity: a tool call advances `last_activity_at` only when it carries caller metadata
  (`daemon/server.py` lines 546-555). The Copilot stop hook sends none (inferred), so a Copilot
  session that makes no other SALTMDB calls can have an early `ended_at` if orphaned and can fall
  out of the 24 h running window. How the Claude Code and Codex capture paths carry caller
  metadata was not checked. Accepted limitation; not part of this spec.
- Existing `test_truncation_keeps_head_and_tail_and_hints_trace_id`: with water-filling its cap
  goes from 200 to 395 and its assertions still hold; only its comment needs updating.

Consultant review (pre-lock) dispositions: B1-B4 and A1-A5 fixed above; limit 6 and string ordering
confirmed safe (all writers stamp UTC isoformat).
