"""Tag normalization and canonical-tag resolution for memory_service.

Pure code-motion extraction (see refactor plan). Zero cross-module dependencies
within this package.
"""

import re
import uuid

from saltmdb.config import get_db_path
from saltmdb.db.connection import get_connection, close_connection

from ._shared import logger

_TAG_NAME_RE = re.compile(r"^#[a-z0-9][a-z0-9-]*$")


def normalize_tag_name(tag_name: str) -> str:
    """Ensures a bare or malformed tag string is '#'-prefixed. Pure syntactic helper,
    reused by write paths and read-path tag filters alike (replaces duplicated
    auto-prefix one-liners across the codebase)."""
    name = (tag_name or "").strip()
    if not name:
        return name
    if not name.startswith("#"):
        name = "#" + name
    return name


_SEPARATOR_RUN_RE = re.compile(r"[^a-z0-9]+")


def sanitize_tag_body(raw_body_lower: str) -> tuple[str | None, str | None]:
    """Returns (sanitized_body, rejection_message). Exactly one is non-None.

    Any single character outside [a-z0-9] is collapsed to '-'; any run of 2+ such
    characters (including an existing '-' adjacent to another separator) is rejected
    rather than guessed at. Leading/trailing '-' are trimmed after collapsing.
    """
    for match in _SEPARATOR_RUN_RE.finditer(raw_body_lower):
        if len(match.group(0)) >= 2:
            return None, (
                f"tag '#{raw_body_lower}' contains adjacent separator characters "
                f"('{match.group(0)}') -- this usually means a typo; check the intended tag name."
            )
    sanitized = _SEPARATOR_RUN_RE.sub("-", raw_body_lower).strip("-")
    return sanitized, None


def validate_tag_names(tags: list[str] | None) -> str | None:
    """Pre-transaction check: returns an error message if any tag has 2+ adjacent
    separator characters, else None. Call before write_transaction_retrying begins."""
    for tag_name in tags or []:
        if not isinstance(tag_name, str) or not tag_name.strip():
            continue
        name = normalize_tag_name(tag_name)
        raw_body_lower = name[1:].lower() if name.startswith("#") else name.lower()
        _, rejection = sanitize_tag_body(raw_body_lower)
        if rejection:
            return rejection
    return None


def list_entity_tags(conn, entity_id: str, *, include_core: bool = False) -> list[str]:
    """Returns the canonical tag names attached to `entity_id`, sorted.

    Excludes the internal '#core' bookkeeping tag by default -- is_core is already surfaced
    as its own boolean field on every read path, so callers who just want an entity's
    user-facing tags shouldn't see it duplicated here. Pass include_core=True to see it
    anyway (e.g. diagnostics). This is the single read-side counterpart to the write-side
    tag lookups duplicated inline in write.py's _effective_tag_report/_legacy_update_guard --
    those stay as-is (already tested, entangled with near-miss/diff logic specific to each),
    this is for callers that just want "what tags does this entity have right now".
    """
    rows = conn.execute(
        """
        SELECT t.name
        FROM entity_tags et JOIN tags t ON t.id = et.tag_id
        WHERE et.entity_id = ?
        ORDER BY t.name
        """,
        (entity_id,),
    ).fetchall()
    return [row[0] for row in rows if include_core or row[0] != "#core"]


def resolve_or_create_tag(conn, tag_name: str, agent_id: str = None) -> str | None:
    """Single source of truth for tag write-time resolution.

    Must be called with `conn` already inside an open write transaction (does not open
    its own). Returns the resolved (canonical, if aliased) tag id, or None if the name is
    empty/unsalvageable after sanitization.

    Resolution order:
      1. Shape-sanitize the name (lowercase, strip characters not in [a-z0-9-] after the
         '#' prefix). If sanitization actually changed the string, fire a soft
         log_event(type='issue') noting the before/after -- this never blocks resolution,
         it's visibility only.
      2. Exact `name` match.
      3. The existing normalized_name / computed-normalization fallback (mirrors
         store_memory's fuzzy lookup exactly).
      4. A simple plural/suffix fallback: only when the normalized input is longer than 3
         chars, full-scan `tags` and compare `norm_input.rstrip('s')` against each row's
         normalized form (also only when that row's normalized form is longer than 3
         chars) -- return on first match.
      5. Otherwise, create a new tag row and return its new id.

    At every step, if a row is found, return `canonical_id if canonical_id else id` --
    respecting existing alias merges (the exact behavior gap commit_consolidation is
    currently missing).
    """
    name = normalize_tag_name(tag_name)
    if not name or name == "#":
        return None

    # Step 1: shape-sanitize -- collapse a single disallowed character to '-'; a rejection
    # here should be unreachable (validate_tag_names already rejects pre-transaction), but
    # never let an unexpected rejection crash mid-transaction -- fall back to legacy delete
    # behavior and log a warning instead.
    raw_body = name[1:]
    sanitized_body, rejection = sanitize_tag_body(raw_body.lower())
    if rejection:
        sanitized_body = re.sub(r"[^a-z0-9-]", "", raw_body.lower())
        try:
            from saltmdb.domain.services.event_service import log_event

            log_event(
                agent_id=agent_id or "system",
                type="issue",
                content=f"Unexpected tag rejection reached resolve_or_create_tag (should have been caught pre-transaction): {rejection}",
                db_connection=conn,
                _in_transaction=True,
            )
        except Exception:
            pass
    sanitized_name = ("#" + sanitized_body) if sanitized_body else name

    if sanitized_name != name:
        try:
            from saltmdb.domain.services.event_service import log_event

            log_event(
                agent_id=agent_id or "system",
                type="issue",
                content=f"Tag name sanitized during resolve_or_create_tag: '{name}' -> '{sanitized_name}'",
                db_connection=conn,
                _in_transaction=True,
            )
        except Exception as ex:
            logger.warning("Failed to log tag sanitization event: %s", ex)
        name = sanitized_name

    if not name or name == "#":
        return None

    # Step 2: exact match
    row = conn.execute("SELECT id, canonical_id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    # Step 3: normalized_name / computed-normalization fallback (mirrors store_memory)
    norm_input = name.lower().lstrip("#")
    norm_input = re.sub(r"[-_\s]+", "", norm_input)

    row = conn.execute(
        "SELECT id, canonical_id FROM tags WHERE normalized_name = ? OR lower(replace(replace(replace(name,'#',''),'-',''),'_','')) = ?",
        (norm_input, norm_input),
    ).fetchone()
    if row:
        return row[1] if row[1] else row[0]

    # Step 4: plural/suffix fallback -- full scan (small table, same cost model already
    # accepted by merge_tags_heuristics()), only for norm_input longer than 3 chars.
    if len(norm_input) > 3:
        stripped_input = norm_input.rstrip("s")
        all_rows = conn.execute(
            "SELECT id, name, normalized_name, canonical_id FROM tags"
        ).fetchall()
        for tid, tname, tnorm, tcanon in all_rows:
            existing_norm = tnorm if tnorm else re.sub(r"[-_\s]+", "", tname.lower().lstrip("#"))
            if len(existing_norm) > 3 and stripped_input == existing_norm.rstrip("s"):
                return tcanon if tcanon else tid

    # Step 5: create a new tag row
    tag_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
        (tag_id, name, norm_input),
    )
    return tag_id


# NOTE: this 'domain' param is a tag-name substring filter, unrelated to the entities table.
def search_tags(
    domain: str = None, limit: int = 50, db_connection=None, db_path: str = None
) -> list:
    """Queries canonical tags (agent API redesign plan §5.12, Phase 6 item 27: renamed from
    get_canonical_tags -- advisory discovery, not a prerequisite)."""
    should_close = False
    conn = db_connection
    if not conn:
        db_path = db_path or get_db_path()
        conn = get_connection(db_path)
        should_close = True

    try:
        if domain:
            cursor = conn.execute(
                """
                SELECT id, name FROM tags
                WHERE canonical_id IS NULL AND name LIKE ?
                LIMIT ?
            """,
                (f"%{domain}%", limit),
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, name FROM tags
                WHERE canonical_id IS NULL
                LIMIT ?
            """,
                (limit,),
            )
        rows = cursor.fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    except Exception as e:
        logger.error("Error fetching canonical tags: %s", e)
        return [{"error": str(e)}]
    finally:
        if should_close:
            close_connection(conn)
