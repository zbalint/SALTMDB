# SPEC-SESSION-CONTINUITY

## 0. Status

LOCKED 2026-10-12.

- Backlog item: BL-044. Shared `context_id`: `session-continuity-2026-10-12`.
- Baseline: commit `56dfe28` on `develop`. Before editing, the developer runs
  `.venv/bin/python -m pytest tests/test_session_digest_service.py tests/test_agent_sessions.py tests/test_cli.py hooks/tests -q`
  (architect baseline 2026-10-12: 201 passed, 18 subtests) and records the result.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `agent_sessions.get_recent_sessions_for_cwd`,
  `session_digest_service.render_last_session_digest`, `session_digest_service.render_session_digest`
  (all with a real SQLite `init_db`, as the existing tests do), `cli.cmd_session_digest`, and the two
  hook scripts run as subprocesses with a fake CLI (as `hooks/tests/test_digest_retry.py` does).
- Scope (may edit):
  - `src/saltmdb/db/agent_sessions.py` (`get_recent_sessions_for_cwd` and its docstring only)
  - `src/saltmdb/domain/services/session_digest_service.py`
  - `src/saltmdb/cli.py` (`cmd_session_digest` and the `session-digest` subparser only)
  - `src/saltmdb/daemon/dispatch.py` (`_dispatch_get_last_session_digest` only)
  - `hooks/_saltmdb_hook_common.py`, `hooks/saltmdb-session-start-bootstrap.py`,
    `hooks/saltmdb-prompt-submit-digest-retry.py`, `hooks/README.md` (the two table rows that
    describe these hooks and the digest, plus any sentence naming 9,800)
  - `tests/test_agent_sessions.py`, `tests/test_session_digest_service.py`, `tests/test_cli.py`,
    `tests/test_mcp_tools.py` (only if an existing digest-dispatch test needs the new keyword),
    `hooks/tests/test_session_start_bootstrap.py`, `hooks/tests/test_digest_retry.py`
  - `docs/architecture.md` (the one bullet that starts "Directory-Scoped Last-Session Digest") and
    `INSTALL.md` (the `SALTMDB_HANDOVER_MAX_CHARS` line only)
- Does not touch: `docs/BACKLOG.md` (the architect adds and updates the BL-044 row),
  `src/saltmdb/config.py`, `src/saltmdb/daemon/protocol.py` (RPC params are free-form, see 8),
  the `_agent_sessions` schema, `MIGRATION.md`, the viewer, every other hook script, and
  `~/.agents/hook-sources/` (the deployed copies; the architect syncs them after acceptance).
- Hand-off: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Goal (owner, 2026-10-12): session continuity. In the owner's workflow every project runs a team of
N sessions (architect, developer, tester, consultant) that start together and end together. The next
session start must receive (1) the core memories, (2) the memories written by the previous team,
(3) the last trace of each member of the previous team, all inside the harness's 10,000-character
hook-output limit.

Observed failure (2026-10-12, after a WSL2 restart; the BL-043 retry hook worked): the digest
delivered the memory index of ONE session (2 memories), a handover of three sessions' last traces,
and was then cut by the hook cap. The previous architect's final turn (a deploy) was missing.
Evidence from `~/.saltmdb/daemon.log`: five sessions sent a clean goodbye between 00:03:56 and
00:04:12 local time (the log is UTC+2), and the daemon reconciled no orphans at the next start. So the
end times were not backdated. The cause is the render order and the budget:

1. `render_session_digest` renders up to 40,000 characters (`config.py:104`) but the hook delivers
   9,800. `cap_hook_output` is a blind head slice that cuts the END, and the handover orders
   sessions by `ended_at` descending, so the session that ended first (here the architect, which
   closed itself before its peers) lands last and is cut.
2. The memory index picks ONE session, ordered by `started_at` (`render_last_session_digest`),
   which for a team that started within 90 ms is an arbitrary member.
3. Team members started within 90 ms of each other but ended over about 16 s (and, in other runs,
   minutes): "started together" is the reliable grouping signal and "ended together" the fragile one.
4. On the retry hook (first prompt, possibly minutes after start) the current team's own siblings
   already have traces and would be rendered as "previous" sessions.

Prior specs this builds on, not reverts: SPEC-HANDOVER-SESSION-WINDOW (BL-024: window, water-filling),
SPEC-BOOTSTRAP-EMPTY-SESSION-WINDOW (BL-011: content filter in SQL),
SPEC-BOOTSTRAP-DELIVERY-GUARANTEE (BL-043: marker, retry hook, 9,800 cap).

Rejected alternatives:
- K = 3 traces per session: the arithmetic (fixed markup about 480 characters per session and trace,
  N = 5) leaves about 570 characters of text per session, so one extra trace costs more than it gives.
  K = 1 is the guarantee.
- Reading `HOOK_OUTPUT_MAX_CHARS` in the service: Antigravity's `injectSteps` branch is not capped and
  the service must not know the harness. The hook passes the budget.
- Backdating theory ("crash gives a backdated `ended_at`"): not what happened this time (clean
  goodbyes). A session that does crash is still handled by the effective end below.
- Role labels per session and cross-directory visibility: need schema or protocol changes, see 9.

## 2. Decisions

- D1. One cohort. A single function `_select_cohort` in `session_digest_service.py` selects the
  sessions that the memory index and the handover both use. Two selections would drift.
- D2. Candidates: sessions of the cwd, with `started_at` strictly before `before` when `before` is
  given, that created or touched a non-archived memory OR have a trace (`with_content="any"`), newest
  `started_at` first, at most `COHORT_CANDIDATE_LIMIT` (50). A running candidate (no `ended_at`) is a
  candidate only when its effective end is within `HANDOVER_RUNNING_MAX_AGE_HOURS` (24) of `now`.
- D3. Effective end of a session: the latest of its `ended_at`, `last_activity_at` and the newest
  trace's `COALESCE(updated_at, created_at)` (section 3 `last_trace_at`), each only when set and
  parseable. `updated_at` is used because `created_at` is the start of a turn, so a long final turn
  would make the session look like it ended early. A session with none of the three parseable uses
  `started_at`; a session where even that does not parse is skipped.
- D4. Anchor: the candidate with the latest effective end (tiebreak `session_id` descending) among
  the candidates that have ended (`ended_at` set); only when no candidate has ended, among all
  candidates. A running candidate can therefore never become the anchor while an ended one exists
  (a current-generation session started minutes before its peers must not displace the previous
  team). Cohort, built in one non-chained pass:
  1. `E`: the anchor, plus every candidate whose effective end is at or after the anchor's
     `started_at` minus `HANDOVER_WINDOW_SECONDS` (60). This is overlap with the anchor's lifetime,
     not closeness of the end times: an architect that reviews and commits minutes after its peers
     ended still brings them in, while a team that ended before the anchor started stays out.
  2. The cohort: `E`, plus every other candidate (running or ended) whose `started_at` is within 60 s
     (inclusive, either side) of the `started_at` of ANY member of `E`. This brings in a team member
     that crashed hours earlier or is still running.
  No minimum is applied: a single previous session is a cohort of one, and an older team is never
  used as padding. Keep at most `HANDOVER_MAX_SESSIONS` (8), ordering by effective end descending,
  tiebreak `session_id` descending, and cutting from the end of that order. This ordering is the render
  order for index and handover.
- D5. Current-team exclusion. `before` (an ISO 8601 timestamp, optional) removes every session that
  started at or after it. The hooks pass the SessionStart hook's own start time minus 60 s, so sibling
  sessions of the team being started (their `started_at` is within seconds of the hook) never appear
  as "previous" sessions. Without `before` nothing is excluded (manual CLI, tests).
- D6. Whole-output budget. `max_chars` bounds the whole `render_session_digest` output: index plus
  handover. `None` means the configured default (`get_handover_max_chars()`, 40,000). `0` keeps
  its meaning: the handover is disabled and only the index is returned (the index keeps its own line
  caps from D8). The CLI argument `--max-chars N` (D11) lowers it: effective budget is
  `min(N, env)`, except that an env value of `0` stays `0`.
- D7. The index takes at most `INDEX_BUDGET_SHARE` (0.25) of the budget; the handover gets the rest.
  When D10 had to turn any session into a stub, the index shrinks to its first
  `INDEX_STUB_LINES` (5) lines plus the `(+N more not shown)` line, and the freed characters go back to
  the handover.
- D8. Memory index over the whole cohort, merged and deduplicated by memory id: memories created by a
  cohort session (`agent_session_id`) first, then memories only touched (`last_touched_session_id`), each
  group ordered by `updated_at` descending, archived excluded. A memory both created and touched by
  cohort members appears once, in the created group. Caps: `INDEX_CREATED_MAX` (15) and
  `INDEX_TOUCHED_MAX` (5); further lines are replaced by one line `- (+N more not shown)`. If the index
  is still longer than D7 allows, drop lines from the end (touched first) and update that count.
  Each line is tagged with the member that wrote it.
- D9. Handover content is unchanged in kind: for each cohort session that has a trace, its newest
  trace (K = 1) with prompt, up to `HANDOVER_MAX_MID_TURN` mid-turn messages and the final message, using
  the existing `_render_session` markup. Cohort sessions without any trace are skipped.
- D10. Nothing silently cut. The handover is rendered to fit its share of the budget by
  render, measure and shrink, never by estimating the markup (truncation markers, truncation hints and
  tag neutralisation add characters only after a body is cut). Let `H` be the budget left for the
  handover and `msgs` the messages to show (each session's prompt, mid-turn messages and final
  message). The algorithm, with its termination:
  `H = budget - len(rendered index) - 1` (the `\n` between index and handover).
  1. Prompts (each shown trace's user message, and the anchor prompt of a background-task turn, but not
     mid-turn messages) are limited to `USER_MESSAGE_HEAD_CHARS` (120), head only (the a2amx subject
     and sender are at the head); this applies at every budget. The other messages start from the
     existing water-filling cap over `H`.
  2. Subroutine `fit(cap)`: render with that cap; return "fits" if `len(handover) <= H`. Otherwise
     let `cut` be the number of messages that were truncated; if `cut == 0` return "exhausted" (the
     markup alone is too big). Lower the cap by `ceil(overflow / cut)` (at least 1) and render again;
     return "exhausted" as soon as the cap would be below `HANDOVER_MIN_MESSAGE_CHARS` (300). Each
     pass strictly lowers the cap, so `fit` always returns.
  3. Run `fit` with the water-filling cap. "fits": done.
  4. "Exhausted": show only the newest mid-turn message of every session (the omitted-count hint
     stays) and run `fit` once more with a fresh water-filling cap.
  5. "Exhausted" again: turn one session into a stub, run `fit` again, and repeat step 5 until `fit`
     returns "fits" or every cohort session is a stub. Stub order: the session with the OLDEST
     newest-trace (`last_trace_at`) first, tiebreak `session_id` ascending, so the member with the
     newest work is the last one stubbed. A stub is ONE line,
     `<session-stub id="..." agent_id="..." state="..." ended_at="..." trace_id="..." status="..."/>`
     followed by nothing else (about 205 characters with uuid7 ids and microsecond timestamps; the
     "lost" and "unfinished" hints are carried by `state` and `status`; `get_trace(trace_id)` reads
     the trace). Once any stub is used, the three handover envelope hints shrink to the first one.
  A cohort session is never dropped by this algorithm. (The selection in D4 still keeps at most 8.)
  If every session is a stub and the output still exceeds the budget, the output is returned as is;
  the hook's `cap_hook_output` is only that backstop.
- D11. CLI and RPC. `saltmdb-cli session-digest` gains `--max-chars N` (int) and `--before ISO`
  (string). Both are forwarded as the RPC params `max_chars` and `before`; `before` is omitted when not
  given. The CLI normalises `--before` before forwarding: `datetime.fromisoformat`, a naive value is
  taken as UTC, then `.astimezone(UTC).isoformat(timespec="microseconds")`, so the string comparison
  in SQL matches the stored `started_at` format (`datetime.now(timezone.utc).isoformat()`, written by
  `record_session` at the adapter's hello, `daemon/server.py:388-403`). An unparseable `--before` prints
  a warning to stderr and is ignored (the CLI stays best-effort, exit 0).
  `_dispatch_get_last_session_digest` passes `before` through.
- D12. Hook budget. The hooks compute the budget they pass:
  `N = max(HOOK_OUTPUT_MAX_CHARS - len(core_section) - OVERDUE_RESERVE_CHARS - NOTICE_RESERVE_CHARS - 4, SESSION_DIGEST_MIN_BUDGET)`
  with `OVERDUE_RESERVE_CHARS = 1400` (the overdue notice is fetched after the digest and is not
  known yet: about 345 characters of fixed text plus up to five titles of up to 200 characters),
  `NOTICE_RESERVE_CHARS = 450` (slack that absorbs a small render overshoot and the cut notice, which
  is 336 characters with a 119-character CLI path in the tester's measurement) and
  `SESSION_DIGEST_MIN_BUDGET = 3500` (eight stubs of about 205 characters, the handover envelope and
  a five-line index). The retry hook has no overdue notice, so its reserve is 0; its `len(core_section)` is the
  length of a core digest fetched in the same run, otherwise 0. When the payload is Antigravity's
  (`conversationId` present, its `injectSteps` output is not capped) the SessionStart hook omits
  `--max-chars` and keeps `--before`. If the core digest is so large that even
  `SESSION_DIGEST_MIN_BUDGET` does not fit, `cap_hook_output` cuts the end; that is the accepted
  backstop and the reason the core tier is capped separately.
- D13. The pending marker records when SessionStart started: `write_digest_pending` gains an optional
  `started_at: float | None = None` (epoch seconds) and writes the key `"started_at"` ONLY when it is
  not `None`, so a marker without it keeps today's exact shape. SessionStart passes its own start time
  on every write; the retry hook passes the claimed marker's value through when it rewrites the marker.
  Both hooks derive `before` as
  `datetime.fromtimestamp(started_at - COHORT_SLACK_S, UTC).isoformat(timespec="microseconds")`
  with `COHORT_SLACK_S = 60`. The retry hook omits `--before` when the marker has no valid
  `started_at`.

## 3. `src/saltmdb/db/agent_sessions.py`: `get_recent_sessions_for_cwd`

Signature becomes
`get_recent_sessions_for_cwd(conn, cwd, limit=10, *, with_content=None, ended=None, before=None)`.

- `with_content` accepts a third value `"any"`: `(<memories predicate> OR <traces predicate>)` using the
  two predicates already in `_CONTENT_PREDICATES`. The `ValueError` text becomes
  `with_content must be one of: memories, traces, any`. Add `"any"` to `_CONTENT_PREDICATES` as the
  parenthesised OR of the other two strings; do not duplicate their SQL.
- `before`: when not `None`, add `AND s.started_at < ?` with the string as given. Callers pass an
  ISO 8601 UTC string with microseconds, the format `started_at` is stored in (D11 normalises it in
  the CLI); the function does not parse it.
- Each returned dict gains `"last_trace_at"`: `(SELECT MAX(COALESCE(t.updated_at, t.created_at)) FROM
  conversation_traces t WHERE t.agent_session_id = s.session_id)` or `None`. This is the only new
  column in the SELECT. (`trace_service` writes `updated_at` at insert and again at completion,
  `trace_service.py:134` and `:307`.)
- Docstring: state the three `with_content` values and `before`.

## 4. `src/saltmdb/domain/services/session_digest_service.py`

Constants (module level, with the existing `# shortcut:` comment kept above the block and extended):
`HANDOVER_WINDOW_SECONDS = 60` unchanged; `HANDOVER_MAX_SESSIONS` becomes `8`;
`HANDOVER_RUNNING_MAX_AGE_HOURS = 24` and `HANDOVER_MAX_MID_TURN = 5` unchanged; delete
`HANDOVER_MIN_SESSIONS` (no floor, D4) and `HANDOVER_MAX_RUNNING`; add `COHORT_CANDIDATE_LIMIT = 50`,
`INDEX_BUDGET_SHARE = 0.25`, `INDEX_STUB_LINES = 5`, `INDEX_CREATED_MAX = 15`,
`INDEX_TOUCHED_MAX = 5`, `HANDOVER_MIN_MESSAGE_CHARS = 300`, `USER_MESSAGE_HEAD_CHARS = 120`.

- Replace `_select_handover_sessions` with `_select_cohort(candidates, now)` implementing D2 (running
  age), D3, D4 over the dicts returned by section 3. It returns the ordered list; each dict gains
  `"effective_end"` (a timezone-aware `datetime`). Timestamp parsing reuses the local
  `parse_timestamp` (naive values are UTC); move it to module level so both functions use it.
- `render_last_session_digest(conn, cwd, *, before=None, now=None, max_chars=None)` renders D8 over
  the cohort members that have a memory line. Envelope: `<saltmdb-last-session-digest session_id="<id of
  the first cohort member with memories>" started_at="<its started_at>" sessions="<number of cohort
  members with memories>">`. Its `max_chars` is the index's own budget: `None` means no budget (only the
  D8 line caps apply); `render_session_digest` passes `int(budget * INDEX_BUDGET_SHARE)`, or `None`
  when the budget is `0`. Line format
  `- {id} [{memory_type}] {title} (by {agent_id or "?"}#{session_id[-4:]})` for the session that
  created it, or that last touched it for touched-only lines. An empty index keeps today's empty
  envelope exactly.
- `_render_handover(conn, cohort, max_chars)` implements D9 and D10. Keep `_render_session`,
  `_message_block`, `_truncate`, `_neutralize` and `_session_state` as they are, except that
  `_truncate` and `_message_block` gain a keyword `head_only: bool = False` (keep only the first `cap`
  characters, with the same truncation marker and `truncated="true"`), used for the D10 prompt cap; add a small
  `_render_stub(session, trace)` for the one-line stub. The water-filling loop stays as the starting
  cap; D10's render, measure and shrink loop wraps it (no markup estimate).
- `render_session_digest(conn, cwd, max_chars=None, *, before=None, now=None)` implements D6 and D7:
  fetch candidates once (`with_content="any"`, `limit=COHORT_CANDIDATE_LIMIT`, `before=before`), call
  `_select_cohort` once, pass the same cohort to the index and the handover.
- Keep the docstring of `render_last_session_digest` honest: it now describes the cohort, not "the
  most recent prior session".

shortcut: candidates are the 50 newest by `started_at`; a session that started earlier than 50
others but ended last could be missed; raise the limit if real use shows it.

## 5. `src/saltmdb/cli.py`

`cmd_session_digest` reads `args.max_chars` and `args.before` (both default `None`), applies D6 for the
effective budget, normalises `--before` as D11 says (warn on stderr and ignore if
`datetime.fromisoformat` raises `ValueError`), and passes `{"cwd", "max_chars", "before"?}` to `get_last_session_digest`. The
`session-digest` subparser gains the two arguments. Nothing else in the file changes.

## 6. `src/saltmdb/daemon/dispatch.py`

`_dispatch_get_last_session_digest` passes `before=kw.get("before")` to `render_session_digest`.

## 7. Hooks

- `hooks/_saltmdb_hook_common.py`: add `COHORT_SLACK_S`, `OVERDUE_RESERVE_CHARS`,
  `NOTICE_RESERVE_CHARS`, `SESSION_DIGEST_MIN_BUDGET` next to `HOOK_OUTPUT_MAX_CHARS`; add
  `session_digest_args(core_len, overdue_reserve, started_at, *, capped=True)` returning the argument tuple
  `("session-digest", "--max-chars", str(N), "--before", iso)` per D12 and D13 (the `--before` pair only
  when `started_at` is a number; `iso` is `datetime.fromtimestamp(started_at - COHORT_SLACK_S,
  UTC).isoformat(timespec="microseconds")`; the `--max-chars` pair is omitted when a parameter
  `capped=False` is passed, which the Antigravity branch uses); extend `write_digest_pending` per D13.
  `cap_hook_output` is unchanged.
- `hooks/saltmdb-session-start-bootstrap.py`: record `STARTED_AT = time.time()` at import; run
  `session-digest` with `session_digest_args(len(core_section), OVERDUE_RESERVE_CHARS, STARTED_AT)`
  (core is fetched first, so its length is known); `bootstrap_text(source, missing, *, capped=True)`
  gains the keyword, and `main` calls it with `capped=not payload.get("conversationId")` (today
  `main` reads that key only after `bootstrap_text` has run, L141-143; the existing callers in
  `hooks/tests/test_session_start_bootstrap.py` at L134, L147, L162 and L182 keep the default); pass
  `started_at=STARTED_AT` to both `write_digest_pending` calls. The pending-notice text and order of sections stay.
- `hooks/saltmdb-prompt-submit-digest-retry.py`: `_fetch_parts` fetches `bootstrap-digest` first when
  missing, then runs `session-digest` with `session_digest_args(core_len, 0, marker_started_at)` where
  `core_len` is the length of the core digest fetched in this run (0 if it was not missing);
  `_finish_claim` passes the marker's `started_at` back into `write_digest_pending`.
- `hooks/README.md`: update the sentences that describe the digest size and the handover selection.

## 8. Verified facts the spec relies on

- RPC params are not validated by name: `daemon/protocol.py` lists method names only (L58) and
  `build_request` takes a free-form dict (L117).
- `render_last_session_digest`'s callers are `render_session_digest` and tests; the daemon reaches
  the service only through `_dispatch_get_last_session_digest` (`dispatch.py:552`).
- The marker is compared exactly in `hooks/tests/test_digest_retry.py:77`
  (`{"missing": [...], "attempts": 0, "running_since": None}`). That test runs `bootstrap.main()`
  (SessionStart), which now passes `started_at` on every write, so the marker gains the key: update
  that assertion (compare `pending["started_at"]` to a float and the rest to the old dict). D13 omits
  `started_at` only when it is `None`, which keeps the retry-hook writes in that file unchanged when
  the test's marker has none.
- Hook tests whose fakes break when `run_cli` gets more arguments: the one-argument lambdas
  `side_effect=lambda part: values[part]` in `hooks/tests/test_digest_retry.py` (from L58 on, several
  tests) must accept `*args` and key on `args[0]`; `hooks/tests/test_session_start_bootstrap.py:155-158`
  keys on `command[-1] == "session-digest"` and must key on `command[1]`. Keep `run_cli`'s signature
  `run_cli(*args)`; do not change it to suit the fakes.
- The BL-024 tests encode the old selection and are rewritten, not deleted, to the D2-D4 rules:
  `test_three_newest_ended_sessions_form_floor_and_skip_traceless`,
  `test_window_is_inclusive_and_anchored_to_newest_end_not_chained`,
  `test_window_caps_eight_nearby_ends_at_six_newest`,
  `test_equal_millisecond_ends_follow_session_id_not_start_time`,
  `test_unparseable_ends_count_in_floor_but_never_anchor_or_stop_window`, and the `test_running_*`
  family (`tests/test_session_digest_service.py` around L371-L518). The envelope assertion at
  `test_prior_session_with_non_archived_entities_renders_digest` (L78) may need the new `sessions`
  attribute. `test_agent_sessions.py:485` (`"memories.*traces"`) still matches the new text.

## 9. Out of scope

- A per-session role label (the a2amx address is not stored in `_agent_sessions`; `agent_id` is bound
  per harness) and cross-directory visibility (the lookup is by `cwd`): separate backlog items.
- More than one trace per session (K > 1), an adaptive session cap, prompt/response budget ratios.
  The 120-character prompt head (D10 step 1) applies at every budget, including the 40,000 default
  and Antigravity: the full prompt stays one `get_trace` away.
- Reordering the SessionStart fetches (corpus-health stays after session-digest; D12 reserves for it).
- Changing `cap_hook_output`, `HOOK_OUTPUT_MAX_CHARS`, `SALTMDB_HANDOVER_MAX_CHARS`'s default or
  `config.py`.
- Core-memory content, the retry marker's other fields, other harness hooks, Antigravity's uncapped
  `injectSteps` path (D12: it keeps the default budget and receives `--before` only).
- Syncing `~/.agents/hook-sources/`: the architect does it after acceptance.

## 10. Acceptance

Run from the main checkout with the repo `.venv`. Other processes may run suites on this host:
no parallel full runs.

1. Fresh `./verify` exits 0 on the final snapshot (new count above the 2231 baseline).
2. `.venv/bin/python -m pytest tests/test_session_digest_service.py tests/test_agent_sessions.py tests/test_cli.py hooks/tests -q` passes.
3. New or rewritten tests cover each of these, by name in the completion report:
   a. Team fixture (the 2026-10-12 failure): five sessions in one cwd started within 100 ms, ended 4-16 s
      apart; each created exactly one memory with a title of at most 40 characters; the member that ended
      first has a 6,000-character final assistant message containing a marker string. Every other member
      has a 1,500-character final message. `render_session_digest(conn, cwd, 9000)` returns at most 9,000
      characters, contains all five memory ids, a `<session id=` tag for each of the five, and the first
      80 characters of the first-ended member's final message as text (not only as a stub).
   b. One-hop cohort (the consultant's instance): A starts alone at 08:00 and D, T, C start at 09:00:00
      (within 100 ms); C crashes at 11:00 (`ended_reason="orphaned"`, `ended_at` 10:58, newest trace 11:00);
      D ends 17:00:00, T 17:00:10, A 17:05. The cohort is {A, T, D, C}: C is included through its start
      proximity to D and T. A separate older team in the same cwd is not in the cohort.
   b2. A crashed member's `ended_at` equal to its `started_at` plus 1 s with a newest trace hours later
      is ordered by its effective end (D3, `updated_at`), not its stored end.
   b3. Anchor: a session still running with a fresh trace, started before `before`, does not become the
      anchor while ended candidates exist; with no ended candidate the running one is the anchor.
   b4. No floor: with one previous session and an older team in the same cwd, the cohort is the one
      previous session only.
   c. `before`: sessions started at or after `before` are absent from both the index and the handover;
      absent `before` excludes nothing.
   d. Index dedupe and caps: a memory created by member A and touched by member B appears once; 20 created
      and 8 touched memories render 15 + 5 lines plus the `(+N more not shown)` line.
   e. Degradation (D10): with a budget so small that the per-message cap would be under 300, the
      order is mid-turn messages cut to one, then stubs starting with the session whose newest trace
      is the oldest; no session is dropped, a stub is one line with the trace id, and the output of
      eight sessions at a budget of 3,500 characters is at most 3,500; the fixture uses uuid7-style
      session ids and microsecond ISO timestamps, as production does. The member with the newest
      trace is the last one stubbed. The loop terminates (a test with a budget of 1 returns eight
      stubs, no exception, and a test where the markup alone exceeds the budget with nothing cut
      returns "exhausted" without a division by zero).
   f. `max_chars=0` returns the index only; `None` uses the configured default.
   g. `get_recent_sessions_for_cwd`: `with_content="any"`, `before`, the `last_trace_at` key, and the
      new `ValueError` text.
   h. CLI: `--max-chars` and `--before` reach the RPC; invalid `--before` warns and is ignored;
      `min(N, env)` and env `0`.
   i. Hooks: the fake CLI receives `session-digest --max-chars <N> --before <iso>`; with a 300-character CLI
      path and a 25,000-character fake session digest the SessionStart and retry outputs are at most
      9,800 characters (the existing cap test stays green); the marker carries `started_at` and the retry hook
      passes it through; a marker without `started_at` yields no `--before`.
4. `git diff --stat` shows only files listed in section 0. `git status --short` shows no stray files.
5. Post-implementation check by the architect (not the developer), after the reviewed code is
   installed into `~/.mcp/SALTMDB` and the live daemon is restarted (the old daemon ignores `before`
   and the old CLI rejects `--max-chars`): `saltmdb-cli session-digest --max-chars 8000` in this
   repository prints at most 8,000 characters and one `<session` or `<session-stub` tag per
   cohort member.
