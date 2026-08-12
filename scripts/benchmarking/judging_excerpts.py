"""Deterministic query-centered excerpts for external judging packets.

The judging packet builder owns task/candidate shuffling and private mappings.  This module owns
only source rendering: it redacts the source, selects a bounded context around the first highest
scoring query-term match, and emits content hashes.  The public object intentionally contains no
rank, search configuration, model identity, or expected target.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from saltmdb.utils.redaction import redact_secrets

EXCERPT_ALGORITHM_VERSION = "query_excerpt_v1"
EXCERPT_SCHEMA_VERSION = 1
DEFAULT_MAX_CHARS = 800
_WORD_RE = re.compile(r"[\w][\w'-]*", flags=re.UNICODE)
_SEGMENT_RE = re.compile(r"\n+|(?<=[.!?])\s+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "about",
    "does",
    "for",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "which",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _normalise(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "\n".join(" ".join(line.split()) for line in normalized.splitlines()).strip()


def _canonical_source(title: str, body: str) -> str:
    # NUL separates the fields so ``title='a\nfoo', body='bar'`` cannot collide with another
    # title/body pair merely because they were concatenated without a delimiter.
    return f"{title}\0{body}"


def _query_terms(query: str) -> tuple[str, ...]:
    words = {match.group(0).lower() for match in _WORD_RE.finditer(_normalise(query))}
    return tuple(sorted(word for word in words if len(word) > 1 and word not in _STOPWORDS))


def _segments(body: str) -> list[str]:
    return [segment.strip() for segment in _SEGMENT_RE.split(body) if segment.strip()]


def _segment_score(segment: str, terms: tuple[str, ...]) -> int:
    words = {match.group(0).lower() for match in _WORD_RE.finditer(segment)}
    return sum(term in words for term in terms)


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1].rstrip() + "…"


def _bounded_context(body: str, query: str, budget: int) -> str:
    if budget <= 0 or not body:
        return ""
    segments = _segments(body)
    if not segments:
        return ""
    terms = _query_terms(query)
    best_index = 0
    if terms:
        best_index = max(
            range(len(segments)), key=lambda index: (_segment_score(segments[index], terms), -index)
        )
    selected = [best_index]
    radius = 1
    while True:
        candidates = []
        left = best_index - radius
        right = best_index + radius
        if left >= 0:
            candidates.append(left)
        if right < len(segments):
            candidates.append(right)
        if not candidates:
            break
        proposed = sorted(selected + candidates)
        rendered = "\n".join(segments[index] for index in proposed)
        if len(rendered) > budget:
            break
        selected = proposed
        radius += 1
    return _truncate("\n".join(segments[index] for index in sorted(selected)), budget)


@dataclass(frozen=True)
class JudgingExcerpt:
    """Public, immutable source excerpt with source and rendering fingerprints."""

    title: str
    excerpt: str
    source_hash: str
    excerpt_hash: str
    algorithm_version: str = EXCERPT_ALGORITHM_VERSION
    redaction_applied: bool = False
    schema_version: int = EXCERPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, field in (
            (self.title, "title"),
            (self.excerpt, "excerpt"),
            (self.source_hash, "source_hash"),
            (self.excerpt_hash, "excerpt_hash"),
            (self.algorithm_version, "algorithm_version"),
        ):
            _require_text(value, field)
        if self.algorithm_version != EXCERPT_ALGORITHM_VERSION:
            raise ValueError("unsupported judging-excerpt algorithm version")
        if self.schema_version != EXCERPT_SCHEMA_VERSION:
            raise ValueError("unsupported judging-excerpt schema version")
        if not isinstance(self.redaction_applied, bool):
            raise TypeError("redaction_applied must be a bool")

    @property
    def rendered(self) -> str:
        """Title followed by bounded source context, as shown to a judge."""
        return self.title if not self.excerpt else f"{self.title}\n\n{self.excerpt}"

    def to_public_dict(self) -> dict[str, Any]:
        """Return the only fields allowed in a public judging packet candidate."""
        return {
            "schema_version": self.schema_version,
            "algorithm_version": self.algorithm_version,
            "title": self.title,
            "excerpt": self.excerpt,
            "source_hash": self.source_hash,
            "excerpt_hash": self.excerpt_hash,
            "redaction_applied": self.redaction_applied,
        }

    @classmethod
    def from_public_dict(cls, value: Mapping[str, Any]) -> "JudgingExcerpt":
        if not isinstance(value, Mapping):
            raise TypeError("judging excerpt must be an object")
        return cls(
            title=value.get("title", ""),
            excerpt=value.get("excerpt", ""),
            source_hash=value.get("source_hash", ""),
            excerpt_hash=value.get("excerpt_hash", ""),
            algorithm_version=value.get("algorithm_version", ""),
            redaction_applied=value.get("redaction_applied", False),
            schema_version=value.get("schema_version", EXCERPT_SCHEMA_VERSION),
        )


def build_query_centered_excerpt(
    title: str,
    body: str,
    query: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> JudgingExcerpt:
    """Build an immutable title-plus-context excerpt without evaluation metadata.

    ``max_chars`` bounds the complete rendered title/context string.  Context selection scores
    each source sentence/line by exact query-term overlap and uses the first best match as a
    deterministic tie-breaker; with no match it uses the leading context.  Source hashes use the
    original title/body, while the excerpt hash uses the redacted public rendering.
    """
    title = _require_text(title, "title")
    body = _require_text(body, "body")
    query = _require_text(query, "query")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")

    redacted_title = _normalise(redact_secrets(title))
    redacted_body = _normalise(redact_secrets(body))
    redacted_query = redact_secrets(query)
    source_hash = _sha256(_canonical_source(title, body))
    redaction_applied = redacted_title != _normalise(title) or redacted_body != _normalise(body)

    title_budget = min(len(redacted_title), max_chars)
    public_title = _truncate(redacted_title, title_budget)
    separator_budget = 2 if redacted_body and public_title else 0
    context_budget = max_chars - len(public_title) - separator_budget
    context = _bounded_context(redacted_body, redacted_query, context_budget)
    rendered = public_title if not context else f"{public_title}\n\n{context}"
    # The title may consume the complete budget.  This final guard is intentionally mechanical so
    # a future context-selection edit cannot violate the packet size contract.
    rendered = _truncate(rendered, max_chars)
    if rendered == public_title:
        context = ""
    elif rendered.startswith(public_title + "\n\n"):
        context = rendered[len(public_title) + 2 :]
    else:
        # This branch only occurs when the title itself had to be truncated at a boundary.
        public_title, _, context = rendered.partition("\n\n")
    return JudgingExcerpt(
        title=public_title,
        excerpt=context,
        source_hash=source_hash,
        excerpt_hash=_sha256(rendered),
        redaction_applied=redaction_applied,
    )


__all__ = [
    "DEFAULT_MAX_CHARS",
    "EXCERPT_ALGORITHM_VERSION",
    "EXCERPT_SCHEMA_VERSION",
    "JudgingExcerpt",
    "build_query_centered_excerpt",
]
