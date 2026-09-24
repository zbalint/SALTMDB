# SPEC-MEMORY-CONTENT-LENGTH-CAP

## 1. Why

A live incident chain this session (SALTMDB memories `575286de`, `badbda38`/`e9e0a617`,
`d50aabb3`/`3d82d315`, `b93ad8a5`, `794e2743`) traced a recurring WSL2 host freeze back to
`saltmdb.domain.services.embedding_service.embed_texts()` being called with a large, unchunked
single batch — the whole chunk list for one memory's content, passed in one call, no size guard.
A deliberately reproduced test (`b93ad8a5`) confirmed the failure trigger sits around
`n_chunks≈400` (roughly 400,000 characters of content, at this project's
`CHUNK_SIZE_CHARS=1200`/`CHUNK_OVERLAP_CHARS=200` → ~1000-char stride per chunk), under
worst-case stacked memory pressure on a 4GB-capped WSL2 host.

zbalint's decision (this session, in conversation, not yet in code): cap memory content at write
time to 40,000 characters (~40 chunks) — roughly 10x below the measured worst-case trigger, and
well above the largest content this project's chunking benchmarks have exercised. Two follow-up
questions were resolved explicitly by zbalint in the same conversation:

1. **Existing over-cap memories** (30 confirmed in production, largest 128,605 chars — see
   `21282b46`): accepted as-is. This spec protects new writes only; no backfill, no migration,
   no retroactive check against existing rows.
2. **The consolidation side-door**: `consolidate_memories`'s `content` parameter is a plain
   caller-supplied string, not auto-assembled from parents — nothing currently stops a caller
   from merging several under-cap memories into one over-cap result. This spec closes that path
   with the same 40,000-char cap, not a separate/looser one.

This is a validation-only change: no chunking/embedding logic changes, no new config surface
beyond one constant, no behavior change for any memory already at or under 40,000 characters.

## 2. `src/saltmdb/config.py` — new constant

Insert immediately after the existing `CORE_MAX_CONTENT_CHARS` line (currently line 650, in the
core-governance constants block starting at line 645):

```python
CORE_MAX_CONTENT_CHARS = 2500  # max full_content per core, Unicode code points (len(text))
MAX_MEMORY_CONTENT_CHARS = 40_000  # max full_content for any memory write (store/revise/
# supersede/consolidate); a strict superset of CORE_MAX_CONTENT_CHARS's tighter core-only cap --
# see SPEC-MEMORY-CONTENT-LENGTH-CAP.md for the measured trigger this margins against.
CORE_MAX_RENDERED_CHARS = 15000  # max exact rendered bootstrap digest, Unicode code points
```

(Only the middle line is new; `CORE_MAX_CONTENT_CHARS` and `CORE_MAX_RENDERED_CHARS` are existing
lines shown for anchoring — do not modify them.)

## 3. `src/saltmdb/domain/services/memory_service/write.py` — `store_memory`

Import: add `MAX_MEMORY_CONTENT_CHARS` to the existing import at line 11:

```python
from saltmdb.config import get_db_path, MAX_MEMORY_CONTENT_CHARS
```

At line 621-622, the current code is:

```python
    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."

    if scope not in ("private", "shared"):
```

Change to (insert a new check immediately after the existing empty-content check, same
plain-string-return convention this function already uses for every other early validation in
this block — do not switch to a dict/envelope return here, it would break every existing caller
of this function that pattern-matches on a string):

```python
    if not content or not content.strip():
        return "Error: content is mandatory and cannot be empty."

    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        return (
            f"Error: content exceeds {MAX_MEMORY_CONTENT_CHARS} characters "
            f"(got {len(content)})."
        )

    if scope not in ("private", "shared"):
```

This applies to both fresh inserts and `entity_id`-targeted updates (`store_memory` handles both
in this one function) — the check runs before either path branches, so both are covered.

## 4. `src/saltmdb/domain/services/memory_service/lifecycle.py` — `revise_memory`/`supersede_memory`

Both route through the shared `_validate_replacement_inputs` (line 129), which both
`revise_memory` and `supersede_memory` call via `_replacement_operation` — one change covers both
public functions.

Import: add `MAX_MEMORY_CONTENT_CHARS` to the existing import at line 13:

```python
from saltmdb.config import get_db_path, MAX_MEMORY_CONTENT_CHARS
```

At lines 145-148, the current code is:

```python
    if not isinstance(content, str) or not content.strip():
        return _replacement_error(
            "MISSING_CONTENT", "content is mandatory and cannot be empty.", "content"
        )
    if not isinstance(reason, str) or not reason.strip():
```

Change to (insert a new check immediately after, using this function's existing
`_replacement_error(code, message, field)` envelope convention):

```python
    if not isinstance(content, str) or not content.strip():
        return _replacement_error(
            "MISSING_CONTENT", "content is mandatory and cannot be empty.", "content"
        )
    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        return _replacement_error(
            "CONTENT_TOO_LONG",
            f"content exceeds {MAX_MEMORY_CONTENT_CHARS} characters (got {len(content)}).",
            "content",
        )
    if not isinstance(reason, str) or not reason.strip():
```

## 5. `src/saltmdb/domain/services/relation_service.py` — `consolidate_memories`

This is the fix for the consolidation side-door named in §1. `commit_consolidation` (the
temporary compatibility alias, line 1878) calls `consolidate_memories` directly, and
`bulk_commit_consolidation` calls the same per-item commit path internally (per its own
docstring, lines 1926-1933) — so this one insertion covers `consolidate_memories`,
`commit_consolidation`, and `bulk_commit_consolidation` together. Do not add a second check
inside `bulk_commit_consolidation` itself — that would be redundant with this one and risks the
two drifting out of sync later.

Import: add `MAX_MEMORY_CONTENT_CHARS` to the existing multi-line import at lines 8-15:

```python
from saltmdb.config import (
    get_db_path,
    COHESION_MIN_PAIRWISE_THRESHOLD,
    COHESION_OVERRIDE_MIN_LENGTH,
    RELATION_GATE_MIN_SIMILARITY_THRESHOLD,
    RELATION_GATE_STRONG_PREDICATES,
    RELATION_GATE_CONTRADICTORY_PREDICATE_PAIRS,
    MAX_MEMORY_CONTENT_CHARS,
)
```

At lines 1417-1419, the current code is:

```python
    if not title or not content:
        return _consolidation_rejected(
            "MISSING_REQUIRED_FIELDS", "title and content are mandatory."
        )
```

Change to (insert a new check immediately after, using this function's existing
`_consolidation_rejected(code, message)` envelope convention):

```python
    if not title or not content:
        return _consolidation_rejected(
            "MISSING_REQUIRED_FIELDS", "title and content are mandatory."
        )
    if len(content) > MAX_MEMORY_CONTENT_CHARS:
        return _consolidation_rejected(
            "CONTENT_TOO_LONG",
            f"content exceeds {MAX_MEMORY_CONTENT_CHARS} characters (got {len(content)}).",
        )
```

## 6. Tests

Add new test coverage for all three paths — do not rely on the existing test suite's incidental
coverage, none of it currently exercises this boundary:

- **`tests/test_mcp_tools.py`**: a test alongside the existing `test_store_memory_*` methods
  (e.g. near line 157) asserting `tools.store_memory(content="x" * (MAX_MEMORY_CONTENT_CHARS + 1),
  ...)` returns the new error string (content exceeds MAX_MEMORY_CONTENT_CHARS check), and a
  second test asserting content at exactly `MAX_MEMORY_CONTENT_CHARS` succeeds (boundary
  inclusive, not exclusive — mirrors `CORE_MAX_CONTENT_CHARS`'s own boundary convention in
  `core_governance_service.py`, which likewise rejects on `>`, not `>=`).
- **`tests/test_phase4_mcp_surface.py`**: a test exercising `revise_memory` and a test exercising
  `supersede_memory`, each with `content` one character over `MAX_MEMORY_CONTENT_CHARS`, asserting
  `status == "rejected"` and `errors[0]["code"] == "CONTENT_TOO_LONG"`.
- **`tests/test_consolidate_memories_phase4.py`**: a test exercising `consolidate_memories`
  directly with `content` one character over `MAX_MEMORY_CONTENT_CHARS`, asserting
  `status == "rejected"` and `errors[0]["code"] == "CONTENT_TOO_LONG"`.

Import `MAX_MEMORY_CONTENT_CHARS` from `saltmdb.config` in each new test module that needs it,
the same way `tests/test_large_content_file_transport.py` already imports
`CONTENT_FILE_DUMP_THRESHOLD_CHARS` — do not hardcode the literal `40000` in test assertions.

Verified compatible with the existing suite: `tests/test_large_content_file_transport.py`'s
`test_get_memory_dumps_oversized_content_to_file` calls real `store_memory` with a generated
`large_body` — computed and confirmed this session at exactly 20,113 characters, comfortably
under the new 40,000 cap. No existing test in `tests/` constructs a real (non-mocked)
`store_memory`/`revise_memory`/`supersede_memory`/`consolidate_memories` call with content over
40,000 characters — confirmed by scanning `tests/*.py` for large numeric literals/string
multiplication combined with these call names. No existing test needs to change.

## 7. Out of scope

- **Any change to `embed_texts()`, chunking (`CHUNK_SIZE_CHARS`/`CHUNK_OVERLAP_CHARS`), or the
  embedding service.** This spec is a write-time content-length guard, not a fix to the
  underlying unchunked-batch gap — that gap still exists for any future non-write code path that
  calls `embed_texts()` directly (e.g. a backfill/reconcile job), and is explicitly not addressed
  here.
- **No backfill, migration, or any check against the 30 existing memories already over 40,000
  characters.** Per zbalint's explicit decision (§1): accepted as-is, untouched.
- **No change to `CORE_MAX_CONTENT_CHARS` (2500) or any other existing core-governance
  constant/check** in `core_governance_service.py`. The new `MAX_MEMORY_CONTENT_CHARS` check is
  independent of and additional to the existing core-specific check, not a replacement — a
  core memory must still separately satisfy the tighter 2500-char cap.
- **No change to `content_file_path` handling** (`mcp/tools.py::_resolve_content`). The new
  length check runs on the resolved `content` string in the domain-service layer, after
  `_resolve_content` has already turned either an inline `content` or a `content_file_path` read
  into a plain string — both forms are covered identically without touching that function.
- **No change to `CONTENT_FILE_DUMP_THRESHOLD_CHARS` (20,000) or `large_content_descriptor()`**
  (`utils/text.py`) — that is the unrelated read-side (`get_memory`) mechanism named in §1 and
  stays exactly as-is.
- **No new MCP tool parameter or user-facing config knob.** `MAX_MEMORY_CONTENT_CHARS` is a fixed
  internal constant, not configurable via environment variable or tool argument, matching
  `CORE_MAX_CONTENT_CHARS`'s own precedent.

## 8. Acceptance

Run from the repo root, using the worktree's own `.venv`:

```bash
./verify
```

Must exit 0 (ruff, mypy, bandit, pip-audit, deptry, full pytest suite all clean — matches this
project's standard bar, see `AGENT_GUIDE.md`/`CONTRIBUTING.md`).

Additionally, confirm the new constant is defined exactly once and imported everywhere it's used
(no duplicate re-declaration in any of the three consuming files):

```bash
rg -n "MAX_MEMORY_CONTENT_CHARS" src/
```

Expected: exactly one definition (`src/saltmdb/config.py`), and one import + one usage line each
in `src/saltmdb/domain/services/memory_service/write.py`,
`src/saltmdb/domain/services/memory_service/lifecycle.py`, and
`src/saltmdb/domain/services/relation_service.py` (import line + the new `if len(content) >
MAX_MEMORY_CONTENT_CHARS:` line in each) — 7 total matches across `src/`, none elsewhere.

## 0. Status

**LOCKED.**

**Scope — files that may be edited:**
- `src/saltmdb/config.py` (add one constant + its inline comment; do not modify
  `CORE_MAX_CONTENT_CHARS`/`CORE_MAX_RENDERED_CHARS` themselves)
- `src/saltmdb/domain/services/memory_service/write.py` (import line 11 + new check after
  line 622)
- `src/saltmdb/domain/services/memory_service/lifecycle.py` (import line 13 + new check after
  line 148)
- `src/saltmdb/domain/services/relation_service.py` (import block lines 8-15 + new check after
  line 1419)
- `tests/test_mcp_tools.py` (new tests only — do not touch existing tests in this file)
- `tests/test_phase4_mcp_surface.py` (new tests only — do not touch existing tests in this file)
- `tests/test_consolidate_memories_phase4.py` (new tests only — do not touch existing tests in
  this file)

**Does not touch:** `src/saltmdb/domain/services/embedding_service.py`,
`src/saltmdb/domain/services/core_governance_service.py`, `src/saltmdb/mcp/tools.py`,
`src/saltmdb/utils/text.py`, `tests/test_large_content_file_transport.py`, any file under
`src/saltmdb/db/`, and every other file in the repo not explicitly listed above.
