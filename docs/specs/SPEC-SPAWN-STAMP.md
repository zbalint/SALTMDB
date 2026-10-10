# SPEC-SPAWN-STAMP

## 0. Status

LOCKED 2026-10-10.

- Backlog item: BL-019. Shared `context_id`: `perf-spawn-stamp-2026-10-10`.
- Baseline: the commit that adds this spec on `develop`, plus the `./verify` counts the developer records first.
- Location and branch: main checkout `/home/zbalint/workspace/SALTMDB`, branch `develop`.
- Test seams: `client._spawn_if_due` (observed through a patched `client._spawn_daemon_subprocess`) and the new
  `discovery.spawn_stamp_path`.
- Scope (may edit): `src/saltmdb/daemon/client.py` (`_spawn_if_due`, about L99-108, and one new function above it),
  `src/saltmdb/daemon/discovery.py` (one new function next to `discovery_path`, L59), `tests/test_daemon_client.py`,
  `tests/test_mcp_server.py` and `tests/test_rpc_backend_starting.py` only if the stamp-leak check in acceptance 4 shows a leak
  there (neither is expected to; they patch `begin_lazy_start` and `_spawn_if_due`).
- Does not touch: `daemon/server.py`, `daemon/service.py`, `config.py`, the spawn argv or env, the intermediary
  launcher, docs.
- Hand-off rule: the developer leaves the diff uncommitted, unstaged and unmerged for review.

## 1. Why

Every adapter, hook and CLI call that finds no daemon spawns its own. The 15 s spawn throttle
(`DAEMON_SPAWN_MIN_INTERVAL_S`, `client.py` `_last_spawn_at`) is per process, so K parallel starters spawn K daemons.
`~/.saltmdb/daemon.log` shows 458 `Daemon process starting` lines, five within 100 ms at 2026-10-10 06:47:45 and
peaks of 25 per minute. Each loser imports about 330 modules (0.46 s warm, about 39 MB, slower with a cold page cache)
before it loses the election bind in `daemon/server.py`, which adds load exactly while a cold host is least able to take it
(BL-016's disk stalls, BL-014).

Baseline probe (throwaway script, six processes released at once by a barrier, `_spawn_daemon_subprocess` replaced by
a counter, temp HOME): `concurrent_adapters 6 spawns 6`, exit 1.

Decision: one cross-process spawn stamp per database. Whoever takes an exclusive `flock` on the stamp file and finds the
stamp older than the interval (or missing) writes the new time and spawns; everyone else, within the interval, does not
spawn and keeps waiting for the daemon through the existing progress-aware wait. A crashed daemon is respawned by
whichever process next calls `_spawn_if_due` after the interval, as today. Known consequence, accepted: if the spawning
process's daemon dies before it binds the probe port, a tool or CLI call that joined the storm can fail after about 13 s
(`ensure_daemon_running` legacy window) without spawning; the next call, or the 120 s lazy-start loop, respawns once the stamp is
stale. A starting daemon (answers the probe as `initializing`) is waited for by the existing progress-aware loop, unchanged.

Rejected (do not re-explore without new evidence): moving the heavy imports in `daemon/server.py` below the election
bind (the five-spawn burst arrives within 100 ms, so every contender would still pass a cheap pre-check; also a
large, risky restructure of module-level imports); a lock held until the daemon is up (blocks the tool path);
`SO_REUSEADDR` style tricks (BL-014 is separate).

## 2. Decisions

- D1. New `discovery.spawn_stamp_path(key: str) -> str`: `os.path.join(_discovery_dir(), f"spawn_{key}.stamp")`.
- D2. New function in `client.py`, `_claim_spawn_slot(db_path: str) -> bool`, placed above `_spawn_if_due`. It returns True
  when the caller should spawn. Behaviour: derive the key as `reachable_daemon_info` does (`discovery.resolve_canonical_db_path`
  then `discovery.daemon_key`); open the stamp path with `os.open(path, os.O_RDWR | os.O_CREAT, 0o600)`; `fcntl.flock(fd,
  fcntl.LOCK_EX)`; read the stored text; if it parses as a float `t` and `0 <= time.time() - t < DAEMON_SPAWN_MIN_INTERVAL_S`
  return False (stamp fresh); otherwise truncate, write `repr(time.time())`, return True. Always close the descriptor
  (which releases the lock). A future-dated or unparseable stamp counts as stale. It uses wall-clock time because the stamp is
  shared between processes.
- D3. Fail open: if `fcntl` cannot be imported (win32), or `os.open`/`flock`/read/write raise `OSError`, log one WARNING with the
  exception and return True, so the old per-process behaviour is the fallback and a spawn is never prevented by a broken
  stamp. The win32 branch carries a comment `# shortcut: no cross-process throttle on win32 (no fcntl); use msvcrt.locking if
  Windows spawn storms matter`.
- D4. `_spawn_if_due` keeps its in-process check and `_spawn_lock` as they are, then calls `_claim_spawn_slot(db_path)`; when it
  returns False, return False WITHOUT touching `_last_spawn_at` (the claim is cheap and the wait loops call `_spawn_if_due` at most
  every ~2 s, so a refused process retries and spawns as soon as the shared stamp is stale; setting `_last_spawn_at` on refusal would
  push its own next spawn out to refusal time plus 15 s and starve the legacy retry window). When True, behave as today (set
  `_last_spawn_at`, call `_spawn_daemon_subprocess`, return True).
- D5. `_claim_spawn_slot` is a separate function (not inlined) because it is the cross-process primitive with its own tests;
  it has exactly one caller.

## 3. Changes per file

`discovery.py`: D1. `client.py`: D2 to D4; import `fcntl` inside the function (a top-level import would break win32 import).
No other change.

## 4. Tests

Written first; red against the baseline for the right reason (the function does not exist, or the six-process probe
spawns six).

- `tests/test_daemon_client.py`, new class `TestSpawnStamp` with a temp directory and `patch.object(discovery, "_discovery_dir",
  return_value=tmp)`: (a) no stamp: `_claim_spawn_slot` returns True and the file now holds a float; (b) a second call within
  the interval returns False; (c) stamp older than the interval returns True and rewrites it; (d) a future-dated stamp returns
  True; (e) garbage content returns True; (f) `os.open` raising `OSError` returns True and logs a warning; (g) `fcntl` import
  failing (patch `builtins.__import__` or `sys.modules["fcntl"] = None`) returns True; (h) `_spawn_if_due`: with the claim False,
  `_spawn_daemon_subprocess` is not called and the function returns False; with the claim True it is called once.
- Existing tests: only `TestColdStartWait` (`tests/test_daemon_client.py`, setUp about L805-830, spawning tests about L842, L870,
  L925-933) reaches the real `_spawn_if_due`, and all its tests share the fixed path `/tmp/cold-start-test.db`, hence one stamp key.
  Apply exactly one fix: in its setUp add `patch.object(client, "_claim_spawn_slot", return_value=True)` (started and cleaned up
  like the neighbouring patches). Do not change `_StartupClock`, and do not let any test write a stamp into the real
  `~/.saltmdb` (a leaked stamp under a fake clock would stay fresh forever and break later runs). The cross-process logic is tested
  only in `TestSpawnStamp`, with a temp `_discovery_dir`.

## 5. Out of scope

Moving imports in `server.py`, changing the throttle interval, the persistent-daemon commands, win32 locking, cleaning up old
stamp files (they are a few bytes and keyed by database).

## 6. Acceptance

1. Before editing: `./verify`; record counts and pre-existing failures.
2. Tests written first and red as stated; after implementing,
   `.venv/bin/python -m pytest tests/test_daemon_client.py tests/test_mcp_server.py tests/test_rpc_backend_starting.py -q` exits 0.
3. Storm probe, run twice, must exit 0 printing `spawns 1` (baseline: `spawns 6`, exit 1):

   ```sh
   .venv/bin/python - <<'EOF'
   import multiprocessing as mp, os, tempfile
   mp = mp.get_context("fork")
   def worker(db, counter, barrier):
       from saltmdb.daemon import client
       def fake_spawn(path):
           with open(counter, "a") as f:
               f.write(f"{os.getpid()}\n")
       client._spawn_daemon_subprocess = fake_spawn
       barrier.wait()
       client._spawn_if_due(db)
   if __name__ == "__main__":
       home = tempfile.mkdtemp()
       os.environ["HOME"] = home
       db = os.path.join(home, "x.db")
       counter = os.path.join(home, "spawns.txt")
       n = 6
       barrier = mp.Barrier(n)
       ps = [mp.Process(target=worker, args=(db, counter, barrier)) for _ in range(n)]
       [p.start() for p in ps]; [p.join() for p in ps]
       spawns = len(open(counter).read().split()) if os.path.exists(counter) else 0
       print("concurrent_adapters", n, "spawns", spawns)
       raise SystemExit(0 if spawns == 1 else 1)
   EOF
   ```
4. Stamp-leak check: `ls ~/.saltmdb | rg 'spawn_.*stamp'` before and after the full `./verify` shows the same output (the tests
   must not leave stamp files in the real directory). Report both listings.
5. `./verify` exits 0. One full suite at a time.
6. `git status --short` lists only files named in section 0.

## 7. Pre-lock gate notes

- The probe in 6.3 was run on the unchanged code at lock time (6 spawns, exit 1); the green result is a post-implementation check.
- Content search of `_last_spawn_at`, `_spawn_if_due`, `DAEMON_SPAWN_MIN_INTERVAL_S` across `src` and `tests`: callers are
  `client.py` L118, L464, L502; `service.py` L274 keeps its own separate throttle for `daemon start`; tests patch it in
  `test_daemon_client.py`, `test_rpc_backend_starting.py`.
- Out of scope but noted: `daemon/service.py` L279 (`daemon start`) calls `_spawn_daemon_process` directly with its own throttle and
  can add one harmless extra contender; hooks never spawn (no spawn or daemon import in `hooks/*.py`).
- Consultant pre-lock review done (D4 timing, one prescribed test fix, wrong cross-references, probe portability: the probe pins the
  `fork` start method because the installed Python 3.14 defaults to `forkserver`).
- `discovery._discovery_dir()` creates `~/.saltmdb` and is the same directory as the discovery file, so one file per database key.
