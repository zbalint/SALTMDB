import re
import logging
import sqlite3

logger = logging.getLogger(__name__)

UUID_REGEX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def resolve_entity_id(conn, input_val: str) -> str | None:
    """Helper to flexibly resolve an entity ID from a raw UUID/ID, a status string containing a UUID, or an entity title."""
    if not input_val or not isinstance(input_val, str):
        return input_val
    input_val = input_val.strip()

    # 0. Check if input_val is already an exact entity ID in the database
    try:
        cursor = conn.execute("SELECT id FROM entities WHERE id = ?", (input_val,))
        if cursor.fetchone():
            return input_val
    except sqlite3.Error as exc:
        logger.debug(
            "Exact entity-id lookup unavailable; continuing with textual resolution: %s", exc
        )

    # 1. Exact UUID pattern
    if UUID_REGEX.fullmatch(input_val):
        return input_val

    # 2. Status string containing UUID (e.g. 'Knowledge stored successfully with ID: <uuid>')
    match = UUID_REGEX.search(input_val)
    if match:
        return match.group(0)

    # 3. Entity title resolution
    try:
        cursor = conn.execute(
            "SELECT id FROM entities WHERE title = ? AND status != 'archived' ORDER BY updated_at DESC LIMIT 1",
            (input_val,),
        )
        row = cursor.fetchone()
        if row:
            return row[0]
    except sqlite3.Error as exc:
        logger.debug("Entity-title lookup unavailable; returning input unchanged: %s", exc)

    return input_val


_HEX_PREFIX_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")


def resolve_id_prefix(conn, prefix: str) -> tuple[str | None, list[dict], bool]:
    """Resolve a short hex ID prefix (8..31 significant hex digits, dashes allowed) against
    entities.id. Matches active AND archived entities (mirrors fetch_memory_chunk's own lack
    of status filtering on exact-ID lookups -- no new visibility narrowing/widening).

    Returns (resolved_id, candidates, truncated):
      - exactly one match -> (full_id, [], False)
      - zero matches / input isn't a valid short-prefix shape -> (None, [], False)
      - 2+ matches -> (None, [{"id","title","status"} ...] capped at 20, truncated)

    Deliberately a no-op for inputs with >=32 significant hex digits (full-length UUIDs) --
    those are already handled exactly by resolve_entity_id/the caller's initial lookup, so
    scanning for them here would just re-scan the whole table for a row already ruled absent.

    This is an unindexed full-table scan (SQLite's default case-insensitive LIKE semantics
    prevent the prefix-range index optimization on entities.id), accepted given SALTMDB's
    memory-graph scale and that the full-length-UUID exclusion above keeps it off the most
    common "not found" path.
    """
    if not prefix or not isinstance(prefix, str):
        return None, [], False
    p = prefix.strip()
    hex_only = p.replace("-", "")
    if not (8 <= len(hex_only) < 32) or not _HEX_PREFIX_RE.match(p):
        return None, [], False
    p_lower = p.lower()
    try:
        cursor = conn.execute(
            "SELECT id, title, status, updated_at FROM entities WHERE id LIKE ? LIMIT 21",
            (f"{p_lower}%",),
        )
        rows = cursor.fetchall()
    except sqlite3.Error as exc:
        logger.debug("Prefix-id lookup unavailable: %s", exc)
        return None, [], False
    if len(rows) == 1:
        return rows[0][0], [], False
    if len(rows) > 1:
        truncated = len(rows) > 20
        top = sorted(rows, key=lambda r: r[3] or "", reverse=True)[:20]
        return None, [{"id": r[0], "title": r[1], "status": r[2]} for r in top], truncated
    return None, [], False


def resolve_entity_ref(conn, raw_id: str) -> tuple[str | None, list[dict], bool]:
    """Composes resolve_entity_id + resolve_id_prefix into the one shared existence-checked
    resolution contract (agent API redesign §4.4): full UUID / title match, falling back to
    short hex-prefix matching when the first pass doesn't land on a real row. Existing
    call sites (fetch_memory_chunk, touch_memory_access) already hand-roll this same two-step
    fallback inline; this is the reusable version for new callers (manage_relation,
    consolidation parent resolution) so a third copy of the pattern doesn't appear.

    Returns (resolved_id, candidates, truncated), same shape as resolve_id_prefix:
      - resolves to a real, existing row -> (id, [], False)
      - ambiguous short prefix (2+ matches) -> (None, candidates, truncated)
      - no match at all -> (None, [], False)

    Unlike resolve_entity_id alone, the returned id (when non-None) is always verified to
    exist in `entities` -- resolve_entity_id's own UUID-shaped/arbitrary-string echo-back
    branches are not trusted here without an existence check.
    """
    if not raw_id or not isinstance(raw_id, str):
        return None, [], False
    resolved = resolve_entity_id(conn, raw_id)
    if resolved:
        try:
            if conn.execute("SELECT 1 FROM entities WHERE id = ?", (resolved,)).fetchone():
                return resolved, [], False
        except sqlite3.Error as exc:
            logger.debug("Existence check unavailable during entity-ref resolution: %s", exc)
    prefix_id, candidates, truncated = resolve_id_prefix(conn, raw_id)
    if candidates:
        return None, candidates, truncated
    if prefix_id:
        return prefix_id, [], False
    return None, [], False


def extract_title_and_snippet(markdown_text: str):
    """Heuristic helper to extract a clean title and snippet from markdown text."""
    if not markdown_text:
        return "Untitled", ""
    lines = [line.strip() for line in markdown_text.splitlines() if line.strip()]
    title = "Untitled"
    for line in lines:
        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            if candidate:
                title = candidate
                break

    if title == "Untitled" and lines:
        title = lines[0]
        if len(title) > 60:
            title = title[:57] + "..."

    text_lines = []
    for line in lines:
        if not line.startswith("#"):
            text_lines.append(line)
            if len(text_lines) >= 3:
                break

    snippet = " ".join(text_lines)
    if len(snippet) > 150:
        snippet = snippet[:147] + "..."
    return title, snippet


def sanitize_fts_query(query: str) -> str:
    """Sanitizes raw query string for FTS5, escaping special characters and balancing quotes."""
    if not query:
        return ""
    query = query.replace('"', " ")
    cleaned = re.sub(r"[\-+<>:/*\\?^$|#@`~!%&(){}[\]]", " ", query)
    return " ".join(cleaned.split())


def normalize_search_query(query: str) -> str:
    """Normalizes input search queries by lowercasing and stripping punctuation."""
    if not query:
        return ""
    q = query.lower()
    q = re.sub(r"[^\w\s]", " ", q)
    return " ".join(q.split())


import hashlib  # noqa: E402


def compute_content_hash(text: str) -> str:
    """Computes a SHA-256 hash of normalized text (lowercase and stripped of leading/trailing whitespace)."""
    if not text:
        return ""
    normalized = text.strip().lower()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
