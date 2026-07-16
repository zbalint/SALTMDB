# SALTMDB Code Review — Findings & Proposed Fixes

**Scope:** Review of `saltmdb_server.py`, `AGENT_GUIDE.md`, and `scratch/test_db.py` from
`zbalint/SALTMDB` (commit at time of review: master, 20 commits).

**Context for the reviewing agent:** SALTMDB replaces a prior workflow where every CLI
agent session dumped a markdown file, occasionally consolidated by hand into
`memory.md`. The design goals are: (1) lower token cost than re-reading/re-consolidating
full markdown files, (2) durable memory of past mistakes/decisions across sessions *and*
across projects, (3) explicit typed connections between memory items. Findings below are
evaluated against those three goals, not just "is this good code."

Each finding includes: what was observed, why it matters, and a proposed fix. Findings
are ordered by priority, not by file location.

---

## 1. `analyze_dependencies` cycle detection is title-based, not ID-based

**File:** `saltmdb_server.py`, line ~1390 (inside `analyze_dependencies`)

**Finding:**
```sql
AND dt.path NOT LIKE '%' || child.title || '%'
```
The recursive CTE prevents infinite traversal loops by checking whether the current
node's **title text** already appears in the accumulated path string, instead of
checking node **IDs**.

**Why it matters:**
This is the core function behind the "connections between memories" feature — the
thing you specifically wanted this tool to do that the old markdown-file approach
couldn't. Two problems follow from using title text as the cycle guard:

- **False positives:** SALTMDB's own SCD versioning (`store_knowledge` with an
  existing `entity_id`) creates historical rows that often retain the same or a
  very similar title as the active row. If two *unrelated* entities happen to
  share title substrings (e.g. "Auth Error" and "Auth Error (Updated)", or two
  different projects both producing a memory titled "Database setup rule"), the
  traversal will silently truncate a real, valid dependency path, thinking it hit
  a cycle.
- **False negatives (weaker but present):** title matching via `LIKE '%...%'` is
  substring matching, not exact — a title that's a substring of another node's
  title can trigger early termination even without any real relationship overlap.

Since this function's whole purpose is to make cross-memory relationships
trustworthy, a correctness bug here undermines the main feature that
differentiates SALTMDB from just grepping old markdown files.

**Proposed fix:**
Track visited entity IDs in the recursive CTE instead of matching on title
text, e.g. accumulate a `path` of concatenated IDs (with a delimiter that
cannot appear in a UUID, such as `,`) and check that instead:

```sql
WITH RECURSIVE dependency_tree(id, title, status, depth, id_path, title_path) AS (
    SELECT e.id, e.title, e.status, 0, ',' || e.id || ',', e.title
    FROM entities e
    WHERE e.id = ? AND e.status != 'archived'

    UNION ALL

    SELECT child.id, child.title, child.status, dt.depth + 1,
           dt.id_path || child.id || ',',
           dt.title_path || ' -> ' || child.title
    FROM entities child
    JOIN relations r ON r.target_id = child.id
    JOIN dependency_tree dt ON r.source_id = dt.id
    WHERE child.status != 'archived' AND r.valid_to IS NULL
      AND dt.id_path NOT LIKE '%,' || child.id || ',%'
)
SELECT DISTINCT id, title, status, depth, title_path FROM dependency_tree;
```
Return `title_path` (renamed from `path`) for the human/agent-readable output, but
use `id_path` strictly for cycle detection.

---

## 2. `commit_consolidation` deletes parent entities without checking for `relations` rows referencing them

**File:** `saltmdb_server.py`, `commit_consolidation`, lines ~1128–1131

**Finding:**
```python
if parent_ids:
    placeholders = ",".join("?" for _ in parent_ids)
    conn.execute(f"DELETE FROM entities WHERE id IN ({placeholders})", parent_ids)
```
This is a hard `DELETE`, not an archive. The `relations` table has
`FOREIGN KEY (source_id) REFERENCES entities(id) ON DELETE CASCADE` and
`FOREIGN KEY (target_id) REFERENCES entities(id) ON DELETE CASCADE` — so if a
raw memory being consolidated away was one endpoint of a `store_relation` edge,
deleting it silently cascades and deletes the relation row too. There's no
warning, no re-pointing of the edge to the new consolidated entity, and no log
of what was lost.

**Why it matters:**
This directly conflicts with the "connections between memories" goal. Consider:
you link memory A (`fix`) to memory B (`issue`) with `store_relation`. Later,
the Librarian flags A for consolidation because its tag got cluttered. The
agent consolidates A into a new summary entity C via `commit_consolidation`.
The edge `A → B` is now silently gone — C has no relation to B at all, even
though the *knowledge* that fix related to that issue is exactly what a
"long-term memory that learns from past mistakes and connects them" is
supposed to preserve. This is a quiet data-loss path that will only be
noticed much later, if ever, when `analyze_dependencies` returns an
incomplete graph.

**Proposed fix:**
Before deleting parents, re-point any relations touching them to the new
consolidated entity, then delete:

```python
if parent_ids:
    placeholders = ",".join("?" for _ in parent_ids)
    # Re-point existing edges to the new consolidated entity instead of losing them
    conn.execute(f"""
        UPDATE relations SET source_id = ?
        WHERE source_id IN ({placeholders}) AND source_id != ?
    """, [entity_id] + parent_ids + [entity_id])
    conn.execute(f"""
        UPDATE relations SET target_id = ?
        WHERE target_id IN ({placeholders}) AND target_id != ?
    """, [entity_id] + parent_ids + [entity_id])
    # Drop any now-self-referential or duplicate edges created by re-pointing
    conn.execute("DELETE FROM relations WHERE source_id = target_id")
    conn.execute(f"DELETE FROM entities WHERE id IN ({placeholders})", parent_ids)
```
Alternatively (simpler, less clever): archive parents (`status = 'archived'`)
instead of hard-deleting them, and let `relations` continue pointing at the
archived rows. This preserves full history at the cost of the "physically
delete raw nodes" behavior the README currently advertises — worth an explicit
product decision rather than defaulting to data loss.

---

## 3. No first-class `session_id` or `project_id` — cross-session/cross-project recall depends on convention, not schema

**File:** `saltmdb_server.py`, `entities` / `events` table schema (lines ~94–125);
`store_knowledge` / `search_memory` signatures

**Finding:**
The `events` table has `agent_id`, `type`, `content`, `error_code` — no
`session_id`. The `entities` table has `owner_id`, `scope`, and a free-form
`metadata` JSON blob where a caller *may* put something like
`{"project": "X"}`, but nothing requires or indexes this. `search_memory`'s
`metadata_filter` does exact `json_extract` matches against whatever keys
happen to be present.

**Why it matters:**
The stated goal is memory that persists "across sessions... even between
different tasks and projects." Right now:
- "What happened last session" can only be reconstructed by an agent
  guessing time ranges and re-reading `get_recent_events`, not by asking
  for a session directly.
- "What do I know about project X" only works if every agent, every time,
  remembers to pass the same `metadata={"project": "X"}` key consistently.
  Nothing enforces this — a typo (`"proj"` vs `"project"`) silently
  fragments memory with no error.
- This is the same class of problem as Finding 1: a feature central to your
  stated goal is currently held together by prompt convention (AGENT_GUIDE.md
  telling agents what to do) rather than the server enforcing it.

**Proposed fix:**
Add first-class columns:
```sql
ALTER TABLE events ADD COLUMN session_id TEXT;
ALTER TABLE entities ADD COLUMN project_id TEXT;
```
(Use the same `try/except sqlite3.OperationalError` migration pattern already
used elsewhere in `init_db` for `valid_from`/`valid_to`/`metadata`.)

Then:
- Add `session_id` as a parameter to `log_event`, generated by the agent
  once per session (e.g. `uuid4()` at bootstrap, reused for all `log_event`
  calls in that turn sequence) or passed in by the calling CLI tool if it
  already has a session concept.
- Add a `get_session_summary(session_id)` tool that returns all events for
  that session in order — this directly answers "what did I do last time,"
  which currently requires an agent to reconstruct it manually.
- Add `project_id` as a required-or-defaulted parameter on `store_knowledge`
  and a filter on `search_memory`, indexed via a real column rather than
  `json_extract` on free-form metadata (indexed columns are both faster and
  impossible to typo past validation if you add a `CHECK` or a lookup table).

This is more invasive than Findings 1–2 (schema change + tool signature
changes + AGENT_GUIDE.md updates), so treat it as a planned migration, not a
patch.

---

## 4. `search_memory` result count is a hardcoded `LIMIT 5`

**File:** `saltmdb_server.py`, `search_memory`, lines ~624, ~651

**Finding:** Both the FTS5 query path and the no-keyword fallback path end in
`LIMIT 5`, with no parameter to change it.

**Why it matters:** This is actually a *reasonable default* for the stated
token-efficiency goal — it's the right instinct. But it's inflexible: an
agent doing a broad sweep before a consolidation pass, or trying to get full
recall on a narrow tag, has no way to ask for more than 5 results without a
code change. Given that a major goal here is "give agents the ability to
learn from past mistakes," under-returning relevant history because of a
hardcoded cap works against that goal in exactly the cases where it matters
most (an agent trying to gather everything it knows about a recurring
problem).

**Proposed fix:** Make it a bounded parameter:
```python
def search_memory(
    query_keywords: str = None,
    tags_filter: list = None,
    owner_id: str = None,
    metadata_filter: dict = None,
    explain_mode: bool = False,
    limit: int = 5,
) -> list | dict:
    ...
    limit = max(1, min(limit, 25))  # keep an upper bound to protect token budget
    ...
    sql = f"""... LIMIT ?"""
    exec_params = [...] + [limit]
```
Keep 5 as the default (preserves current token-conscious behavior) but let
callers opt into a larger sweep when they explicitly need it. Document the
upper bound in the tool docstring so agents know it's capped, not unlimited.

---

## 5. No test coverage for real concurrent access; existing "lock" and "isolation" tests are single-threaded

**File:** `scratch/test_db.py`, `test_system_locks` (line 284),
`test_multi_agent_isolation` (line 354)

**Finding:** I searched the full test file for `thread`, `concurrent`,
`multiprocess`, `Lock()`, and `race` — zero matches. `test_system_locks`
calls `acquire_librarian_lock(self.conn)` twice **sequentially on the same
connection object**, which validates the SQL predicate logic but not actual
race behavior. `test_multi_agent_isolation` tests data isolation (agent1
can't see agent2's rows), not concurrent access — it's also sequential,
single-process.

**Why it matters:** The README explicitly advertises "concurrent shared
memory" as a core feature, and your stated use case is multiple CLI tools
(Copilot, Claude Code, Antigravity) writing to the same DB. [Unverified] —
there is currently no test that proves this works under real contention.
The underlying pattern (SQLite WAL mode, `timeout=10.0`, atomic
compare-and-set `UPDATE ... WHERE locked_at IS NULL OR ...`) is the
*correct* pattern for this, so it is plausible it works — but "correct by
code inspection" and "verified by test" are different claims, and only the
former currently holds.

**Proposed fix:** Add a concurrency test that spawns real OS processes (not
threads, since SQLite + Python threading can mask issues that only show up
across processes) hammering the same DB file:
```python
import subprocess, sys, os

def test_concurrent_lock_acquisition_single_winner(self):
    """Spawn N real processes racing acquire_librarian_lock; exactly one should win."""
    worker_script = os.path.join(os.path.dirname(__file__), "_lock_race_worker.py")
    procs = [
        subprocess.Popen([sys.executable, worker_script, TEST_DB_PATH],
                          stdout=subprocess.PIPE, text=True)
        for _ in range(10)
    ]
    results = [p.communicate()[0].strip() for p in procs]
    self.assertEqual(results.count("ACQUIRED"), 1)

def test_concurrent_store_knowledge_no_lost_writes(self):
    """Spawn N real processes each storing a distinct entity_id; all N must persist."""
    # similar pattern: spawn processes, each calls store_knowledge with a unique id,
    # then assert count(*) == N in the entities table afterward.
```
This closes the gap between what's claimed and what's tested, and is
directly relevant since your use case is literally the scenario being
undertested.

---

## 6. `trigger_librarian` spawns a subprocess on every write call, before its own cooldown check completes

**File:** `saltmdb_server.py`, `trigger_librarian`, lines ~276–321

**Finding:** `store_knowledge` and `log_event` call `trigger_librarian()`
unconditionally after every successful write. The function opens its own
short-lived DB connection to check `raw_count` and the 5-minute cooldown
*before* deciding whether to spawn — but this check-then-spawn is not atomic.
Two agents (or the same agent, writing rapidly — e.g. logging several
`issue`/`attempt`/`fix` events in a tight loop) could both pass the cooldown
check before either has written back `last_run_at`, resulting in multiple
Librarian subprocesses spawned close together.

**Why it matters:** The downstream `acquire_librarian_lock` (Finding 5's
subject) does correctly prevent more than one Librarian from actually doing
work concurrently — so this isn't a correctness bug for the data itself.
It's a resource-efficiency one: under bursty write patterns (which your use
case — an agent logging many small events per session — will produce),
this can spawn more OS processes than necessary, each opening its own SQLite
connection just to lose the lock race and exit. [Inference] based on the
code path, not measured: this is more likely to matter under load than at
low write volume, and I have not profiled it.

**Proposed fix (low-risk, addresses the redundant spawns):**
Have `trigger_librarian` acquire the lock itself (or check-and-set an
in-process/lockfile debounce) before spawning, rather than spawning
unconditionally and relying on the child process to lose the race:
```python
def trigger_librarian():
    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM entities WHERE status = 'raw'")
            if cursor.fetchone()[0] < 2:
                return
            # Attempt the same atomic acquire the Librarian itself uses,
            # so only the process that actually intends to run spawns a child.
            if not acquire_librarian_lock(conn):
                return
            release_librarian_lock(conn)  # release immediately; child re-acquires for real work
        finally:
            conn.close()
    except Exception:
        pass
    # ... existing subprocess.Popen spawn logic
```
This trades a slightly more complex trigger function for fewer wasted
subprocess spawns under concurrent/bursty writes.

---

## 7. `store_knowledge` deduplication is exact-title-match only; fuzzy duplicate checking exists but isn't enforced

**File:** `saltmdb_server.py`, `store_knowledge`, lines ~438–449 vs.
`check_duplicate_memories`, lines ~1266–1328

**Finding:** `store_knowledge`'s upsert-routing logic only matches on
`WHERE title = ? AND owner_id = ? AND scope = ?` — exact string match. A
separate tool, `check_duplicate_memories`, does real fuzzy matching
(`difflib.SequenceMatcher` on title + content, 60%/70% thresholds) — but
nothing in the server forces an agent to call it before `store_knowledge`.
The only enforcement is a line in `AGENT_GUIDE.md` telling agents to "run
before storing."

**Why it matters:** This is a direct token-cost and memory-quality risk for
your stated goal. If an agent forgets (or a different CLI tool's agent,
using a different system prompt, never learned) to call
`check_duplicate_memories` first, near-duplicate memories accumulate under
slightly different titles — the exact clutter problem you were trying to
escape by moving off flat markdown files. The Librarian's
`consolidate_cluttered_tags`/`consolidate_memories` will eventually catch
this via the raw-count threshold, but only after the duplication has already
cost storage and search-noise, and only if the agent honors the resulting
`consolidation_request` event (also convention-enforced, not server-enforced).

**Proposed fix:** Make `store_knowledge` call the same similarity check
internally before falling through to exact-title matching, and either warn
in the return value or (optionally, via a parameter) refuse to store above a
high-confidence threshold:
```python
def store_knowledge(..., skip_duplicate_check: bool = False) -> str:
    ...
    if not entity_id and not skip_duplicate_check:
        dup_check = check_duplicate_memories(title=title, content=redacted_content,
                                              owner_id=owner_id, tags=tags)
        if dup_check.get("duplicate_found"):
            top = dup_check["potential_duplicates"][0]
            return (f"Warning: potential duplicate of existing memory "
                    f"'{top['title']}' (ID: {top['id']}, similarity {top['similarity_score']}). "
                    f"Call store_knowledge with entity_id='{top['id']}' to update it instead, "
                    f"or skip_duplicate_check=True to force a new entry.")
    ...
```
This keeps agent autonomy (they can still force a new entry) but makes the
safety net automatic rather than prompt-dependent — which matters
specifically because your setup involves multiple *different* CLI tools
(Copilot, Antigravity, Claude Code) that may not share identical system
prompts.

---

## Summary Table

| # | Finding | Impact area | Risk if unfixed | Effort |
|---|---|---|---|---|
| 1 | `analyze_dependencies` cycle check uses title text, not entity ID | Cross-memory connections | Silently wrong/truncated dependency graphs | Low |
| 2 | `commit_consolidation` hard-deletes parents without re-pointing relations | Cross-memory connections | Silent loss of relationship edges | Low–Medium |
| 3 | No first-class `session_id` / `project_id` | Cross-session & cross-project recall | Feature only works by prompt convention | Medium–High |
| 4 | `search_memory` hardcoded `LIMIT 5` | Token efficiency / recall completeness | Can't do deliberate broad recall | Low |
| 5 | No real concurrency tests | Multi-agent reliability | Unverified claim vs. actual use case | Medium |
| 6 | `trigger_librarian` spawns before atomic lock check | Resource efficiency | Redundant subprocess spawns under load | Low |
| 7 | Exact-title dedup only; fuzzy check not enforced | Memory quality / token efficiency | Duplicate clutter across differently-prompted agents | Low–Medium |

**Suggested order of work**, given the stated priorities (connections between
memories, then cross-session/project recall, then hardening):
1 → 2 → 4 → 7 → 3 → 6 → 5.

Findings 1, 2, 4, and 6 are self-contained and low-risk. Finding 3 is a
schema migration and should be scoped as its own task with its own tests.
Finding 5 should be added incrementally as the other fixes land, since new
concurrent-write paths (e.g. Finding 6's changes) should themselves be
covered by it.
