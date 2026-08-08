# SALTMDB Handover — Ready to Deploy Track A+B (alpha.72) to Live

**Read this first if SALTMDB itself is down, broken, or its memories aren't reachable.** This file
is self-contained on purpose — it doesn't assume you can query SALTMDB to get the rest of the
story. If SALTMDB *is* reachable, the memory IDs at the bottom have more detail; if it isn't, this
file should be enough on its own to understand what changed, why, and how to get back to a known
state.

**Written:** 2026-08-08. **Repo:** `/home/zbalint/workspace/SALTMDB`, branch `rework`, HEAD
`443bd49` (right after the commit below). 11 commits ahead of `origin/rework`, not pushed (known
sandboxed-terminal auth limitation — `git push` needs an interactive credential prompt this
environment can't satisfy; don't fight it, use the local clone-to-clone method in "How to deploy"
below).

---

## 1. What's about to be deployed

The live install (`~/.mcp/SALTMDB`, a separate git clone) is currently on `master@6bae69d`
(`v0.1.0-alpha.68`). The dev repo's `rework` branch is 11 commits ahead of that, ending at
`443bd49` (`v0.1.0-alpha.72`). In commit order:

1. `1d1cf30` `a0b83df` `e9173f5` `1593e13` `153fbfa` `af6bd0d` `125f82d` `1be6770` — search-precision
   work: supersession-chain resolution, a relevance-abstention gate (`mode="strict"`), ranking
   refinements, an optional ONNX cross-encoder reranker, and `prefer_durable_types`/
   `demote_superseded` now defaulting to `True`. All already covered by MIGRATION.md rows
   alpha.66–70 from before this session.
2. `8e87075` — **Track A: store-time disposition rewrite**. See §2.
3. `fe63c7d` — **Track B: single-owner backend daemon**. See §3.
4. `443bd49` — **this session's docs catch-up**: MIGRATION.md rows for alpha.71/72, version bump,
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
  `memory_type`/`scope` is flagged *only if it also* shows correction language, crosses the
  stricter ≥0.85 duplicate band, or is a stale consolidated node. A flagged write returns a
  `REVIEW_REQUIRED`/`REVIEW_STALE` **dict** instead of persisting — the caller must resolve it
  (`distinct` always available; `elaborate`/`supersede` against a core target; `consolidate`/
  `supersede` against a non-core target) and resubmit with the returned `review_token`.
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
  leader among with exactly one daemon process.
- **Viewer moved in-daemon**: `viewer/server.py`'s `start_viewer`/`stop_viewer` (subprocess
  management) removed; the viewer now runs as an in-daemon thread. `saltmdb-viewer` and the
  repo-root `saltmdb_viewer.py` shim (deleted) became thin `viewer_status` RPC clients.
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
- **Explicitly NOT done / not live-validated** (this is the important one for tomorrow): the plan's
  full exhaustive `test_daemon_*.py` list beyond the highest-value cases; Windows-specific paths
  (untestable from this Linux/WSL dev box); and — **no live validation that the four real MCP
  clients (Claude Code, Codex, Antigravity, Copilot) actually reconnect correctly through the new
  daemon in a real concurrent multi-client session.** Only this repo's own test suite + manual
  smoke tests (single-client, throwaway DB) have exercised this code. This is exactly what live
  deploy + supervised testing needs to confirm.

---

## 4. This session's docs catch-up (`443bd49`)

Track A and Track B both shipped without MIGRATION.md entries, a version bump, or
README/AGENT_GUIDE/INSTALL updates. Fixed:

- **MIGRATION.md**: added `v0.1.0-alpha.71` (Track A) and `v0.1.0-alpha.72` (Track B) rows.
- **Version bump**: `pyproject.toml`/`config.py` → `0.1.0-alpha.72` (was stuck at alpha.70).
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
loopback-only), so it's not a security issue, just a silently-ignored setting. Out of scope for a
docs-only pass.

---

## 5. Deploy-readiness verdict (as discussed with the user before this work started)

**Not blocked by anything on the feature roadmap** (memory `ba2cf66f` — P0 items 1–5 and P1 items
6–7 are all already in these commits; P1#8, a memory-injection hook, is deliberately sequenced
*after* this and isn't a deploy prerequisite). The real gate was the documentation debt above (now
closed) and live validation of Track B's core concurrency guarantee (now the next step, deliberately
deferred to be done together with the user rather than unattended).

**Known historical risk, worth repeating here**: an earlier session's rework-branch process once
accidentally wrote to the *live* default DB path during an unscoped smoke test, causing a 425MB
bloat incident (root-caused, DB since cleaned up, but the process risk is evergreen). **Any smoke
test or manual verification — before or after this deploy — must set `SALTMDB_DB_PATH` to a
throwaway path, never rely on the unscoped default**, unless the explicit intent is to touch the
real live DB.

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

---

## 7. Post-deploy verification checklist

- [ ] Confirm the live server actually started on the new code: reported version should be
      `0.1.0-alpha.72` (was `0.1.0-alpha.68`).
- [ ] Confirm the daemon actually spawned: `~/.saltmdb/daemon.log` should exist and show a clean
      startup (no repeated crash-loop lines). `~/.saltmdb/daemon_<key>.json` (the discovery file)
      should exist while any session is connected.
- [ ] Confirm the **daemon architecture is actually in effect**, not just old code still running:
      `ps aux | grep saltmdb` should show at most one long-lived `saltmdb.daemon.server` process
      per DB path, not one `saltmdb` server process per connected agent.
- [ ] **The big one — live multi-client validation** (Track B's core untested claim, see §3):
      with this session (Claude Code) connected, start/resume a Codex and/or Antigravity session
      against the same live DB and confirm both reach the *same* daemon process (same PID in
      `daemon.log`, not each spawning their own) and both get served correctly — try a
      `search_memory` and a `store_memory` from each. This is the one thing that genuinely can't be
      verified without real concurrent clients, which is why it's deliberately being done together
      rather than by an unattended session.
- [ ] Spot-check `search_memory` still returns sane results; try `rerank_by_topic=True` or
      `use_cross_encoder` (if `SALTMDB_RERANKER_MODEL` is set) to confirm those newer code paths
      work end to end.
- [ ] Spot-check `store_memory` on an intentionally near-duplicate write and confirm it returns a
      `REVIEW_REQUIRED` dict (Track A's disposition gate is live, not just present in code) —
      resolve it with `distinct` or `supersede` and confirm resubmission with `review_token` works.
- [ ] Confirm a Librarian pass runs without spawning a separate subprocess (check `ps aux` — no
      `python -m saltmdb --librarian` process should ever appear as its own long-lived entry now;
      it runs on the daemon's own thread pool).
- [ ] If the web viewer is enabled, confirm `saltmdb-viewer` reports it up and
      `http://localhost:8080` loads.
- [ ] If anything looks wrong: restore from the backup taken in §6, and revert the live clone with
      `cd ~/.mcp/SALTMDB && git reset --hard 6bae69d` (its pre-deploy HEAD).

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

- `2c850baa` — this session's full docs-catch-up + 2-round Codex review detail (`elaborates_on` the
  handover below).
- `a7cd9dbb` — prior handover: Track A+B both committed, full pre-docs-catch-up state.
- `3a168aac` — Track B's full 3-round diff-review detail (the "how was this verified" story,
  including the SIGTERM deadlock bug and its empirical 500/500 test-discrimination proof).
- `3bcdcab3` — Track A's full implementation + review detail.
- `ba2cf66f` — the post-rework feature roadmap (P1#8, the memory-injection hook, is next after this
  if no other direction is given).
- `4ab4cbc9` / `b1d176d8` — the prior live-DB-bloat incident writeup (why §5's warning exists).
- `77aef47e` — the deploy-method precedent this file's §6 is based on.
- `75de9da4` / `5a4aae8e` — the Codex CLI review recipe and invocation footguns used throughout.

---

## 10. What's next (explicitly deferred, to be done together)

Live-conditions validation: deploy per §6, then work through the §7 checklist together —
especially the concurrent-multi-client item, which is the one thing this session's automated
testing structurally cannot verify on its own.
