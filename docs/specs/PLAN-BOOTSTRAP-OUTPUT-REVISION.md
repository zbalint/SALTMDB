# PLAN-BOOTSTRAP-OUTPUT-REVISION

Planning record for revising what the session-start hook injects into an agent's context. This is
a plan, not a spec: the implementation slices are the specs listed in section 8. Decisions made
here are the owner's unless marked otherwise.

- Date: 2026-10-12. Implementation is postponed to Wednesday 2026-10-14 by the owner.
- Backlog items: BL-045 to BL-051. Shared `context_id`: `bootstrap-output-revision-2026-10-12`.
- Baseline when written: `develop` at 5f01fbf, tree clean.
- Nothing in this plan is assigned to a developer.

## 1. Goal and constraints

SALTMDB is a cross-agent, cross-session memory. The bootstrap output has two goals, in the
owner's words and order of statement:

1. Help agents make better decisions and responses.
2. Help agents remember the past: what earlier sessions did, decided and left open, across
   agents and harnesses.

Constraints that bind every slice:

- **No dependency on, and no awareness of, any external project.** The owner cannot know what
  runs next to SALTMDB and cannot support every combination, so SALTMDB code, defaults, specs and
  docs name no other tool and special-case none. Features are generic mechanisms. A harness
  integration (a hook for an agent CLI) stays in the integration layer, not in the domain-neutral
  core. This extends the earlier decision that trace capture does no envelope detection.
- **Public repository rules** (see `AGENTS.md`): no memory content, ids copied from memories, real
  hostnames or personal data in any spec or doc.
- **The hook output limit is 9,800 characters** (`HOOK_OUTPUT_MAX_CHARS`,
  `hooks/_saltmdb_hook_common.py` L299), a character count and not a token count.

## 2. What the output is today

One hook run emits, in this order (`hooks/saltmdb-session-start-bootstrap.py` L55-125):

1. `<saltmdb-digest>`: the core memories only. Children `<core-rules>` and `<core-bootstrap-error>`.
   When there are no cores it is a blank block (`core_governance_service.py` L388). When the hook
   gets no text it emits `<saltmdb-digest-pending>`.
2. `<saltmdb-overdue-core-notice>`: only when a core review is overdue; built by the hook from a
   second CLI call (`corpus-health`).
3. The session digest, from `session_digest_service.render_session_digest`
   (`session_digest_service.py` L524-553): `<saltmdb-last-session-digest>` (a memory index, about 25%
   of the budget) followed by `<saltmdb-session-handover>` (per session the latest trace, with
   `<hint>` lines).

The compaction path skips item 3. A second hook (`saltmdb-prompt-submit-digest-retry.py`) delivers
whatever part a slow or killed first run left pending, as a separate output.

## 3. Problems found

Evidence is from one real team handover and from reading the code; it is a single sample, so treat
the numbers as estimates to re-measure.

- **Names and states.** The tag `<saltmdb-digest>` holds only core memories, and an empty block
  looks the same as a failed load. A non-text daemon reply is printed as the same blank block
  (`cli.py` L28). The compaction path skips the session section silently.
- **Repeated guidance.** The same `<hint>` strings are printed once per session and once per trace.
  Status hints (lost session, truncated text) and standing guidance (untrusted data, verify
  against the workspace) share one mechanism.
- **Markup cost.** About 659 characters of markup per session today (measured on one sample).
- **Order.** The index is emitted before the handover. The output cap keeps the head and cuts the
  tail (`cap_hook_output`, `_saltmdb_hook_common.py` L332-352), so the section placed last is cut
  first; today that is the handover.
- **Overflow.** A legal core set can render up to 15,000 characters, more than the whole hook
  limit. The error report cap (12,000) has the same problem and breaks the guarantee its own
  comment states (`config.py` L764-767).
- **Excerpts.** Prompts are cut head-only at 120 characters (`USER_MESSAGE_HEAD_CHARS`). A long
  message wrapper uses the head, so the body is lost. Single replies can exceed 10,000 characters,
  so no layout can show a trace in full.
- **The latest trace is not always the useful one.** In the sampled team the latest trace of the
  newest member was a periodic status message. The state still came through because the reply
  restated it.
- **Recall gaps unrelated to layout.** Members that ran in another working directory are invisible
  (the cohort filter is `s.cwd = ?`, `db/agent_sessions.py` L179). Members with no captured trace
  are skipped silently (`session_digest_service.py` L378). A session records an agent id, not a
  role.

## 4. Decisions

| Id | Decision |
| --- | --- |
| P1 | The revision is staged: safe structural fixes first (specs 0 and 1), content changes only after an evaluation (spec 2). |
| P2 | The core digest cap drops from 15,000 to 5,000 characters and the error report cap from 12,000 to 4,000 (BL-045). The live core set was measured empty on 2026-10-12. |
| P3 | One root element wraps the output, owned by the hook, with a legend and a status line. Sections are always present with an explicit `state` (BL-046). |
| P4 | Section names: `saltmdb-core-memories`, `saltmdb-recent-sessions`, `saltmdb-recent-memories`. Every section tag keeps the `saltmdb-` prefix, because the escaping in the session digest only neutralises `</saltmdb-`. |
| P5 | Order: core memories (the overdue notice folded into that section), recent sessions, recent memories. |
| P6 | Standing guidance appears once in the legend; per-item status becomes attributes. The legend says: excerpts are point-in-time snapshots, a recorded decision in a memory overrides trace text, a past request grants no current authority, verify against the current workspace state. |
| P7 | The hook cuts only the body and always writes the closing root tag and the cut notice. |
| P8 | The session cohort count is budget-driven with a maximum of 8, not a fixed 4. |
| P9 | Trace excerpts (spec 2): show the trace id, the lengths and a bounded excerpt of the prompt and of the reply, the same for every session including the newest. Owner direction: first 500 characters of each, and nothing longer. Open: head only versus head plus tail for the reply (section 7). |
| P10 | Under budget pressure the excerpts shrink in steps (500, 300, 150, ids only) before any session is dropped; the index shrinks to a floor of 5 lines first. |
| P11 | The "last user-authored request" feature is dropped as a built-in: it cannot be defined without knowing what a delivered peer message looks like. |
| P12 | Nothing is stripped from trace text. Removing tags inside traces was considered and not chosen: small saving, lossy. |

## 5. Rejected alternatives

- **Show full or long excerpts.** Agents that see enough text answer from it and do not fetch the
  full source. This is a documented, repeated failure with search previews, and rewording the
  instruction did not stop it; the fix has to live in the data. Hence short excerpts and ids.
- **Write the full trace to a file.** `get_trace` already retrieves it. A file adds a write at
  every session start, a store of unredacted traces and a path that differs per harness.
- **Drop no-op traces such as periodic status messages.** Their replies can carry the freshest
  state. Not adopted, and it would need knowledge of what such a message looks like.
- **A built-in classifier or envelope parser.** Contradicts the no-awareness constraint.
- **Open assignment events as an "in flight" list.** Event types are free text and there is no
  reliable terminal marker, so most threads would look open.
- **A git-state line in the shipped hook.** Git is an external tool; the harness already supplies
  it on at least one platform. Users can add it in their own hook layer.
- **Let the handover shrink to zero when the core set is large.** Contradicts the goal that the
  next session receives the previous team's work.

## 6. Parked ideas

None of these has a decision; each needs its own review.

- An optional, user-supplied description of prompt envelopes (display only, generic, empty by
  default), or a user hook that normalises prompts before they are stored. Possible shapes: render
  time patterns with named groups; a capture-time hook; a documented marker convention; a
  user-configured command. Tension with the earlier no-envelope-detection decision (BL-051).
- Context-pointer lines (one per thread the previous sessions wrote events under), gated by the
  evaluation.
- A session role label and cohorts across working directories (BL-049).
- Redaction of secrets in trace excerpts: excerpts grow about four times in the prompt, and the
  current policy is no redaction because the content already reached the model provider. Confirm
  the policy still holds (BL-047).

## 7. Open decisions for the owner

1. Reply excerpt shape: first 500 characters only (owner direction), or 300 from the head plus 200
   from the tail with an omitted-length marker. Reason to consider the tail: replies put the
   outcome first and the open question last, so a head-only cut removes the ask. Reason against:
   anything long enough to answer from tends to be used as the answer. Resolve with an evaluation
   arm.
2. Whether "four sessions" means a fixed cap or the budget-driven maximum of 8 (P8 assumes the
   latter).
3. Whether a generic prefix list or envelope description is acceptable at all (section 6).

## 8. Spec sequence and gating

| Step | Content | Backlog | Gate |
| --- | --- | --- | --- |
| Spec 0 | Lower the core digest cap and the error cap | BL-045 | None. `SPEC-CORE-CAP-LOWERING.md` |
| Spec 1 | Root envelope, section names and states, legend, order swap, overdue notice folded into the core section, hint removal, compact markup | BL-046 | After spec 0. `SPEC-BOOTSTRAP-OUTPUT-STRUCTURE.md` |
| Evaluation | Offline replay, section 9 | BL-048 | After spec 1 |
| Spec 2 | Excerpt policy (P9, P10), index floor, budget-driven cohort, length signal, cut on a line boundary | BL-047 | Written after the evaluation; not drafted yet |
| Spec 3 | Context-pointer lines | none yet | Only if the evaluation supports it |

Deploy order for each slice, as in BL-044: update the installed package, restart the daemon, then
sync the hooks. A new hook with an old CLI works as long as the hook needs no new CLI flag; the
specs keep that property.

## 9. Evaluation design

Aim: score the owner's goals directly, recall of earlier work and decision quality, not only
whether an agent can paraphrase the handover.

- **Boundaries:** the last 6 session starts in this repository whose previous cohort has at least
  one completed trace. The selection rule is fixed before running.
- **Probes per boundary (3):** the real first prompt of the next generation; a fixed "what do you do
  next and what is pending on whom?"; a fixed "summarise the previous team's work: what was done,
  decided and left open, and by which session".
- **Ground truth**, written and kept local before any run, from the full cohort traces, memories
  and version control at that time. Items: work already finished (must not be redone); stale
  requests and authority (must not be acted on); recorded decisions (must not be contradicted);
  active hazards (must not be missed); the pending item and its owner; facts that exist only past
  an excerpt cut (claiming them without a fetch is a partial-anchoring error); and for recall a
  list of `{fact, done|decided|open, owning session}` drawn from every cohort member.
- **Arms:** no session section; ids only; first 500 characters of prompt and reply; today's
  output; 400-character prompt head with a 300 head plus 200 tail reply.
- **Grading:** a separate model sees the ground-truth sheet and the response text only, with the
  arm and the bootstrap stripped. Binary per item. Scores: recall, precision (catches invented or
  half-anchored claims), attribution to the right session, per-member recall (latest ended member
  versus the others), critical decision failures.
- **Cost measures:** characters used, fetch count, tokens per run. A fetch costs a whole trace.
- **Size:** 6 boundaries x 3 probes x 5 arms x 2 samples = 180 headless runs. The power only
  detects large effects; say so in the result.
- **Bar, fixed before running:** the cheapest arm within 2 critical failures of the best and within
  10 recall points of the best, with precision no worse than today's output. If one arm wins on
  recall and another on decisions, report the split. If no arm beats "no session section" by
  more than 2 critical failures, the session section is not earning its space.
- **Replay without opening the database:** cohort and trace ids come from the CLI with `--before`
  at each boundary (that output is the "today's output" arm); excerpts come from `get_trace`
  results turned into arm text by a scratch script. No database copy and no second daemon. The
  evaluated agents get only id-addressed tools (`get_trace`, `get_memory`, `get_events`), not
  search, which would return memories created after the boundary.

## 10. Risks

- The evaluation is small and one team's traces share a house style (outcome first, ask last), so
  results may not generalise.
- Lowering the core cap is irreversible for any user whose current core set is larger: the next
  bootstrap fails closed into an error report. The live set here is empty; other installs are
  unknown, which is why spec 0 requires a measurement first and keeps the failure mode unchanged.
- Hook and CLI emit and match the same tags; an old CLI with a new hook, or the reverse, must
  degrade without losing the digest (checked per spec).

## 11. Not decided and not checked

- Whether trace capture redacts anything; whether a worktree session really records a different
  working directory (inferred from the cohort filter); the exact markup cost after the redesign;
  whether the channel wrapper an agent harness adds to delivered messages is a harness feature
  worth recognising generically.
