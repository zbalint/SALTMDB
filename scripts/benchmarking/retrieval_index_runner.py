"""Disposable, resumable index storage and deterministic retrieval for the bakeoff.

The stores in this module are run-private SQLite sidecars, never the SALTMDB production database.
Dense vectors and late-interaction token matrices use different schemas and runner classes so a
ColBERT row cannot enter a dense table accidentally.  Inference is injected through narrow
protocols; model download policy belongs to the adapter layer.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np


SCHEMA_VERSION = 1
CHANNELS = frozenset({"entity", "chunk", "retrieval_text"})


class IndexRunnerError(ValueError):
    """A sidecar, vector, or resume invariant is invalid."""


class DenseAdapter(Protocol):
    dimension: int
    compatibility_key: str

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class LateInteractionAdapter(Protocol):
    dimension: int
    compatibility_key: str

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[Sequence[float]]]: ...

    def embed_query(self, text: str) -> Sequence[Sequence[float]]: ...

    def maxsim(
        self, query_tokens: Sequence[Sequence[float]], document_tokens: Sequence[Sequence[float]]
    ) -> float: ...


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pack_vector(vector: Sequence[float], dimension: int) -> bytes:
    values = tuple(float(item) for item in vector)
    if len(values) != dimension or any(not math.isfinite(item) for item in values):
        raise IndexRunnerError("vector dimension mismatch or non-finite value")
    return struct.pack(f"<{dimension}f", *values)


def _unpack_vector(value: bytes, dimension: int) -> tuple[float, ...]:
    if len(value) != dimension * 4:
        raise IndexRunnerError("stored vector byte length does not match dimension")
    return struct.unpack(f"<{dimension}f", value)


def _pack_matrix(matrix: Sequence[Sequence[float]], dimension: int) -> tuple[bytes, int]:
    # In a real ColBERT run ``matrix`` is a numpy 2D array (tokens, dimension); ``if not matrix``
    # raises "truth value of an array with more than one element is ambiguous" for any matrix
    # with more than one token row, which is every real document. ``len()`` is unambiguous for
    # both numpy arrays and plain sequences.
    if len(matrix) == 0:
        raise IndexRunnerError("late-interaction matrix cannot be empty")
    rows = [_pack_vector(row, dimension) for row in matrix]
    return b"".join(rows), len(rows)


def _unpack_matrix(value: bytes, token_count: int, dimension: int) -> tuple[tuple[float, ...], ...]:
    if token_count <= 0 or len(value) != token_count * dimension * 4:
        raise IndexRunnerError("stored token matrix shape mismatch")
    row_size = dimension * 4
    return tuple(
        _unpack_vector(value[offset : offset + row_size], dimension)
        for offset in range(0, len(value), row_size)
    )


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise IndexRunnerError("query/index vector dimension mismatch")
    return sum(float(a) * float(b) for a, b in zip(left, right))


@dataclass(frozen=True, slots=True)
class IndexDocument:
    item_id: str
    entity_id: str
    channel: str
    text: str
    source_hash: str
    representation_hash: str

    def __post_init__(self) -> None:
        if not self.item_id or not self.entity_id or self.channel not in CHANNELS or not self.text:
            raise IndexRunnerError("index document identity/channel/text is invalid")
        for name in ("source_hash", "representation_hash"):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise IndexRunnerError(f"{name} must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class SearchHit:
    entity_id: str
    item_id: str
    score: float
    rank: int


def authoritative_documents(
    entity_id: str,
    title: str,
    body: str,
    chunks: Sequence[str],
    source_hash: str,
    representation_hash: str,
) -> list[IndexDocument]:
    """Render title+body entity text and title+chunk rows in deterministic order."""
    normalized_title = " ".join(title.split())
    normalized_body = " ".join(body.split())
    entity_text = "\n\n".join(item for item in (normalized_title, normalized_body) if item)
    if not entity_text:
        raise IndexRunnerError("authoritative entity text cannot be empty")
    effective_chunks = list(chunks) or [""]
    documents = [
        IndexDocument(
            entity_id,
            entity_id,
            "entity",
            entity_text,
            source_hash,
            representation_hash,
        )
    ]
    for index, chunk in enumerate(effective_chunks):
        normalized_chunk = " ".join(chunk.split())
        chunk_text = "\n\n".join(item for item in (normalized_title, normalized_chunk) if item)
        documents.append(
            IndexDocument(
                f"{entity_id}:chunk:{index:04d}",
                entity_id,
                "chunk",
                chunk_text,
                source_hash,
                representation_hash,
            )
        )
    return documents


class _Sidecar:
    def __init__(
        self,
        path: Path,
        *,
        compatibility_key: str,
        representation_root: str,
        dimension: int,
        kind: str,
    ) -> None:
        if dimension <= 0 or kind not in {"dense", "late_interaction"}:
            raise IndexRunnerError("sidecar dimension/kind is invalid")
        self.path = path
        self.compatibility_key = compatibility_key
        self.representation_root = representation_root
        self.dimension = dimension
        self.kind = kind
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS index_meta (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                schema_version INTEGER NOT NULL,
                kind TEXT NOT NULL,
                compatibility_key TEXT NOT NULL,
                representation_root TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                ready INTEGER NOT NULL DEFAULT 0,
                expected_count INTEGER,
                completed_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS failures (
                item_id TEXT PRIMARY KEY,
                error TEXT NOT NULL,
                created_ns INTEGER NOT NULL
            );
            """
        )
        row = self.conn.execute("SELECT * FROM index_meta WHERE singleton = 1").fetchone()
        identity = (
            SCHEMA_VERSION,
            self.kind,
            self.compatibility_key,
            self.representation_root,
            self.dimension,
        )
        if row is None:
            self.conn.execute(
                "INSERT INTO index_meta(singleton, schema_version, kind, compatibility_key, "
                "representation_root, dimension) VALUES (1, ?, ?, ?, ?, ?)",
                identity,
            )
            self.conn.commit()
            return
        observed = tuple(
            row[name]
            for name in (
                "schema_version",
                "kind",
                "compatibility_key",
                "representation_root",
                "dimension",
            )
        )
        if observed != identity:
            raise IndexRunnerError("sidecar identity mismatch; cross-model resume refused")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "_Sidecar":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _mark_progress(self, expected: int) -> None:
        failures = self.conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        completed = self.row_count()
        ready = int(completed == expected and failures == 0)
        self.conn.execute(
            "UPDATE index_meta SET expected_count = ?, completed_count = ?, ready = ? "
            "WHERE singleton = 1",
            (expected, completed, ready),
        )
        self.conn.commit()

    def row_count(self) -> int:
        raise NotImplementedError

    def receipt(self) -> dict[str, Any]:
        meta = dict(self.conn.execute("SELECT * FROM index_meta WHERE singleton = 1").fetchone())
        meta["sidecar_sha256"] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        meta["failure_count"] = self.conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        meta["receipt_fingerprint"] = fingerprint(meta)
        return meta


class DenseIndexRunner(_Sidecar):
    def __init__(self, path: Path, adapter: DenseAdapter, *, representation_root: str) -> None:
        self.adapter = adapter
        super().__init__(
            path,
            compatibility_key=adapter.compatibility_key,
            representation_root=representation_root,
            dimension=adapter.dimension,
            kind="dense",
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dense_vectors (
                item_id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                representation_hash TEXT NOT NULL,
                vector BLOB NOT NULL,
                vector_sha256 TEXT NOT NULL,
                rendered_text_sha256 TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def row_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM dense_vectors").fetchone()[0]

    def build(self, documents: Sequence[IndexDocument], *, batch_size: int = 32) -> dict[str, Any]:
        ordered = sorted(documents, key=lambda item: (item.channel, item.entity_id, item.item_id))
        if len({item.item_id for item in ordered}) != len(ordered):
            raise IndexRunnerError("index documents contain duplicate item IDs")
        for offset in range(0, len(ordered), batch_size):
            batch = [
                item for item in ordered[offset : offset + batch_size] if not self._is_fresh(item)
            ]
            if not batch:
                continue
            vectors = self.adapter.embed_documents([item.text for item in batch])
            if len(vectors) != len(batch):
                raise IndexRunnerError("adapter returned the wrong vector count")
            with self.conn:
                for item, vector in zip(batch, vectors):
                    packed = _pack_vector(vector, self.dimension)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO dense_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.item_id,
                            item.entity_id,
                            item.channel,
                            item.source_hash,
                            item.representation_hash,
                            packed,
                            hashlib.sha256(packed).hexdigest(),
                            _text_hash(item.text),
                        ),
                    )
        self._mark_progress(len(ordered))
        return self.receipt()

    def _is_fresh(self, item: IndexDocument) -> bool:
        row = self.conn.execute(
            "SELECT source_hash, representation_hash, vector, vector_sha256, rendered_text_sha256 "
            "FROM dense_vectors WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
        return bool(
            row
            and row["source_hash"] == item.source_hash
            and row["representation_hash"] == item.representation_hash
            and row["rendered_text_sha256"] == _text_hash(item.text)
            and row["vector_sha256"] == hashlib.sha256(row["vector"]).hexdigest()
            and len(row["vector"]) == self.dimension * 4
        )

    def search(self, query: str, channel: str, *, limit: int = 20) -> list[SearchHit]:
        if channel not in CHANNELS or limit <= 0:
            raise IndexRunnerError("search channel/limit is invalid")
        query_vector = _pack_vector(self.adapter.embed_query(query), self.dimension)
        unpacked_query = _unpack_vector(query_vector, self.dimension)
        rows = self.conn.execute(
            "SELECT item_id, entity_id, vector, vector_sha256 FROM dense_vectors "
            "WHERE channel = ? ORDER BY item_id",
            (channel,),
        ).fetchall()
        best: dict[str, tuple[float, str]] = {}
        for row in rows:
            if row["vector_sha256"] != hashlib.sha256(row["vector"]).hexdigest():
                raise IndexRunnerError("stored vector checksum mismatch")
            score = _dot(unpacked_query, _unpack_vector(row["vector"], self.dimension))
            previous = best.get(row["entity_id"])
            candidate = (score, row["item_id"])
            if previous is None or candidate > previous:
                best[row["entity_id"]] = candidate
        ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[0], item[1][1]))[:limit]
        return [
            SearchHit(entity_id, item_id, score, rank)
            for rank, (entity_id, (score, item_id)) in enumerate(ranked, 1)
        ]


class LateInteractionIndexRunner(_Sidecar):
    def __init__(
        self, path: Path, adapter: LateInteractionAdapter, *, representation_root: str
    ) -> None:
        self.adapter = adapter
        super().__init__(
            path,
            compatibility_key=adapter.compatibility_key,
            representation_root=representation_root,
            dimension=adapter.dimension,
            kind="late_interaction",
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS token_vectors (
                item_id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                representation_hash TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                vectors BLOB NOT NULL,
                vectors_sha256 TEXT NOT NULL,
                rendered_text_sha256 TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def row_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM token_vectors").fetchone()[0]

    def build(self, documents: Sequence[IndexDocument], *, batch_size: int = 16) -> dict[str, Any]:
        if any(item.channel != "entity" for item in documents):
            raise IndexRunnerError("late-interaction sidecar accepts entity documents only")
        ordered = sorted(documents, key=lambda item: item.item_id)
        if len({item.item_id for item in ordered}) != len(ordered):
            raise IndexRunnerError("index documents contain duplicate item IDs")
        for offset in range(0, len(ordered), batch_size):
            batch = [
                item for item in ordered[offset : offset + batch_size] if not self._is_fresh(item)
            ]
            if not batch:
                continue
            matrices = self.adapter.embed_documents([item.text for item in batch])
            if len(matrices) != len(batch):
                raise IndexRunnerError("adapter returned the wrong matrix count")
            with self.conn:
                for item, matrix in zip(batch, matrices):
                    packed, token_count = _pack_matrix(matrix, self.dimension)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO token_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item.item_id,
                            item.entity_id,
                            item.source_hash,
                            item.representation_hash,
                            token_count,
                            packed,
                            hashlib.sha256(packed).hexdigest(),
                            _text_hash(item.text),
                        ),
                    )
        self._mark_progress(len(ordered))
        return self.receipt()

    def _is_fresh(self, item: IndexDocument) -> bool:
        row = self.conn.execute(
            "SELECT source_hash, representation_hash, token_count, vectors, vectors_sha256, "
            "rendered_text_sha256 FROM token_vectors WHERE item_id = ?",
            (item.item_id,),
        ).fetchone()
        return bool(
            row
            and row["source_hash"] == item.source_hash
            and row["representation_hash"] == item.representation_hash
            and row["rendered_text_sha256"] == _text_hash(item.text)
            and row["vectors_sha256"] == hashlib.sha256(row["vectors"]).hexdigest()
            and len(row["vectors"]) == row["token_count"] * self.dimension * 4
        )

    def search(self, query: str, *, limit: int = 20) -> list[SearchHit]:
        if limit <= 0:
            raise IndexRunnerError("search limit must be positive")
        query_tokens = self.adapter.embed_query(query)
        _pack_matrix(query_tokens, self.dimension)
        cursor = self.conn.execute("SELECT * FROM token_vectors ORDER BY item_id")
        return self._rank_rows(query_tokens, cursor, limit=limit)

    def search_subset(
        self, query: str, item_ids: Sequence[str], *, limit: int = 20
    ) -> list[SearchHit]:
        """Exact MaxSim-rerank a deterministic, caller-selected entity candidate set."""
        if limit <= 0:
            raise IndexRunnerError("search limit must be positive")
        ordered = sorted(set(item_ids))
        if not ordered:
            raise IndexRunnerError("late-interaction candidate set must not be empty")
        if len(ordered) > 999:
            raise IndexRunnerError("late-interaction candidate set exceeds SQLite bind limit")
        query_tokens = self.adapter.embed_query(query)
        _pack_matrix(query_tokens, self.dimension)
        placeholders = ", ".join("?" for _ in ordered)
        cursor = self.conn.execute(
            f"SELECT * FROM token_vectors WHERE item_id IN ({placeholders}) ORDER BY item_id",
            ordered,
        )
        return self._rank_rows(query_tokens, cursor, limit=limit)

    def _rank_rows(
        self, query_tokens: object, cursor: sqlite3.Cursor, *, limit: int
    ) -> list[SearchHit]:
        query_matrix = np.asarray(query_tokens, dtype=np.float64)
        scored = []
        while rows := cursor.fetchmany(128):
            maximum_tokens = max(row["token_count"] for row in rows)
            matrices = np.zeros((len(rows), maximum_tokens, self.dimension), dtype=np.float64)
            token_counts = np.empty(len(rows), dtype=np.intp)
            for index, row in enumerate(rows):
                if row["vectors_sha256"] != hashlib.sha256(row["vectors"]).hexdigest():
                    raise IndexRunnerError("stored token matrix checksum mismatch")
                token_count = row["token_count"]
                token_counts[index] = token_count
                matrices[index, :token_count] = _unpack_matrix(
                    row["vectors"], token_count, self.dimension
                )
            similarities = np.matmul(query_matrix[None, :, :], matrices.transpose(0, 2, 1))
            padding = np.arange(maximum_tokens)[None, None, :] >= token_counts[:, None, None]
            similarities = np.where(padding, -np.inf, similarities)
            maxima = np.max(similarities, axis=2)
            scores = np.sum(maxima, axis=1)
            if not np.isfinite(scores).all():
                raise IndexRunnerError("late-interaction score is non-finite")
            scored.extend(
                (float(score), row["entity_id"], row["item_id"]) for score, row in zip(scores, rows)
            )
        ranked = sorted(scored, key=lambda item: (-item[0], item[1], item[2]))[:limit]
        return [
            SearchHit(entity_id, item_id, score, rank)
            for rank, (score, entity_id, item_id) in enumerate(ranked, 1)
        ]


def timed_search(function: Any, *args: Any, **kwargs: Any) -> tuple[Any, float, str | None]:
    """Record one search latency and convert an execution failure to explicit evidence."""
    started = time.perf_counter_ns()
    try:
        return function(*args, **kwargs), (time.perf_counter_ns() - started) / 1_000_000, None
    except Exception as exc:  # benchmark receipt must retain a candidate-specific failure
        return [], (time.perf_counter_ns() - started) / 1_000_000, f"{type(exc).__name__}: {exc}"
