"""Read-only, deterministic corpus snapshot export for evaluation callers.

The benchmark layer must not open the production SQLite database or embed SQL of its own.  This
service is the narrow read boundary: it exports authoritative entity fields in keyset pages and
binds every page to a logical database/schema provenance hash.  Each page is read under one
explicit SQLite read transaction.  A caller passes the returned ``snapshot_hash`` on the next
page; if the authoritative corpus or schema changed, the next call fails closed.

The service deliberately exports no vectors, events, non-supersedes relations, or agent-generated
enrichment.  The currently-valid ``supersedes`` edges needed for lifecycle resolution are included
and separately fingerprinted.  ``metadata`` is preserved as the authoritative source metadata
string, while ``content_hash`` is exposed as ``source_hash`` for downstream manifests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterator

from saltmdb.db.connection import managed_connection


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1_000
_HASH_CHUNK_ROWS = 256

_ENTITY_COLUMNS = (
    "id",
    "title",
    "full_content",
    "status",
    "content_hash",
    "metadata",
    "created_at",
    "updated_at",
    "owner_id",
    "scope",
    "is_core",
    "weight",
    "context_id",
    "memory_type",
)


class CorpusSnapshotError(RuntimeError):
    """Base error for malformed snapshot requests or unavailable schema."""


class SnapshotChangedError(CorpusSnapshotError):
    """Raised when a requested page no longer belongs to the frozen snapshot."""


@dataclass(frozen=True, slots=True)
class SnapshotProvenance:
    """Stable logical provenance shared by every page of one export."""

    schema_hash: str
    corpus_hash: str
    database_hash: str
    snapshot_hash: str
    entity_count: int
    include_archived: bool
    owner_id: str
    relation_root_hash: str
    relation_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_hash": self.schema_hash,
            "corpus_hash": self.corpus_hash,
            "database_hash": self.database_hash,
            "snapshot_hash": self.snapshot_hash,
            "entity_count": self.entity_count,
            "include_archived": self.include_archived,
            "owner_id": self.owner_id,
            "relation_root_hash": self.relation_root_hash,
            "relation_count": self.relation_count,
        }


@contextmanager
def _read_transaction(conn: sqlite3.Connection) -> Iterator[int]:
    """Open exactly one explicit read transaction and detect concurrent commits.

    SQLite's snapshot isolation ensures all SELECTs in the block see one database version.  The
    post-commit ``data_version`` check catches a concurrent writer that committed while this
    page was being read; the next page's provenance check catches a change between page calls.
    """
    if conn.in_transaction:
        raise CorpusSnapshotError("snapshot export requires a connection with no open transaction")
    before_version = _data_version(conn)
    conn.execute("BEGIN;")
    try:
        yield before_version
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    else:
        conn.execute("COMMIT;")
        after_version = _data_version(conn)
        if after_version != before_version:
            raise SnapshotChangedError(
                "database changed while the snapshot page was being read; retry from the first page"
            )


def _data_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA data_version;").fetchone()
    if not row or not isinstance(row[0], int):
        raise CorpusSnapshotError("SQLite did not return a usable data_version")
    return row[0]


def _sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _schema_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE sql IS NOT NULL
         ORDER BY type, name, tbl_name
        """
    ).fetchall()
    schema_version = conn.execute("PRAGMA schema_version;").fetchone()[0]
    user_version = conn.execute("PRAGMA user_version;").fetchone()[0]
    return _sha256_json(
        {
            "schema_version": schema_version,
            "user_version": user_version,
            "objects": [list(row) for row in rows],
        }
    )


def _require_entity_columns(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(entities);").fetchall()
    available = {row[1] for row in rows}
    missing = sorted(set(_ENTITY_COLUMNS) - available)
    if missing:
        raise CorpusSnapshotError(
            "entities table is missing authoritative snapshot columns: " + ", ".join(missing)
        )


def _visible_predicate(*, include_archived: bool) -> str:
    lifecycle = "" if include_archived else " AND status != 'archived'"
    return "(owner_id = ? OR scope = 'shared')" + lifecycle


def _entity_rows(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    include_archived: bool,
    after_id: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row | tuple[Any, ...]]:
    predicates: list[str] = []
    params: list[object] = []
    predicates.append(_visible_predicate(include_archived=include_archived))
    params.append(owner_id)
    if after_id is not None:
        predicates.append("id > ?")
        params.append(after_id)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    sql = "SELECT " + ", ".join(_ENTITY_COLUMNS) + " FROM entities" + where + " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()


def _supersedes_edges(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    include_archived: bool,
    as_of: str,
) -> tuple[list[dict[str, object]], str]:
    """Return all currently-valid supersedes edges between visible entities."""
    visible = _visible_predicate(include_archived=include_archived)
    rows = conn.execute(
        f"""
        WITH visible_entities AS (
            SELECT id
              FROM entities
             WHERE {visible}
        )
        SELECT r.id, r.source_id, r.target_id, r.predicate, r.created_at,
               r.valid_from, r.valid_to, r.valid_at, r.invalid_at
          FROM relations AS r
          JOIN visible_entities AS source_entity ON source_entity.id = r.source_id
          JOIN visible_entities AS target_entity ON target_entity.id = r.target_id
         WHERE r.predicate = 'supersedes'
           AND (r.valid_from IS NULL OR datetime(r.valid_from) <= datetime(?))
           AND (r.valid_to IS NULL OR datetime(r.valid_to) > datetime(?))
           AND (r.valid_at IS NULL OR datetime(r.valid_at) <= datetime(?))
           AND (r.invalid_at IS NULL OR datetime(r.invalid_at) > datetime(?))
         ORDER BY r.source_id, r.target_id, r.id
        """,
        (owner_id, as_of, as_of, as_of, as_of),
    ).fetchall()
    edges = [
        {
            "id": row[0],
            "source_id": row[1],
            "target_id": row[2],
            "predicate": row[3],
            "created_at": row[4],
            "valid_from": row[5],
            "valid_to": row[6],
            "valid_at": row[7],
            "invalid_at": row[8],
        }
        for row in rows
    ]
    return edges, _sha256_json(
        {
            "owner_id": owner_id,
            "include_archived": include_archived,
            "edges": edges,
        }
    )


def _row_values(row: sqlite3.Row | tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(row[index] for index in range(len(_ENTITY_COLUMNS)))


def _entity_payload(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, object]:
    (
        entity_id,
        title,
        body,
        status,
        source_hash,
        source_metadata,
        created_at,
        updated_at,
        owner_id,
        scope,
        is_core,
        weight,
        context_id,
        memory_type,
    ) = _row_values(row)
    return {
        "id": entity_id,
        "title": title,
        "body": body,
        "status": status,
        "source_hash": source_hash,
        "source_metadata": source_metadata,
        "created_at": created_at,
        "updated_at": updated_at,
        "owner_id": owner_id,
        "scope": scope,
        "is_core": bool(is_core),
        "weight": weight,
        "context_id": context_id,
        "memory_type": memory_type,
    }


def _corpus_fingerprint(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    include_archived: bool,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {"owner_id": owner_id, "include_archived": include_archived},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\n")
    count = 0
    cursor = conn.execute(
        "SELECT "
        + ", ".join(_ENTITY_COLUMNS)
        + " FROM entities"
        + " WHERE "
        + _visible_predicate(include_archived=include_archived)
        + " ORDER BY id",
        (owner_id,),
    )
    while True:
        rows = cursor.fetchmany(_HASH_CHUNK_ROWS)
        if not rows:
            break
        for row in rows:
            digest.update(
                json.dumps(
                    list(_row_values(row)),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
            count += 1
    return digest.hexdigest(), count


def _provenance(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    include_archived: bool,
    as_of: str,
) -> SnapshotProvenance:
    schema_hash = _schema_hash(conn)
    corpus_hash, entity_count = _corpus_fingerprint(
        conn, owner_id=owner_id, include_archived=include_archived
    )
    supersedes_edges, relation_root_hash = _supersedes_edges(
        conn, owner_id=owner_id, include_archived=include_archived, as_of=as_of
    )
    # This is a logical database hash rather than a raw file hash.  It is stable across SQLite
    # WAL checkpoints and captures exactly the schema plus authoritative entity source rows that
    # this export promises to freeze.
    database_hash = _sha256_json(
        {
            "schema_hash": schema_hash,
            "corpus_hash": corpus_hash,
            "entity_count": entity_count,
            "owner_id": owner_id,
            "relation_root_hash": relation_root_hash,
        }
    )
    snapshot_hash = _sha256_json(
        {
            "database_hash": database_hash,
            "owner_id": owner_id,
            "relation_root_hash": relation_root_hash,
            "include_archived": include_archived,
        }
    )
    return SnapshotProvenance(
        schema_hash=schema_hash,
        corpus_hash=corpus_hash,
        database_hash=database_hash,
        snapshot_hash=snapshot_hash,
        entity_count=entity_count,
        include_archived=include_archived,
        owner_id=owner_id,
        relation_root_hash=relation_root_hash,
        relation_count=len(supersedes_edges),
    )


def _validate_page_request(
    page_size: int | None,
    cursor: str | None,
    snapshot_hash: str | None,
    include_archived: bool,
) -> tuple[int, str | None, str | None, bool]:
    if page_size is None:
        page_size = DEFAULT_PAGE_SIZE
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise CorpusSnapshotError("page_size must be an integer")
    if page_size <= 0 or page_size > MAX_PAGE_SIZE:
        raise CorpusSnapshotError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise CorpusSnapshotError("cursor must be a non-empty entity ID when supplied")
    if snapshot_hash is not None:
        if (
            not isinstance(snapshot_hash, str)
            or len(snapshot_hash) != 64
            or any(char not in "0123456789abcdef" for char in snapshot_hash)
        ):
            raise CorpusSnapshotError("snapshot_hash must be a lowercase SHA-256")
    if not isinstance(include_archived, bool):
        raise CorpusSnapshotError("include_archived must be boolean")
    return page_size, cursor, snapshot_hash, include_archived


def _page_result(
    rows: list[sqlite3.Row | tuple[Any, ...]],
    *,
    page_size: int,
    provenance: SnapshotProvenance,
    include_archived: bool,
    supersedes_edges: list[dict[str, object]],
) -> dict[str, object]:
    has_more = len(rows) > page_size
    page_rows = rows[:page_size]
    entities = [_entity_payload(row) for row in page_rows]
    next_cursor = entities[-1]["id"] if has_more and entities else None
    return {
        "entities": entities,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "page_size": page_size,
        "provenance": provenance.to_dict(),
        # Top-level hashes keep page consumers simple while the nested envelope remains
        # self-describing for artifact writers.
        "schema_hash": provenance.schema_hash,
        "database_hash": provenance.database_hash,
        "corpus_hash": provenance.corpus_hash,
        "snapshot_hash": provenance.snapshot_hash,
        "entity_count": provenance.entity_count,
        "include_archived": include_archived,
        "owner_id": provenance.owner_id,
        "supersedes_edges": supersedes_edges,
        "relations": supersedes_edges,
        "relation_root_hash": provenance.relation_root_hash,
        "relations_hash": provenance.relation_root_hash,
        "relation_count": provenance.relation_count,
    }


def export_corpus_snapshot_page(
    *,
    owner_id: str | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    snapshot_hash: str | None = None,
    include_archived: bool = False,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> dict[str, object]:
    """Return one deterministic corpus page bound to a stable snapshot hash.

    ``cursor`` is an exclusive entity-ID cursor, not an offset.  On the first call omit both
    ``cursor`` and ``snapshot_hash``.  Pass the returned ``next_cursor`` and ``snapshot_hash`` for
    every subsequent call.  A mismatch raises :class:`SnapshotChangedError` instead of silently
    mixing rows from different production states.
    """
    page_size, cursor, snapshot_hash, include_archived = _validate_page_request(
        page_size, cursor, snapshot_hash, include_archived
    )
    if not isinstance(owner_id, str) or not owner_id:
        raise CorpusSnapshotError("owner_id is mandatory for corpus snapshot export")
    with managed_connection(db_connection=db_connection, db_path=db_path) as conn:
        with _read_transaction(conn):
            _require_entity_columns(conn)
            as_of = datetime.now(UTC).isoformat()
            provenance = _provenance(
                conn, owner_id=owner_id, include_archived=include_archived, as_of=as_of
            )
            if snapshot_hash is not None and snapshot_hash != provenance.snapshot_hash:
                raise SnapshotChangedError(
                    "corpus snapshot changed since the previous page; restart the export"
                )
            rows = _entity_rows(
                conn,
                owner_id=owner_id,
                include_archived=include_archived,
                after_id=cursor,
                limit=page_size + 1,
            )
            supersedes_edges, relation_root_hash = _supersedes_edges(
                conn,
                owner_id=owner_id,
                include_archived=include_archived,
                as_of=as_of,
            )
            if relation_root_hash != provenance.relation_root_hash:
                raise SnapshotChangedError(
                    "supersedes relation snapshot changed while the page was being read"
                )
            return _page_result(
                rows,
                page_size=page_size,
                provenance=provenance,
                include_archived=include_archived,
                supersedes_edges=supersedes_edges,
            )


def iter_corpus_snapshot_pages(
    *,
    owner_id: str | None = None,
    page_size: int | None = None,
    include_archived: bool = False,
    db_connection: sqlite3.Connection | None = None,
    db_path: str | None = None,
) -> Iterator[dict[str, object]]:
    """Stream all pages while enforcing one immutable provenance across page calls."""
    page_size, _, _, include_archived = _validate_page_request(
        page_size, None, None, include_archived
    )
    if not isinstance(owner_id, str) or not owner_id:
        raise CorpusSnapshotError("owner_id is mandatory for corpus snapshot export")
    cursor: str | None = None
    with managed_connection(db_connection=db_connection, db_path=db_path) as conn:
        with _read_transaction(conn):
            _require_entity_columns(conn)
            as_of = datetime.now(UTC).isoformat()
            provenance = _provenance(
                conn, owner_id=owner_id, include_archived=include_archived, as_of=as_of
            )
            supersedes_edges, relation_root_hash = _supersedes_edges(
                conn,
                owner_id=owner_id,
                include_archived=include_archived,
                as_of=as_of,
            )
            if relation_root_hash != provenance.relation_root_hash:
                raise SnapshotChangedError(
                    "supersedes relation snapshot changed while export was starting"
                )
            while True:
                rows = _entity_rows(
                    conn,
                    owner_id=owner_id,
                    include_archived=include_archived,
                    after_id=cursor,
                    limit=page_size + 1,
                )
                page = _page_result(
                    rows,
                    page_size=page_size,
                    provenance=provenance,
                    include_archived=include_archived,
                    supersedes_edges=supersedes_edges,
                )
                yield page
                next_cursor = page["next_cursor"]
                if next_cursor is None:
                    return
                cursor = str(next_cursor)


__all__ = [
    "CorpusSnapshotError",
    "SnapshotChangedError",
    "SnapshotProvenance",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "export_corpus_snapshot_page",
    "iter_corpus_snapshot_pages",
]
