# SALTMDB Handover — alpha.72 (Track A+B) LIVE and Verified, Next Up: Windows

**Read this first if SALTMDB itself is down, broken, or its memories aren't reachable.** This file
is self-contained on purpose — it doesn't assume you can query SALTMDB to get the rest of the
story. If SALTMDB *is* reachable, the memory IDs at the bottom have more detail; if it isn't, this
file should be enough on its own to understand what changed, why, and how to get back to a known
state.

**Written:** 2026-08-08 (pre-deploy). **Updated:** 2026-08-08, same day, after the deploy actually
happened and was verified live with the user (including the one thing that genuinely needed real
concurrent clients — see §7 and §11). **Session is stopping now, deliberately, before the next
planned step (Windows validation, see §12)** — read §11/§12 first if you're picking this up fresh.

**Repo:** `/home/zbalint/workspace/SALTMDB`, branch `rework`, HEAD `6296c62` (this file's original
commit). 11 commits ahead of `origin/rework`, not pushed (known sandboxed-terminal auth limitation
— `git push` needs an interactive credential prompt this environment can't satisfy; don't fight it,
use the local clone-to-clone method in §6 below).

---

## 1. What was deployed

The live install (`~/.mcp/SALTMDB`, a separate git clone) **is now on `rework@6296c62`
(`v0.1.0-alpha.72`), deployed and verified 2026-08-08.** It was previously on `master@6bae69d`
(`v0.1.0-alpha.68`). In commit order, what shipped:

1. `1d1cf30` `a0b83df` `e9173f5` `1593e13` `153fbfa` `af6bd0d` `125f82d` `1be6770` — search-precision
   work: supersession-chain resolution, a relevance-abstention gate (`mode="strict"`), ranking
   refinements, an optional ONNX cross-encoder reranker, and `prefer_durable_types`/
   `demote_superseded` now defaulting to `True`. All already covered by MIGRATION.md rows
   alpha.66–70 from before this session.
2. `8e87075` — **Track A: store-time disposition rewrite**. See §2.
3. `fe63c7d` — **Track B: single-owner backend daemon**. See §3.
4. `443bd49` — **docs catch-up**: MIGRATION.md rows for alpha.71/72, version bump,
   README/AGENT_GUIDE/INSTALL corrections. See §4.

Full test suite: **651/651 passing** as of `443bd49` (`PYTHONPATH=src SALTMDB_DB_PATH=<throwaway>
.venv/bin/python3 -m unittest discover -s tests -p "test_*.py"` — use the repo's own `.venv`, not
bare `python3`, since the `mcp` SDK is only installed there).

---

## 2. Track A — store-time disposition rewrite (`8e87075`)

Replaces the old async Librarian review queue with a synchronous preflight on `store_memory`.

- **Deleted**: `librarian_service.py`'s `consolidate_vector_clusters`/`scout_consolidated_supersessions`
  scanners (905 lines + 10 helpers), the 4 constants that calibrated them, and
  `memory_service._handle_supersession_candidate` (the old auto-`similar_to`-linking path).
- **New**: `domain/services/disposition_service.py` — `evaluate_store_preflight` (side-effect-free)
  and `commit_disposed_write` (atomic, in-transaction TOCTOU-safe).
- **New caller-facing contract**: on a brand-new write (skipped when the call already resolves to
  an existing entity via explicit `entity_id` or a same-title/owner/scope match, or when
  `skip_duplicate_check=True`), a candidate at ≥0.75 cosine similarity with compatible
  `memory_type`/`scope` (both must match exactly — confirmed live, see §11) is flagged *only if it
  also* shows correction language, crosses the stricter ≥0.85 duplicate band, or is a stale
  consolidated node. A flagged write returns a `REVIEW_REQUIRED`/`REVIEW_STALE` **dict** instead of
  persisting — the caller must resolve it (`distinct` always available; `elaborate`/`supersede`
  against a core target; `consolidate`/`supersede` against a non-core target) and resubmit with the
  returned `review_token`.
- **One-time DB migration**: `db/schema.py` gained a `PRAGMA user_version`-gated migration that
  auto-dismisses the legacy `consolidation_request`/`supersession_candidate` event backlog via the
  existing `dismiss_events` mechanism — runs once, idempotent, on `init_db`.
- **Review trail**: 3 rounds of Codex CLI plan-review + 3 rounds of Codex CLI implementation-review
  (`codex exec -s read-only`), all findings fixed and re-verified. 637/637 tests passing at the
  time this commit was made.

---

## 3. Track B — single-owner backend daemon (`fe63c7d`)

Introduces `src/saltmdb/daemon/` (`discovery.py`, `protocol.py`, `dispatch.py`, `client.py`,
`server.py`, `platform_paths.py`). Exactly one daemon process now opens SQLite for a given DB path;
every MCP client (Claude Code, Codex, Antigravity, Copilot) and most CLI entrypoints
(`python -m saltmdb`, `--librarian`, `--backfill-chunk-embeddings`) become thin RPC clients over
loopback TCP (length-prefixed JSON framing), auto-spawning the daemon on first connect if none is
running. `saltmdb-viewer` is the one exception — a read-only status client that requires an
already-running daemon and never spawns one.

- **Ownership arbitration**: a bind-only guard socket on a per-DB-path "election port" (derived
  into the `49500`–`65499` range) — the daemon binds it for its whole lifetime and never
  `accept()`s; a losing contender's bind fails almost instantly and it exits without touching the
  DB. No stale-lock file a crashed process could leave behind.
- **Auto-shutdown**: a 30s grace timer (`DAEMON_SHUTDOWN_GRACE_PERIOD_S`) starts both at daemon
  startup itself and whenever the session count returns to zero — so even a daemon spawned only to
  service a one-shot RPC (no session ever opened) still shuts itself down on schedule. An in-flight
  RPC blocks the timer. `saltmdb-daemon --foreground` disables the timer entirely.
- **Librarian moved in-daemon**: no more detached `python -m saltmdb --librarian` subprocess. Runs
  on the daemon's own single-worker `_librarian_trigger_pool`; a `run_librarian_now` RPC replaces
  the old CLI subprocess path. The old cross-process leader-election lock (`db/locks.py`,
  `acquire_librarian_lock`/`release_librarian_lock`) is retired outright — nothing left to elect a
  leader among with exactly one daemon process. **Confirmed live, see §11.**
- **Viewer moved in-daemon**: `viewer/server.py`'s `start_viewer`/`stop_viewer` (subprocess
  management) removed; the viewer now runs as an in-daemon thread. `saltmdb-viewer` and the
  repo-root `saltmdb_viewer.py` shim (deleted) became thin `viewer_status` RPC clients. **Confirmed
  live** (HTTP 200 on `:8080`); note the legacy `~/.saltmdb/viewer_8080.pid` file still carries a
  stale test-mock artifact from a pre-existing, already-known bug (Track B doesn't read that file
  anymore, so it's cosmetic, not a regression from this deploy).
- **Logging**: a spawned background daemon's stdout/stderr redirect to `daemon.log` (same directory
  as `saltmdb.db` — for the live default path, that's `~/.saltmdb/daemon.log`), replacing the old
  separate `librarian.log`/`viewer.log`. A `--foreground`-launched daemon logs to the terminal as
  normal instead.
- **Discovery files**: live at `~/.saltmdb/daemon_<key>.json` (one per DB path). If a fresh session
  seems unable to reach SALTMDB at all, this file (or its absence) plus `~/.saltmdb/daemon.log` are
  the first two things to check.
- **No new dependencies** — the daemon package is stdlib-only (`socket`/`socketserver`/`threading`/
  `subprocess`/`signal`/`hmac`). No schema/data changes.
- **Review trail**: 5 rounds of Codex CLI plan-review + 3 rounds of Codex CLI implementation-review.
  Round 1 found a real SIGTERM/SIGINT shutdown deadlock (signal handler blocking on the same thread
  `serve_forever()` was blocked in) plus 5 other blocking issues; round 2 found the round-1
  admission-race fix was incomplete plus a params-validation hole; round 3 confirmed both fixes and
  found only a test-rigor nit, empirically verified (reverted the fix, re-ran the hardened test 500
  times: 500/500 failures on broken code, 0/500 on fixed code). 651/651 tests passing, plus real
  background-spawn smoke tests independent of the unit suite (real RPCs, real SIGTERM shutdown,
  discovery-file lifecycle).
- **Was NOT done / not live-validated as of the original write-up — status now, see §11/§12**: the
  plan's full exhaustive `test_daemon_*.py` list beyond the highest-value cases (still not done, low
  priority); **real concurrent multi-client validation — DONE, confirmed live 2026-08-08** (Claude
  Code + Codex + Antigravity simultaneously against the same daemon, see §11); **Windows-specific
  paths — still not done**, untestable from this Linux/WSL dev box, this is what's next (§12).

---

## 4. Docs catch-up (`443bd49`)

Track A and Track B both shipped without MIGRATION.md entries, a version bump, or
README/AGENT_GUIDE/INSTALL updates. Fixed:

- **MIGRATION.md**: added `v0.1.0-alpha.71` (Track A) and `v0.1.0-alpha.72` (Track B) rows.
- **Version bump**: `pyproject.toml`/`config.py` → `0.1.0-alpha.72` (was stuck at alpha.70).
  **Confirmed live** — both files on the live clone report `0.1.0-alpha.72`.
- **README.md / AGENT_GUIDE.md / INSTALL.md**: rewrote the architecture diagram, the Librarian
  section, the viewer-launch instructions, and the `store_memory`/disposition-flow descriptions to
  match actual current behavior (some of this staleness — the disposition-flow docs — predated
  this session, left over from Track A's original commit).

Verified via **2 rounds of independent Codex CLI review** against just the docs diff (not folded
in blindly — every finding cross-checked against actual source first):
- Round 1 found 8 real inaccuracies (`saltmdb-viewer` doesn't auto-spawn a daemon; the Track A
  preflight is bypassed on resolved-entity/`skip_duplicate_check` writes; the flagging rule doesn't
  require correction language; dispositions differ by target `is_core`; stale Track-A-era claims
  about removed auto-linking/scanner behavior; `SALTMDB_VIEWER_HOST` isn't consumed by the daemon;
  the grace timer also starts at daemon startup and `--foreground` disables it; `daemon.log`
  redirection only applies to a spawned background daemon).
- Round 2 confirmed all 8 fixed, found 4 smaller wording overclaims, also fixed.

**Real bug found, not fixed (flagged for follow-up, not blocking deploy)**: `SALTMDB_VIEWER_HOST`
is defined in `config.py` (`get_viewer_host()`) but genuinely unused — `daemon/server.py` hardcodes
`("127.0.0.1", viewer_port)` for the viewer bind regardless of this env var. Fails safe (stays
loopback-only), so it's not a security issue, just a silently-ignored setting. Still open, still
out of scope, not touched during the §11 verification pass either.

---

## 5. Deploy-readiness verdict (pre-deploy discussion, kept for history)

**Not blocked by anything on the feature roadmap** (memory `ba2cf66f` — P0 items 1–5 and P1 items
6–7 are all already in these commits; P1#8, a memory-injection hook, is deliberately sequenced
*after* this and isn't a deploy prerequisite). The real gate was the documentation debt (closed,
§4) and live validation of Track B's core concurrency guarantee (now done, §11).

**Known historical risk, worth repeating here**: an earlier session's rework-branch process once
accidentally wrote to the *live* default DB path during an unscoped smoke test, causing a 425MB
bloat incident (root-caused, DB since cleaned up, but the process risk is evergreen). **Any smoke
test or manual verification — before or after this deploy — must set `SALTMDB_DB_PATH` to a
throwaway path, never rely on the unscoped default**, unless the explicit intent is to touch the
real live DB. (The live-DB writes done for §11's verification were intentional and are documented
there, including cleanup.)

---

## 6. How to deploy (the established, working method — do NOT use `git push`)

`git push origin <branch>` fails in this sandboxed environment (`fatal: User cancelled dialog`, no
`/dev/tty` for the interactive credential prompt) — this is a known, accepted limitation, not
something to retry. The proven method for syncing the dev repo into the live clone
(`~/.mcp/SALTMDB`, itself a separate `git clone` of `github.com/zbalint/SALTMDB`) is local
clone-to-clone:

```bash
cd ~/.mcp/SALTMDB
git fetch /home/zbalint/workspace/SALTMDB rework   # or whichever branch you're deploying
# Verify the live clone's current HEAD is an ancestor of the incoming commit BEFORE merging:
git merge-base --is-ancestor HEAD FETCH_HEAD && echo "safe fast-forward" || echo "STOP -- not a fast-forward, investigate first"
git merge --ff-only FETCH_HEAD
```

Live install dependencies (`mcp`, `sqlite-vec`, `fastembed`, `numpy`) are unchanged/already pinned
identically — a plain code sync is sufficient, no `pip install` needed for this deploy. No new
schema/data migration is needed either (both Track A's and Track B's DB-facing changes are fully
automatic via `init_db`, confirmed in the MIGRATION.md rows above) — just the code sync.

**Take a fresh DB backup immediately before syncing**, as a restore point:
```bash
cp ~/.saltmdb/saltmdb.db ~/.saltmdb/saltmdb.db.bak_pre_alpha72_deploy_$(date +%Y%m%d_%H%M)
```

(This was done: `~/.saltmdb/saltmdb.db.bak_pre_alpha72_deploy_*` exists as the restore point for
this specific deploy.)

---

## 7. Post-deploy verification checklist — ALL DONE (2026-08-08)

- [x] Live server on new code: confirmed `0.1.0-alpha.72` on both `pyproject.toml` and `config.py`
      in the live clone (was `0.1.0-alpha.68`).
- [x] Daemon spawned, clean log: `daemon.log` shows the expected startup sequence. (The user's
      initial connection failure was two stale old-code processes — a viewer and a server — holding
      the probe port 57101 and viewer port 8080; `daemon.log` shows the exact symptom, repeated
      `Address already in use` from 14:49:00–14:49:32, then a clean bind at 14:51:00 once those were
      killed.) Discovery file `~/.saltmdb/daemon_<key>.json` fresh and matches the live daemon.
- [x] Daemon architecture actually in effect: `ps aux` showed exactly one long-lived
      `saltmdb.daemon.server` process throughout, even with 3 concurrent clients connected (see
      next item) — never more than one.
- [x] **The big one — live multi-client validation**: DONE. See §11 for the full story. Claude Code
      + Codex + Antigravity all connected simultaneously, all served correctly by the same daemon
      PID, cross-client state visible from a third session. This was the one thing that couldn't be
      verified by automated testing alone.
- [x] `search_memory` spot-checked, sane results, `rerank_by_topic=True` exercised.
- [x] `store_memory` `REVIEW_REQUIRED` gate spot-checked: a real round-trip (flag → `distinct` on
      all 5 flagged candidates → resubmit with `review_token` → commit) confirmed live. Test writes
      archived afterward, live DB left clean. See §11 for the nuance discovered along the way
      (flagging requires the proposed write's `scope` **and** `memory_type` to both exactly match
      the candidate's — not previously documented this precisely).
- [x] Librarian pass confirmed running in-daemon: `SALTMDB_DB_PATH=... saltmdb-server --librarian`
      (live clone's own console script) triggered a real pass logged straight into `daemon.log`,
      no subprocess appeared in `ps aux`, and the legacy `librarian.log` file stayed untouched.
- [x] Web viewer confirmed up: `http://localhost:8080` returns HTTP 200 via the in-daemon thread.
- [ ] *(Not needed — nothing looked wrong. Rollback instructions in §8 remain valid if ever
      needed.)*

---

## 8. If SALTMDB itself won't start or isn't reachable

1. Check `~/.saltmdb/daemon.log` first — it's the daemon's own log, independent of any client.
2. Try a manual foreground launch for live error output — run from `~/.mcp/SALTMDB` using **that**
   clone's own installed console script, not the dev repo's:
   `SALTMDB_DB_PATH=~/.saltmdb/saltmdb.db saltmdb-daemon --foreground`
   (or `python -m saltmdb.daemon.server --foreground` with that clone's own interpreter on `PATH`).
3. If it's a code problem introduced by this deploy: `cd ~/.mcp/SALTMDB && git reset --hard 6bae69d`
   to revert to the known-good pre-deploy state, then restart any client session.
4. If it's a DB problem: restore the backup taken in §6
   (`~/.saltmdb/saltmdb.db.bak_pre_alpha72_deploy_*`).
5. This file (`HANDOVER.md`) lives in the **dev repo** (`/home/zbalint/workspace/SALTMDB`), tracked
   in git — it survives independently of whatever state the live install or its DB end up in.

---

## 9. Key SALTMDB memory IDs (if SALTMDB is reachable — more detail than this file carries)

- `5ec9b2a0` — **live concurrent multi-client validation** (Claude Code + Codex + Antigravity), the
  full §11 story in detail, `elaborates_on` `2c26d59a`.
- `2c26d59a` — the rest of the live post-deploy verification pass (daemon, librarian, viewer, Track
  A disposition-gate round-trip), `elaborates_on` `2c850baa`.
- `f2eec735` — minor unrelated recurring friction note: `TITLE_MAX_LENGTH=120` in
  `memory_service.py` has now caused friction 3 times (trivial fix, 120→150, not yet scheduled).
- `2c850baa` — docs-catch-up + 2-round Codex review detail (`elaborates_on` the handover below).
- `a7cd9dbb` — prior handover: Track A+B both committed, full pre-docs-catch-up state.
- `3a168aac` — Track B's full 3-round diff-review detail (the "how was this verified" story,
  including the SIGTERM deadlock bug and its empirical 500/500 test-discrimination proof).
- `3bcdcab3` — Track A's full implementation + review detail.
- `ba2cf66f` — the post-rework feature roadmap (P1#8, the memory-injection hook, is next after
  Windows validation if no other direction is given).
- `4ab4cbc9` / `b1d176d8` — the prior live-DB-bloat incident writeup (why §5's warning exists).
- `77aef47e` — the deploy-method precedent this file's §6 is based on.
- `75de9da4` / `5a4aae8e` — the Codex CLI review recipe and invocation footguns used throughout.

---

## 10. (superseded by §11 — kept as a pointer)

The old "what's next, deferred" section here was the live-conditions validation work. It's done —
see §11 for what was actually found, and §12 for what's next now.

---

## 11. Live post-deploy verification — what actually happened (2026-08-08)

Full detail in memories `2c26d59a` and `5ec9b2a0`; summary here so this file stays self-contained.

**Connection hiccup (matches the design, not a bug)**: on first connecting a fresh session after
the code sync, SALTMDB was unreachable. Cause: stale pre-deploy viewer and server processes were
still holding the probe port (57101) and the viewer port (8080) that the new daemon needs to bind.
Killing those two processes and restarting the client session fixed it — `daemon.log` shows this
exactly (repeated bind failures 14:49:00–14:49:32, clean bind at 14:51:00). Worth knowing for next
time: **after syncing new daemon code onto a box that had an old-code SALTMDB running, kill any
lingering old viewer/server processes before expecting a clean connect.**

**Everything in the §7 checklist passed** — version, daemon singleton, discovery file, librarian
in-daemon (no subprocess), viewer in-daemon (HTTP 200), `search_memory`, and a full live
`REVIEW_REQUIRED` round-trip on `store_memory` (flag → resolve `distinct` on all 5 candidates →
resubmit with `review_token` → commit; test data archived after).

**One nuance surfaced while testing the disposition gate that wasn't previously documented this
precisely**: the flagging rule requires the proposed write's `scope` *and* `memory_type` to both
match a candidate's *exactly* (not just "compatible" in some loose sense). Several early test
writes with mismatched `scope`/`memory_type` silently passed straight through with no flag — this
is the gate working as designed, not a bug, but it's easy to misread as "the gate isn't working" if
you don't know this. `check_duplicates_only=True` is the fast way to see raw similarity scores when
a flag doesn't fire and it's unclear why.

**The big one — live concurrent multi-client validation, Track B's core untested claim — DONE**:
with this Claude Code session already connected, the user started a Codex CLI session and an
Antigravity (Gemini CLI) session in parallel against the same live DB. Result:
- 3 separate `saltmdb_server.py` stdio client processes appeared in `ps aux`, one per agent.
- The daemon itself (`saltmdb.daemon.server`) stayed exactly **one** process throughout — no second
  daemon spawn attempt, no election-port contention, discovery file never changed.
- Codex and Antigravity each independently ran a real `search_memory` and a real `store_memory`
  against the live daemon — both succeeded, no errors, no timeouts.
- Their self-reported result entity IDs were then independently fetched from this (third) Claude
  Code session and matched their self-reported content exactly — proving genuine shared-daemon/
  shared-DB state, not just three separate processes that happened to run.
- `daemon.log` activity (a viewer scatterplot request, a librarian auto-trigger tag-merge pass)
  landed on the single daemon PID with timestamps matching the clients' reported action times.
- Both test-probe memories were archived immediately after; live DB left clean.

**Conclusion**: Track B's single-owner-daemon design holds under real concurrent load from 3
different agent implementations. This closes the last item HANDOVER.md originally flagged as
untestable by automation.

---

## 12. What's next: Windows validation (session stopping here, deliberately)

The one remaining item from Track B's original scope (§3) that's still genuinely unverified:
**Windows-specific paths**. `platform_paths.py` was written to be cross-platform, but every test
above — including the concurrent multi-client one — ran from this Linux/WSL dev box. No real
Windows process has ever connected to a live daemon.

**This session is stopping now, before that test happens**, at the user's request. Whoever picks
this up next (could be a fresh session, could be this same user driving a Windows-side client):

- Nothing above needs re-deriving — the deploy is live, verified, and stable as of this write-up.
- The open question is specifically: does a real Windows-native MCP client (or the daemon itself,
  if it's expected to run natively on Windows rather than only WSL) correctly discover/connect to
  the daemon, respect the same discovery-file/port-election protocol, and behave under the same
  kind of concurrent load just proven on Linux? `platform_paths.py` is the file that matters most
  to read first.
- No specifics about the Windows test setup (native Windows process vs. WSL-mounted path, same DB
  file or a separate one, etc.) were decided before this session ended — that's the first thing to
  clarify with the user when this resumes.
- Unrelated minor open item, not blocking, surfaced during this session: `TITLE_MAX_LENGTH=120` in
  `memory_service.py` has now caused real friction 3 times (memory `f2eec735`) — trivial fix
  (120→150), worth doing in the next small-cleanup pass.
