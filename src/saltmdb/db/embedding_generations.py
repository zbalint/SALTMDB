"""Shadow-build and atomically activate isolated embedding generations.

The legacy 384-dimensional tables remain untouched until a benchmark winner is selected.  This
module supplies the migration contract needed by that later rollout: every generation owns a
dimension-specific vec0 table and immutable specification, while activation fails closed unless
coverage and health checks are complete.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Sequence


GENERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class GenerationSpec:
    model_id: str
    model_revision: str
    dimension: int
    query_prefix: str
    document_prefix: str
    normalization: str
    representation: str
    source_manifest_hash: str

    def __post_init__(self) -> None:
        required = (
            self.model_id,
            self.model_revision,
            self.normalization,
            self.representation,
            self.source_manifest_hash,
        )
        if any(not value for value in required) or self.dimension < 1:
            raise ValueError("generation specification is incomplete")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def generation_table_name(generation_id: str, dimension: int) -> str:
    """Return a safe, short identifier derived from the immutable generation identity."""
    if not GENERATION_ID_RE.fullmatch(generation_id) or dimension < 1:
        raise ValueError("invalid generation identifier or dimension")
    token = hashlib.sha256(generation_id.encode()).hexdigest()[:16]
    return f"entity_embeddings_g_{token}_d{dimension}"


def init_embedding_generation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS embedding_generations (
            id TEXT PRIMARY KEY,
            spec_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL,
            vector_table TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL DEFAULT 'building'
                CHECK(state IN ('building','ready','active','failed','retired')),
            expected_count INTEGER NOT NULL CHECK(expected_count >= 0),
            embedded_count INTEGER NOT NULL DEFAULT 0 CHECK(embedded_count >= 0),
            health_check_hash TEXT,
            created_at TEXT NOT NULL,
            activated_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_embedding_generation
            ON embedding_generations(state) WHERE state='active';
        CREATE TABLE IF NOT EXISTS embedding_generation_entities (
            generation_id TEXT NOT NULL REFERENCES embedding_generations(id) ON DELETE CASCADE,
            entity_id TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            vector_checksum TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('ready','failed')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(generation_id, entity_id)
        );
        CREATE TABLE IF NOT EXISTS embedding_generation_activations (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id TEXT NOT NULL REFERENCES embedding_generations(id),
            previous_generation_id TEXT,
            activated_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        """
    )


def register_generation(
    conn: sqlite3.Connection, generation_id: str, spec: GenerationSpec, *, expected_count: int
) -> str:
    """Register an immutable generation and create its isolated vec0 table."""
    if expected_count < 1:
        raise ValueError("eligible generation coverage must be non-zero")
    init_embedding_generation_schema(conn)
    table = generation_table_name(generation_id, spec.dimension)
    existing = conn.execute(
        "SELECT spec_hash,expected_count,vector_table FROM embedding_generations WHERE id=?",
        (generation_id,),
    ).fetchone()
    if existing:
        if tuple(existing) != (spec.fingerprint, expected_count, table):
            raise ValueError("generation ID already exists with incompatible metadata")
        return table
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f'CREATE VIRTUAL TABLE "{table}" USING vec0('  # noqa: S608 -- table is validated+derived
        f"entity_id TEXT PRIMARY KEY, embedding FLOAT[{spec.dimension}], "
        "+source_hash TEXT, +vector_checksum TEXT)"
    )
    conn.execute(
        "INSERT INTO embedding_generations "
        "(id,spec_json,spec_hash,vector_table,expected_count,created_at) VALUES (?,?,?,?,?,?)",
        (
            generation_id,
            json.dumps(asdict(spec), sort_keys=True, separators=(",", ":")),
            spec.fingerprint,
            table,
            expected_count,
            _now(),
        ),
    )
    return table


def record_generation_vector(
    conn: sqlite3.Connection,
    generation_id: str,
    entity_id: str,
    source_hash: str,
    vector: Sequence[float],
) -> str:
    """Checksum and upsert one vector, making interrupted builds safely resumable."""
    row = conn.execute(
        "SELECT spec_json,vector_table,state FROM embedding_generations WHERE id=?",
        (generation_id,),
    ).fetchone()
    if not row or row[2] not in {"building", "ready"}:
        raise ValueError("generation is unavailable for embedding writes")
    spec = json.loads(row[0])
    if len(vector) != spec["dimension"]:
        raise ValueError("vector dimension does not match generation")
    import sqlite_vec

    blob = sqlite_vec.serialize_float32([float(value) for value in vector])
    checksum = hashlib.sha256(blob).hexdigest()
    table = str(row[1])
    conn.execute(f'DELETE FROM "{table}" WHERE entity_id=?', (entity_id,))  # noqa: S608
    conn.execute(
        f'INSERT INTO "{table}" (entity_id,embedding,source_hash,vector_checksum) VALUES (?,?,?,?)',  # noqa: S608
        (entity_id, blob, source_hash, checksum),
    )
    conn.execute(
        "INSERT INTO embedding_generation_entities "
        "(generation_id,entity_id,source_hash,vector_checksum,state,updated_at) "
        "VALUES (?,?,?,?, 'ready',?) ON CONFLICT(generation_id,entity_id) DO UPDATE SET "
        "source_hash=excluded.source_hash,vector_checksum=excluded.vector_checksum,"
        "state='ready',updated_at=excluded.updated_at",
        (generation_id, entity_id, source_hash, checksum, _now()),
    )
    conn.execute(
        "UPDATE embedding_generations SET embedded_count=(SELECT COUNT(*) FROM "
        "embedding_generation_entities WHERE generation_id=? AND state='ready') WHERE id=?",
        (generation_id, generation_id),
    )
    return checksum


def mark_generation_ready(
    conn: sqlite3.Connection, generation_id: str, health_check_hash: str
) -> None:
    """Seal a fully covered generation after external retrieval/health verification."""
    row = conn.execute(
        "SELECT state,expected_count,embedded_count FROM embedding_generations WHERE id=?",
        (generation_id,),
    ).fetchone()
    if not row or row[0] != "building":
        raise ValueError("only a building generation can become ready")
    if row[1] != row[2] or not health_check_hash:
        raise ValueError("generation coverage or health verification is incomplete")
    conn.execute(
        "UPDATE embedding_generations SET state='ready',health_check_hash=? WHERE id=?",
        (health_check_hash, generation_id),
    )


def activate_generation(
    conn: sqlite3.Connection, generation_id: str, *, reason: str = "benchmark winner"
) -> None:
    """Atomically switch the active generation; a prior generation remains rollback-ready."""
    row = conn.execute(
        "SELECT state,expected_count,embedded_count,health_check_hash FROM embedding_generations WHERE id=?",
        (generation_id,),
    ).fetchone()
    if not row or row[0] not in {"ready", "active"}:
        raise ValueError("generation is not ready for activation")
    if row[1] != row[2] or not row[3] or not reason:
        raise ValueError("generation cannot activate without full coverage and health checks")
    current = conn.execute(
        "SELECT id FROM embedding_generations WHERE state='active' AND id != ?", (generation_id,)
    ).fetchone()
    previous = current[0] if current else None
    if previous:
        conn.execute("UPDATE embedding_generations SET state='ready' WHERE id=?", (previous,))
    now = _now()
    conn.execute(
        "UPDATE embedding_generations SET state='active',activated_at=? WHERE id=?",
        (now, generation_id),
    )
    conn.execute(
        "INSERT INTO embedding_generation_activations "
        "(generation_id,previous_generation_id,activated_at,reason) VALUES (?,?,?,?)",
        (generation_id, previous, now, reason),
    )


def validate_active_generation(conn: sqlite3.Connection, expected: GenerationSpec) -> str:
    """Daemon startup guard for model/revision/dimension/coverage/staleness mismatches."""
    row = conn.execute(
        "SELECT id,spec_hash,vector_table,expected_count,embedded_count,health_check_hash "
        "FROM embedding_generations WHERE state='active'"
    ).fetchone()
    if not row:
        raise RuntimeError("no active embedding generation")
    if row[1] != expected.fingerprint:
        raise RuntimeError("active embedding generation model/revision/specification mismatch")
    if row[3] != row[4] or not row[5]:
        raise RuntimeError("active embedding generation is incomplete or unhealthy")
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (row[2],)
    ).fetchone()
    if not ddl or f"FLOAT[{expected.dimension}]" not in (ddl[0] or ""):
        raise RuntimeError("active embedding generation has wrong or missing vector dimension")
    return str(row[0])
