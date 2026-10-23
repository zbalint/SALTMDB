"""Allow-list entries for legitimate public-surface mentions in scanned docs."""

from __future__ import annotations

from typing import Final

ALLOWED: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "docs/architecture.md",
        "query_keywords",
        "service function `memory_service.search_memory` keeps",
        "R9 internal service spelling is not a public MCP field",
    ),
    (
        "docs/architecture.md",
        "inspect_memory",
        "is removed",
        "historical removal note documents the migration",
    ),
)
