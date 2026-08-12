"""Typed, benchmark-only contracts for retrieval architecture experiments.

This module deliberately contains no FastEmbed imports and never opens a database.  It records
the inputs needed to make a retrieval comparison reproducible, plans names for disposable vector
indexes, and supplies deterministic helpers for synthetic benchmark rows.  A production search
path must not import this module as a source of model defaults.

The contracts are intentionally strict at the boundaries.  A vector from a different model
revision, dimension, prefix policy, representation, or corpus generation is not a compatible
substitute, even when the numerical shape happens to match.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping, Sequence, cast


EmbeddingKind = Literal["dense", "late_interaction"]
_KINDS = frozenset(("dense", "late_interaction"))
_NORMALIZATION_MODES = frozenset(("none", "l2"))
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
FASTEMBED_REGISTRY_REVISION = "fastembed-0.8.0"


class RetrievalContractError(ValueError):
    """Base error for malformed or incompatible benchmark contracts."""


class CompatibilityError(RetrievalContractError):
    """Raised when vectors, representations, or index generations cannot be mixed."""


class IncompleteCoverageError(CompatibilityError):
    """Raised when a run is used where complete retrieval coverage is required."""


class LateInteractionError(RetrievalContractError):
    """Raised when a late-interaction model is routed into dense-vector code."""


def _require_text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "" if allow_empty else " non-empty"
        raise RetrievalContractError(f"{field_name} must be a{suffix} string")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RetrievalContractError(f"{field_name} must be a positive integer")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text_hash(value: str) -> str:
    """Hash exact UTF-8 text; no whitespace or case normalization is performed."""
    return _sha256_bytes(value.encode("utf-8"))


def _json_hash(value: object) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _fingerprint(value: object) -> str:
    return _json_hash(value)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    """Immutable identity and preprocessing contract for one embedding model.

    ``revision`` is a pinned registry/model revision, while ``model_hash`` can be used when a
    caller has a content hash instead.  Exactly one must be supplied.  ``normalization`` is the
    output-vector normalization mode, not tokenizer text normalization.  A ``late_interaction``
    spec is valid as model metadata but is rejected by :func:`plan_isolated_index` and all dense
    index helpers.
    """

    model_id: str
    revision: str | None
    dimension: int
    query_prefix: str = ""
    document_prefix: str = ""
    normalization: str | bool = "l2"
    tokenizer: str = ""
    max_input_tokens: int = 512
    model_hash: str | None = None
    kind: EmbeddingKind = "dense"

    def __post_init__(self) -> None:
        _require_text(self.model_id, "model_id")
        if self.revision is None and self.model_hash is None:
            raise RetrievalContractError("one of revision or model_hash is required")
        if self.revision is not None and self.model_hash is not None:
            raise RetrievalContractError("provide revision or model_hash, not both")
        if self.revision is not None:
            _require_text(self.revision, "revision")
        if self.model_hash is not None:
            _require_text(self.model_hash, "model_hash")
            if not re.fullmatch(r"[0-9a-fA-F]{16,128}", self.model_hash):
                raise RetrievalContractError("model_hash must be a hexadecimal content hash")
        _require_positive_int(self.dimension, "dimension")
        _require_text(self.query_prefix, "query_prefix", allow_empty=True)
        _require_text(self.document_prefix, "document_prefix", allow_empty=True)
        _require_text(self.tokenizer, "tokenizer")
        _require_positive_int(self.max_input_tokens, "max_input_tokens")
        if isinstance(self.normalization, bool):
            object.__setattr__(self, "normalization", "l2" if self.normalization else "none")
        if self.normalization not in _NORMALIZATION_MODES:
            raise RetrievalContractError("normalization must be 'none' or 'l2'")
        if self.kind not in _KINDS:
            raise RetrievalContractError("kind must be 'dense' or 'late_interaction'")

    @property
    def normalized(self) -> bool:
        """Compatibility alias for callers that represent normalization as a boolean."""
        return self.normalization == "l2"

    @property
    def passage_prefix(self) -> str:
        """FastEmbed's passage terminology alias for ``document_prefix``."""
        return self.document_prefix

    @property
    def embedding_kind(self) -> EmbeddingKind:
        return self.kind

    @property
    def identity_key(self) -> tuple[object, ...]:
        """All model/preprocessing fields that affect vector compatibility."""
        return (
            self.model_id,
            self.revision,
            self.model_hash,
            self.dimension,
            self.query_prefix,
            self.document_prefix,
            self.normalization,
            self.tokenizer,
            self.max_input_tokens,
            self.kind,
        )

    def compatibility_key(self) -> str:
        return _fingerprint({"embedding": self.to_dict()})

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "revision": self.revision,
            "model_hash": self.model_hash,
            "dimension": self.dimension,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
            "normalization": self.normalization,
            "tokenizer": self.tokenizer,
            "max_input_tokens": self.max_input_tokens,
            "kind": self.kind,
        }

    def require_dense(self) -> "EmbeddingSpec":
        if self.kind != "dense":
            raise LateInteractionError(
                f"{self.model_id} is {self.kind}; late-interaction models cannot enter dense-vector code"
            )
        return self


@dataclass(frozen=True, slots=True)
class RepresentationSpec:
    """Immutable hashes for the authoritative representation used by a benchmark index."""

    representation_id: str
    title_hash: str
    body_hash: str
    chunks_hash: str
    retrieval_text_hash: str
    chunk_count: int
    title_source: str = "authoritative_title"
    body_source: str = "authoritative_body"
    chunking_revision: str = "chunks_v1"

    def __post_init__(self) -> None:
        _require_text(self.representation_id, "representation_id")
        for field_name in ("title_hash", "body_hash", "chunks_hash", "retrieval_text_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                raise RetrievalContractError(f"{field_name} must be a lowercase SHA-256 hash")
        _require_positive_int(self.chunk_count, "chunk_count")
        _require_text(self.title_source, "title_source")
        _require_text(self.body_source, "body_source")
        _require_text(self.chunking_revision, "chunking_revision")

    @classmethod
    def from_authoritative_text(
        cls,
        title: str,
        body: str,
        chunks: Sequence[str],
        retrieval_text_v1: str | None = None,
        *,
        representation_id: str = "retrieval_text_v1",
        chunking_revision: str = "chunks_v1",
    ) -> "RepresentationSpec":
        """Build a representation contract from authoritative title/body and chunk text.

        The default retrieval text is the existing benchmark convention (title, blank line,
        body).  Callers may pass the exact persisted ``retrieval_text_v1`` value when it was
        produced by a different, explicitly documented formatter.
        """
        _require_text(title, "title", allow_empty=True)
        _require_text(body, "body", allow_empty=True)
        if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
            raise RetrievalContractError("chunks must be a sequence of strings")
        frozen_chunks = tuple(_require_text(chunk, "chunk", allow_empty=True) for chunk in chunks)
        if not frozen_chunks:
            raise RetrievalContractError("chunks must contain at least one chunk")
        retrieval_text = f"{title}\n\n{body}" if retrieval_text_v1 is None else retrieval_text_v1
        _require_text(retrieval_text, "retrieval_text_v1", allow_empty=True)
        return cls(
            representation_id=representation_id,
            title_hash=text_hash(title),
            body_hash=text_hash(body),
            chunks_hash=_json_hash(frozen_chunks),
            retrieval_text_hash=text_hash(retrieval_text),
            chunk_count=len(frozen_chunks),
            chunking_revision=chunking_revision,
        )

    @property
    def authoritative_title_hash(self) -> str:
        return self.title_hash

    @property
    def authoritative_body_hash(self) -> str:
        return self.body_hash

    @property
    def retrieval_text_v1_hash(self) -> str:
        return self.retrieval_text_hash

    @property
    def representation_key(self) -> tuple[object, ...]:
        return (
            self.representation_id,
            self.title_source,
            self.body_source,
            self.title_hash,
            self.body_hash,
            self.chunks_hash,
            self.chunk_count,
            self.chunking_revision,
            self.retrieval_text_hash,
        )

    def compatibility_key(self) -> str:
        return _fingerprint({"representation": self.to_dict()})

    def to_dict(self) -> dict[str, object]:
        return {
            "representation_id": self.representation_id,
            "title_source": self.title_source,
            "body_source": self.body_source,
            "title_hash": self.title_hash,
            "body_hash": self.body_hash,
            "chunks_hash": self.chunks_hash,
            "chunk_count": self.chunk_count,
            "chunking_revision": self.chunking_revision,
            "retrieval_text_v1_hash": self.retrieval_text_hash,
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    """Per-query candidate coverage, independent from relevance or ranking quality."""

    expected: int
    observed: int
    complete: bool | None = None
    missing_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_positive_int(self.expected, "coverage.expected")
        if (
            not isinstance(self.observed, int)
            or isinstance(self.observed, bool)
            or self.observed < 0
        ):
            raise RetrievalContractError("coverage.observed must be a non-negative integer")
        if self.observed > self.expected:
            raise RetrievalContractError("coverage.observed cannot exceed coverage.expected")
        if self.complete is None:
            object.__setattr__(self, "complete", self.observed >= self.expected)
        if not isinstance(self.complete, bool):
            raise RetrievalContractError("coverage.complete must be boolean")
        if any(not isinstance(value, str) or not value for value in self.missing_ids):
            raise RetrievalContractError("coverage.missing_ids must contain non-empty strings")

    @property
    def ratio(self) -> float:
        return self.observed / self.expected

    def to_dict(self) -> dict[str, object]:
        return {
            "expected": self.expected,
            "observed": self.observed,
            "complete": self.complete,
            "missing_ids": list(self.missing_ids),
            "ratio": self.ratio,
        }


def _coerce_coverage(value: Coverage | Mapping[str, object] | float | int) -> Coverage:
    if isinstance(value, Coverage):
        return value
    if isinstance(value, Mapping):
        expected = value.get("expected", value.get("expected_count"))
        observed = value.get("observed", value.get("returned", value.get("returned_count")))
        if expected is None or observed is None:
            raise RetrievalContractError("coverage mapping requires expected and observed counts")
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise RetrievalContractError("coverage.expected must be an integer")
        if not isinstance(observed, int) or isinstance(observed, bool):
            raise RetrievalContractError("coverage.observed must be an integer")
        complete = value.get("complete")
        if complete is not None and not isinstance(complete, bool):
            raise RetrievalContractError("coverage.complete must be boolean")
        missing_ids = value.get("missing_ids", ())
        if not isinstance(missing_ids, Sequence) or isinstance(missing_ids, (str, bytes)):
            raise RetrievalContractError("coverage.missing_ids must be a sequence")
        return Coverage(
            expected,
            observed,
            complete,
            tuple(str(value) for value in missing_ids),
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RetrievalContractError("coverage must be Coverage, a mapping, or a finite ratio")
    if value < 0 or value > 1:
        raise RetrievalContractError("coverage ratio must be between zero and one")
    expected = 1_000_000
    observed = int(round(float(value) * expected))
    return Coverage(expected, observed, complete=float(value) >= 1.0)


def _coerce_rank_map(value: object, channel: str) -> Mapping[str, int]:
    if isinstance(value, Mapping):
        rows = dict(value)
        result: dict[str, int] = {}
        for candidate_id, rank in rows.items():
            _require_text(candidate_id, f"channel_ranks[{channel}] candidate_id")
            if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
                raise RetrievalContractError(f"channel rank for {candidate_id!r} must be positive")
            result[candidate_id] = rank
        return _freeze_mapping(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = {
            _require_text(candidate_id, f"channel_ranks[{channel}] candidate_id"): index
            for index, candidate_id in enumerate(value, 1)
        }
        if len(result) != len(value):
            raise RetrievalContractError(f"channel {channel!r} contains duplicate candidate IDs")
        return _freeze_mapping(result)
    raise RetrievalContractError(f"channel {channel!r} ranks must be a mapping or ordered IDs")


@dataclass(frozen=True, slots=True)
class RetrievalQueryResult:
    """One query's ordered IDs, raw score evidence, channel ranks, and diagnostics."""

    query_id: str
    query_text: str
    ordered_ids: tuple[str, ...]
    raw_scores: Mapping[str, float]
    channel_ranks: Mapping[str, Mapping[str, int] | Sequence[str]]
    coverage: Coverage | Mapping[str, object] | float | int
    failures: tuple[str, ...] = ()
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        _require_text(self.query_id, "query_id")
        _require_text(self.query_text, "query_text", allow_empty=True)
        ids = tuple(_require_text(value, "ordered_ids item") for value in self.ordered_ids)
        if len(ids) != len(set(ids)):
            raise RetrievalContractError("ordered_ids must be unique")
        object.__setattr__(self, "ordered_ids", ids)
        if not isinstance(self.raw_scores, Mapping):
            raise RetrievalContractError("raw_scores must be a mapping of candidate ID to score")
        score_map: dict[str, float] = {}
        for candidate_id, score in self.raw_scores.items():
            _require_text(candidate_id, "raw_scores candidate_id")
            try:
                value = float(score)
            except (TypeError, ValueError) as exc:
                raise RetrievalContractError("raw scores must be numeric") from exc
            if not math.isfinite(value):
                raise RetrievalContractError("raw scores must be finite")
            score_map[candidate_id] = value
        object.__setattr__(self, "raw_scores", _freeze_mapping(score_map))
        if not isinstance(self.channel_ranks, Mapping):
            raise RetrievalContractError("channel_ranks must be a mapping")
        channels = {
            _require_text(channel, "channel name"): _coerce_rank_map(ranks, str(channel))
            for channel, ranks in self.channel_ranks.items()
        }
        object.__setattr__(self, "channel_ranks", _freeze_mapping(channels))
        object.__setattr__(self, "coverage", _coerce_coverage(self.coverage))
        failures = tuple(
            _require_text(value, "failure", allow_empty=False) for value in self.failures
        )
        object.__setattr__(self, "failures", failures)
        try:
            latency = float(self.latency_ms)
        except (TypeError, ValueError) as exc:
            raise RetrievalContractError("latency_ms must be numeric") from exc
        if not math.isfinite(latency) or latency < 0:
            raise RetrievalContractError("latency_ms must be finite and non-negative")
        object.__setattr__(self, "latency_ms", latency)

    @property
    def is_complete(self) -> bool:
        coverage = _coerce_coverage(self.coverage)
        return bool(coverage.complete) and not self.failures

    def require_complete(self) -> "RetrievalQueryResult":
        if not self.is_complete:
            raise IncompleteCoverageError(
                f"query {self.query_id!r} has incomplete coverage or failures"
            )
        return self

    def to_dict(self) -> dict[str, object]:
        channels = {
            channel: dict(_coerce_rank_map(ranks, channel))
            for channel, ranks in self.channel_ranks.items()
        }
        return {
            "query_id": self.query_id,
            "query_text": self.query_text,
            "ordered_ids": list(self.ordered_ids),
            "raw_scores": dict(self.raw_scores),
            "channel_ranks": channels,
            "coverage": _coerce_coverage(self.coverage).to_dict(),
            "failures": list(self.failures),
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    """Run-level provenance binding vectors and representation to one generation/index."""

    embedding: EmbeddingSpec
    representation: RepresentationSpec
    generation: str
    index_namespace: str
    corpus_fingerprint: str | None = None
    source_revision: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.generation, "generation")
        _require_text(self.index_namespace, "index_namespace")
        if self.corpus_fingerprint is not None:
            _require_text(self.corpus_fingerprint, "corpus_fingerprint")
        if self.source_revision is not None:
            _require_text(self.source_revision, "source_revision")

    @property
    def compatibility_key(self) -> str:
        return build_compatibility_key(self.embedding, self.representation, self.generation)

    def to_dict(self) -> dict[str, object]:
        return {
            "embedding": self.embedding.to_dict(),
            "representation": self.representation.to_dict(),
            "generation": self.generation,
            "index_namespace": self.index_namespace,
            "corpus_fingerprint": self.corpus_fingerprint,
            "source_revision": self.source_revision,
            "compatibility_key": self.compatibility_key,
        }


@dataclass(frozen=True, slots=True)
class RetrievalRun:
    """Immutable benchmark result envelope with query-level retrieval evidence."""

    run_id: str
    provenance: RetrievalProvenance
    query_results: tuple[RetrievalQueryResult, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        rows = tuple(self.query_results)
        if any(not isinstance(row, RetrievalQueryResult) for row in rows):
            raise RetrievalContractError("query_results must contain RetrievalQueryResult values")
        query_ids = [row.query_id for row in rows]
        if len(query_ids) != len(set(query_ids)):
            raise RetrievalContractError("query_results must have unique query IDs")
        object.__setattr__(self, "query_results", rows)

    @property
    def results(self) -> tuple[RetrievalQueryResult, ...]:
        """Short alias used by benchmark callers."""
        return self.query_results

    @property
    def compatibility_key(self) -> str:
        return self.provenance.compatibility_key

    def require_complete_coverage(self) -> "RetrievalRun":
        for row in self.query_results:
            row.require_complete()
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "provenance": self.provenance.to_dict(),
            "query_results": [row.to_dict() for row in self.query_results],
        }


def build_compatibility_key(
    embedding: EmbeddingSpec,
    representation: RepresentationSpec,
    generation: str,
) -> str:
    """Build a strict key; all model and representation identity fields participate."""
    _require_text(generation, "generation")
    return _fingerprint(
        {
            "embedding": embedding.to_dict(),
            "representation": representation.to_dict(),
            "generation": generation,
        }
    )


def assert_compatible(
    expected_embedding: EmbeddingSpec,
    actual_embedding: EmbeddingSpec,
    expected_representation: RepresentationSpec | None = None,
    actual_representation: RepresentationSpec | None = None,
    expected_generation: str | None = None,
    actual_generation: str | None = None,
) -> None:
    """Reject every compatibility dimension explicitly, with an actionable reason."""
    mismatches: list[str] = []
    fields = (
        "model_id",
        "revision",
        "model_hash",
        "dimension",
        "query_prefix",
        "document_prefix",
        "normalization",
        "tokenizer",
        "max_input_tokens",
        "kind",
    )
    for field_name in fields:
        expected = getattr(expected_embedding, field_name)
        actual = getattr(actual_embedding, field_name)
        if expected != actual:
            mismatches.append(f"{field_name}: expected {expected!r}, got {actual!r}")
    if (expected_representation is None) != (actual_representation is None):
        mismatches.append("representation: one side is missing")
    elif expected_representation is not None and actual_representation is not None:
        if expected_representation.representation_key != actual_representation.representation_key:
            mismatches.append("representation: title/body/chunk/retrieval-text hash differs")
    if expected_generation != actual_generation:
        mismatches.append(
            f"generation: expected {expected_generation!r}, got {actual_generation!r}"
        )
    if mismatches:
        raise CompatibilityError("incompatible retrieval specs (" + "; ".join(mismatches) + ")")


validate_compatibility = assert_compatible


@dataclass(frozen=True, slots=True)
class IndexNamespace:
    """Names for an isolated disposable vector index and its compatibility identity."""

    namespace: str
    table_name: str
    database_name: str
    dimension: int
    kind: EmbeddingKind
    compatibility_key: str
    embedding_key: str
    representation_key: str
    generation: str

    def __post_init__(self) -> None:
        for field_name in ("namespace", "table_name", "database_name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise RetrievalContractError(f"{field_name} must be non-empty")
            if field_name != "database_name" and not _SAFE_IDENTIFIER_RE.fullmatch(value):
                raise RetrievalContractError(f"{field_name} is not a safe SQL identifier")
        _require_positive_int(self.dimension, "dimension")
        if self.kind not in _KINDS:
            raise RetrievalContractError("invalid index kind")
        _require_text(self.compatibility_key, "compatibility_key")
        _require_text(self.embedding_key, "embedding_key")
        _require_text(self.representation_key, "representation_key")
        _require_text(self.generation, "generation")


def _slug(value: str, *, fallback: str = "x") -> str:
    result = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    result = result or fallback
    if result[0].isdigit():
        result = "x_" + result
    return result[:20]


def plan_isolated_index(
    embedding: EmbeddingSpec,
    representation: RepresentationSpec,
    generation: str,
    *,
    namespace_prefix: str = "bench",
) -> IndexNamespace:
    """Plan dimension-specific safe identifiers without creating a DB or downloading a model."""
    embedding.require_dense()
    _require_text(namespace_prefix, "namespace_prefix")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", namespace_prefix):
        raise RetrievalContractError("namespace_prefix must be a lowercase SQL-safe prefix")
    _require_text(generation, "generation")
    compatibility = build_compatibility_key(embedding, representation, generation)
    short_key = compatibility[:16]
    # Keep the human-readable components short enough for SQLite's identifier limits while the
    # compatibility digest below preserves collision resistance and all strict identity fields.
    model = _slug(embedding.model_id)[:12]
    rep = _slug(representation.representation_id)[:8]
    gen = _slug(generation)[:8]
    namespace = f"{namespace_prefix}_{model}_{embedding.dimension}d_{rep}_{gen}_{short_key}"
    table = f"vec_{embedding.dimension}d_{short_key}"
    database = f"retrieval_{embedding.dimension}d_{short_key}.db"
    if not _SAFE_IDENTIFIER_RE.fullmatch(namespace) or not _SAFE_IDENTIFIER_RE.fullmatch(table):
        raise RetrievalContractError("generated index identifier is not SQL-safe")
    return IndexNamespace(
        namespace=namespace,
        table_name=table,
        database_name=database,
        dimension=embedding.dimension,
        kind=embedding.kind,
        compatibility_key=compatibility,
        embedding_key=embedding.compatibility_key(),
        representation_key=representation.compatibility_key(),
        generation=generation,
    )


build_isolated_index_plan = plan_isolated_index
isolated_index_namespace = plan_isolated_index


def assert_index_compatible(
    index: IndexNamespace,
    embedding: EmbeddingSpec,
    representation: RepresentationSpec,
    generation: str,
) -> None:
    """Ensure a planned table cannot silently receive vectors from another spec."""
    embedding.require_dense()
    expected = plan_isolated_index(embedding, representation, generation)
    if index.compatibility_key != expected.compatibility_key:
        raise CompatibilityError(
            "index namespace compatibility key differs; refusing vector reuse across specs"
        )
    if index.dimension != embedding.dimension:
        raise CompatibilityError("index dimension differs from embedding dimension")


@dataclass(frozen=True, slots=True)
class ChunkScore:
    """A raw chunk score used by entity-by-maximum-chunk aggregation."""

    entity_id: str
    chunk_id: str
    score: float

    def __post_init__(self) -> None:
        _require_text(self.entity_id, "entity_id")
        _require_text(self.chunk_id, "chunk_id")
        value = float(self.score)
        if not math.isfinite(value):
            raise RetrievalContractError("chunk score must be finite")
        object.__setattr__(self, "score", value)


@dataclass(frozen=True, slots=True)
class EntityAggregate:
    """Maximum raw chunk score for one entity; no corpus/global RRF bonus is included."""

    entity_id: str
    max_chunk_score: float
    best_chunk_id: str
    rank: int

    @property
    def score(self) -> float:
        return self.max_chunk_score


def _coerce_chunk_score(row: ChunkScore | Mapping[str, object]) -> ChunkScore:
    if isinstance(row, ChunkScore):
        return row
    if not isinstance(row, Mapping):
        raise RetrievalContractError("chunk rows must be ChunkScore or mappings")
    entity_id = row.get("entity_id", row.get("id"))
    chunk_id = row.get("chunk_id", row.get("chunk_index"))
    score = row.get("score", row.get("raw_score", row.get("dense_score")))
    if entity_id is None or chunk_id is None or score is None:
        raise RetrievalContractError("chunk row requires entity_id, chunk_id, and score")
    return ChunkScore(str(entity_id), str(chunk_id), float(cast(Any, score)))


def aggregate_entities_by_max_chunk(
    rows: Iterable[ChunkScore | Mapping[str, object]], *, limit: int | None = None
) -> tuple[EntityAggregate, ...]:
    """Aggregate entities by their best chunk score, without a global RRF contribution."""
    best: dict[str, ChunkScore] = {}
    for raw_row in rows:
        row = _coerce_chunk_score(raw_row)
        current = best.get(row.entity_id)
        if current is None or (row.score, row.chunk_id) > (current.score, current.chunk_id):
            best[row.entity_id] = row
    ordered = sorted(best.values(), key=lambda row: (-row.score, row.entity_id, row.chunk_id))
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise RetrievalContractError("limit must be a positive integer")
        ordered = ordered[:limit]
    return tuple(
        EntityAggregate(row.entity_id, row.score, row.chunk_id, rank)
        for rank, row in enumerate(ordered, 1)
    )


aggregate_by_max_chunk = aggregate_entities_by_max_chunk
max_chunk_entity_aggregation = aggregate_entities_by_max_chunk


@dataclass(frozen=True, slots=True)
class CandidateMetric:
    """Synthetic development metric row used by the conservative shortlist selector."""

    candidate_id: str
    kind: EmbeddingKind
    accuracy: float
    latency_ms: float
    safety_ok: bool = True
    coverage_complete: bool = True
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        if self.kind not in _KINDS:
            raise RetrievalContractError("candidate kind must be dense or late_interaction")
        accuracy = float(self.accuracy)
        latency = float(self.latency_ms)
        if not math.isfinite(accuracy) or not 0 <= accuracy <= 1:
            raise RetrievalContractError("accuracy must be a finite ratio between zero and one")
        if not math.isfinite(latency) or latency < 0:
            raise RetrievalContractError("latency_ms must be finite and non-negative")
        if not isinstance(self.safety_ok, bool) or not isinstance(self.coverage_complete, bool):
            raise RetrievalContractError("safety_ok and coverage_complete must be boolean")
        object.__setattr__(self, "accuracy", accuracy)
        object.__setattr__(self, "latency_ms", latency)
        object.__setattr__(
            self,
            "failures",
            tuple(_require_text(value, "candidate failure") for value in self.failures),
        )

    @property
    def safe(self) -> bool:
        return self.safety_ok and self.coverage_complete and not self.failures


def _coerce_candidate_metric(row: CandidateMetric | Mapping[str, object]) -> CandidateMetric:
    if isinstance(row, CandidateMetric):
        return row
    if not isinstance(row, Mapping):
        raise RetrievalContractError("candidate rows must be CandidateMetric or mappings")
    candidate_id = row.get("candidate_id", row.get("name", row.get("model_id")))
    kind = row.get("kind", "dense")
    accuracy = row.get("accuracy", row.get("recall", row.get("top1_accuracy")))
    latency = row.get("latency_ms", row.get("latency", 0.0))
    if candidate_id is None or accuracy is None:
        raise RetrievalContractError("candidate row requires candidate_id and accuracy")
    coverage_complete = row.get("coverage_complete", row.get("complete_coverage", True))
    raw_failures = row.get("failures", ())
    if not isinstance(raw_failures, Sequence) or isinstance(raw_failures, (str, bytes)):
        raise RetrievalContractError("candidate failures must be a sequence")
    failures: tuple[str, ...] = tuple(str(value) for value in raw_failures)
    kind_value = str(kind)
    if kind_value not in _KINDS:
        raise RetrievalContractError("candidate kind must be dense or late_interaction")
    return CandidateMetric(
        str(candidate_id),
        cast(EmbeddingKind, kind_value),
        float(cast(Any, accuracy)),
        float(cast(Any, latency)),
        bool(row.get("safety_ok", row.get("safe", True))),
        bool(coverage_complete),
        failures,
    )


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """Deterministic safe shortlist and explicit discard reasons."""

    dense_top_three: tuple[CandidateMetric, ...]
    survivors: tuple[CandidateMetric, ...]
    discarded: Mapping[str, str]

    @property
    def selected(self) -> CandidateMetric | None:
        return self.survivors[0] if self.survivors else None

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.survivors)


def _selection_key(candidate: CandidateMetric) -> tuple[float, float, str]:
    # Accuracy is the primary objective.  Latency is only a tie-breaker, then ID stabilizes ties.
    return (-candidate.accuracy, candidate.latency_ms, candidate.candidate_id)


def _discard_reason(candidate: CandidateMetric) -> str:
    reasons = [
        label
        for label, present in (
            ("safety", not candidate.safety_ok),
            ("incomplete_coverage", not candidate.coverage_complete),
            ("failure", bool(candidate.failures)),
        )
        if present
    ]
    return "+".join(reasons) or "unsafe"


def _partition_safe_candidates(
    rows: Sequence[CandidateMetric], discarded: dict[str, str]
) -> tuple[list[CandidateMetric], list[CandidateMetric]]:
    dense: list[CandidateMetric] = []
    late: list[CandidateMetric] = []
    for row in rows:
        if not row.safe:
            discarded[row.candidate_id] = _discard_reason(row)
        elif row.kind == "dense":
            dense.append(row)
        else:
            late.append(row)
    return dense, late


def _select_dense_top_three(
    safe_dense: Sequence[CandidateMetric], discarded: dict[str, str]
) -> tuple[CandidateMetric, ...]:
    ordered = sorted(safe_dense, key=_selection_key)
    shortlist = tuple(ordered[:3])
    for row in ordered[3:]:
        discarded[row.candidate_id] = "below_dense_top_three"
    return shortlist


def _select_late_interaction(
    safe_late: Sequence[CandidateMetric],
    dense_top_three: Sequence[CandidateMetric],
    discarded: dict[str, str],
) -> list[CandidateMetric]:
    threshold = dense_top_three[-1].accuracy if len(dense_top_three) == 3 else None
    qualifying: list[CandidateMetric] = []
    for row in safe_late:
        if threshold is None:
            discarded[row.candidate_id] = "fewer_than_three_dense_candidates"
        elif row.accuracy <= threshold:
            discarded[row.candidate_id] = "late_interaction_not_above_third_dense"
        else:
            qualifying.append(row)
    return qualifying


def screen_development_candidates(
    candidates: Iterable[CandidateMetric | Mapping[str, object]], *, dense_limit: int = 3
) -> ScreeningResult:
    """Keep the top three safe dense candidates and strict ColBERT survival semantics.

    A late-interaction candidate survives only when its accuracy is strictly greater than the
    third safe dense candidate's accuracy.  Unsafe, failed, or incompletely-covered candidates
    are discarded before ranking; latency never rescues an accuracy or safety failure.
    """
    if dense_limit != 3:
        raise RetrievalContractError("the development contract fixes dense_limit at exactly three")
    rows = tuple(_coerce_candidate_metric(row) for row in candidates)
    ids = [row.candidate_id for row in rows]
    if len(ids) != len(set(ids)):
        raise RetrievalContractError("candidate IDs must be unique")
    discarded: dict[str, str] = {}
    safe_dense, safe_late = _partition_safe_candidates(rows, discarded)
    dense_top_three = _select_dense_top_three(safe_dense, discarded)
    qualifying_late = _select_late_interaction(safe_late, dense_top_three, discarded)
    survivors = tuple(sorted((*dense_top_three, *qualifying_late), key=_selection_key))
    return ScreeningResult(dense_top_three, survivors, MappingProxyType(discarded))


def select_development_candidates(
    candidates: Iterable[CandidateMetric | Mapping[str, object]],
) -> tuple[CandidateMetric, ...]:
    """Return the accuracy-first, latency-second safe development shortlist."""
    return screen_development_candidates(candidates).survivors


screen_candidates = screen_development_candidates
lexicographic_select = select_development_candidates


# These are metadata-only declarations.  They intentionally do not instantiate FastEmbed or
# attempt to resolve/download model files.  Prefixes follow the corresponding model-card
# retrieval conventions: BGE/Arctic instruction for asymmetric retrieval, E5 query/passage
# labels, and empty prefixes where the model card specifies none.
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


FASTEMBED_DENSE_CANDIDATES: tuple[EmbeddingSpec, ...] = (
    EmbeddingSpec(
        "BAAI/bge-small-en-v1.5",
        FASTEMBED_REGISTRY_REVISION,
        384,
        _BGE_QUERY_PREFIX,
        "",
        "l2",
        "BAAI/bge-small-en-v1.5-tokenizer",
        512,
    ),
    EmbeddingSpec(
        "BAAI/bge-base-en-v1.5",
        FASTEMBED_REGISTRY_REVISION,
        768,
        _BGE_QUERY_PREFIX,
        "",
        "l2",
        "BAAI/bge-base-en-v1.5-tokenizer",
        512,
    ),
    EmbeddingSpec(
        "BAAI/bge-large-en-v1.5",
        FASTEMBED_REGISTRY_REVISION,
        1024,
        _BGE_QUERY_PREFIX,
        "",
        "l2",
        "BAAI/bge-large-en-v1.5-tokenizer",
        512,
    ),
    EmbeddingSpec(
        "snowflake/snowflake-arctic-embed-m-long",
        FASTEMBED_REGISTRY_REVISION,
        768,
        _BGE_QUERY_PREFIX,
        "",
        "l2",
        "snowflake/snowflake-arctic-embed-m-long-tokenizer",
        2048,
    ),
    EmbeddingSpec(
        "jinaai/jina-embeddings-v2-base-en",
        FASTEMBED_REGISTRY_REVISION,
        768,
        "",
        "",
        "l2",
        "jinaai/jina-embeddings-v2-base-en-tokenizer",
        8192,
    ),
    EmbeddingSpec(
        "nomic-ai/nomic-embed-text-v1.5",
        FASTEMBED_REGISTRY_REVISION,
        768,
        "search_query: ",
        "search_document: ",
        "l2",
        "nomic-ai/nomic-embed-text-v1.5-tokenizer",
        8192,
    ),
    EmbeddingSpec(
        "mixedbread-ai/mxbai-embed-large-v1",
        FASTEMBED_REGISTRY_REVISION,
        1024,
        _BGE_QUERY_PREFIX,
        "",
        "l2",
        "mixedbread-ai/mxbai-embed-large-v1-tokenizer",
        512,
    ),
    EmbeddingSpec(
        "intfloat/multilingual-e5-large",
        FASTEMBED_REGISTRY_REVISION,
        1024,
        "query: ",
        "passage: ",
        "l2",
        "intfloat/multilingual-e5-large-tokenizer",
        512,
    ),
)


FASTEMBED_LATE_INTERACTION_CANDIDATES: tuple[EmbeddingSpec, ...] = (
    EmbeddingSpec(
        "answerdotai/answerai-colbert-small-v1",
        FASTEMBED_REGISTRY_REVISION,
        96,
        "",
        "",
        "none",
        "answerdotai/answerai-colbert-small-v1-tokenizer",
        512,
        kind="late_interaction",
    ),
)

# Descriptive aliases used by benchmark scripts and external development notebooks.
ACCEPTED_DENSE_CANDIDATES = FASTEMBED_DENSE_CANDIDATES
DENSE_CANDIDATES = FASTEMBED_DENSE_CANDIDATES
COLBERT_CANDIDATE = FASTEMBED_LATE_INTERACTION_CANDIDATES[0]
ACCEPTED_LATE_INTERACTION_CANDIDATES = FASTEMBED_LATE_INTERACTION_CANDIDATES


def candidate_by_model_id(model_id: str) -> EmbeddingSpec:
    """Resolve one predeclared candidate without loading model code or weights."""
    for candidate in (*FASTEMBED_DENSE_CANDIDATES, *FASTEMBED_LATE_INTERACTION_CANDIDATES):
        if candidate.model_id == model_id:
            return candidate
    raise RetrievalContractError(f"model is not an accepted benchmark candidate: {model_id!r}")


__all__ = [
    "ACCEPTED_DENSE_CANDIDATES",
    "ACCEPTED_LATE_INTERACTION_CANDIDATES",
    "COLBERT_CANDIDATE",
    "CandidateMetric",
    "ChunkScore",
    "CompatibilityError",
    "Coverage",
    "DENSE_CANDIDATES",
    "EmbeddingSpec",
    "EntityAggregate",
    "FASTEMBED_DENSE_CANDIDATES",
    "FASTEMBED_LATE_INTERACTION_CANDIDATES",
    "FASTEMBED_REGISTRY_REVISION",
    "IncompleteCoverageError",
    "IndexNamespace",
    "LateInteractionError",
    "RepresentationSpec",
    "RetrievalContractError",
    "RetrievalProvenance",
    "RetrievalQueryResult",
    "RetrievalRun",
    "ScreeningResult",
    "aggregate_by_max_chunk",
    "aggregate_entities_by_max_chunk",
    "assert_compatible",
    "assert_index_compatible",
    "build_compatibility_key",
    "build_isolated_index_plan",
    "candidate_by_model_id",
    "isolated_index_namespace",
    "lexicographic_select",
    "max_chunk_entity_aggregation",
    "plan_isolated_index",
    "screen_candidates",
    "screen_development_candidates",
    "select_development_candidates",
    "text_hash",
    "validate_compatibility",
]
