"""Pure, self-independent constants and helpers shared across viewer route mixins.

No method here touches ``self`` — these are plain functions/constants extracted
verbatim from the former monolithic ``viewer/routes.py``, unchanged.
"""

from datetime import UTC, datetime, timedelta

MAX_ENTITY_LIMIT = 100
MAX_EVENT_LIMIT = 100
MAX_RELATION_LIMIT = 50

STATIC_ASSETS = {
    "/static/viewer.css": "viewer.css",
    "/static/viewer.js": "viewer.js",
    "/static/vendor/marked-18.0.7.umd.js": "vendor/marked-18.0.7.umd.js",
    "/static/vendor/dompurify-3.4.10.min.js": "vendor/dompurify-3.4.10.min.js",
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
