"""Pure, self-independent constants and helpers shared across viewer route mixins.

No method here touches ``self`` — these are plain functions/constants extracted
verbatim from the former monolithic ``viewer/routes.py``, unchanged.
"""

from datetime import UTC, datetime, timedelta

MAX_ENTITY_LIMIT = 100
MAX_EVENT_LIMIT = 100
MAX_RELATION_LIMIT = 50
MAX_SESSION_LIMIT = 100
MAX_TRACE_LIMIT = 100

STATIC_ASSETS = {
    "/static/viewer.css": "viewer.css",
    "/static/viewer.js": "viewer.js",
    "/static/vendor/marked-18.0.7.umd.js": "vendor/marked-18.0.7.umd.js",
    "/static/vendor/dompurify-3.4.16.min.js": "vendor/dompurify-3.4.16.min.js",
}

_ENTITY_SORTS = {
    "updated_desc": ("updated_at", "DESC"),
    "updated_asc": ("updated_at", "ASC"),
    "created_desc": ("created_at", "DESC"),
    "created_asc": ("created_at", "ASC"),
}


def _bounded_query_int(query, name, default, minimum, maximum):
    """Read one positive bounded integer query parameter or raise ValueError."""
    raw = query.get(name, [None])[0]
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _utc_day_bound(raw: str, *, end_exclusive: bool) -> str:
    """Return a UTC ISO timestamp for a calendar-date query bound."""
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("date bounds must use YYYY-MM-DD") from exc
    if end_exclusive:
        parsed += timedelta(days=1)
    return parsed.isoformat()


def active_relation_filter(as_of: str | None = None, alias: str = "r") -> tuple[str, list[str]]:
    """SQL condition + params keeping only relations valid at ``as_of`` (default: now).

    One predicate for every current-state Viewer relation surface, so the graph, lists,
    per-entity pages, detail counts and neighborhood can never disagree about an edge.
    """
    when = as_of or datetime.now(UTC).isoformat()
    sql = " AND ".join(
        [
            f"({alias}.valid_from IS NULL OR datetime({alias}.valid_from) <= datetime(?))",
            f"({alias}.valid_to IS NULL OR datetime({alias}.valid_to) > datetime(?))",
            f"({alias}.valid_at IS NULL OR datetime({alias}.valid_at) <= datetime(?))",
            f"({alias}.invalid_at IS NULL OR datetime({alias}.invalid_at) > datetime(?))",
        ]
    )
    return sql, [when] * 4
