"""Reconstruct a run-private SQLite database, bound to the frozen live-corpus snapshot, so the
lexical (BM25) retrieval cell can run against exactly the same production FTS5 schema/triggers
used everywhere else in this codebase -- without ever opening SALTMDB's real database.

Why this exists
----------------
``run_retrieval_bakeoff.py``'s lexical cell (``execute_lexical_cell``) and
``lexical_adapter.bm25_search``/``include_current_heads`` run BM25 search through a supplied
``--db-path`` SQLite file, using production FTS5 query logic imported live from
``saltmdb.domain.services.memory_service._run_fts_search``.  That function expects the real
``entities``/``relations``/``entities_fts`` schema and triggers (``saltmdb.db.schema.init_db``).
This module builds exactly that schema, from scratch, in a throwaway file, and populates it
*only* from the frozen, already-signed ``corpus_export.json`` + ``corpus_representation_
manifest.json`` pair produced by ``freeze_live_corpus.py`` for one bakeoff run.

This module never opens SALTMDB's live database.  It never reads, writes, or connects to any
path under a real SALTMDB install -- its sole inputs are the two frozen JSON files named above,
and its sole output is a brand-new SQLite file at the caller-supplied ``--db-path``.

Flow
----
1. Load and cross-verify ``corpus_export.json`` against the signed ``corpus_representation_
   manifest.json`` via ``build_query_slots.load_export_bound_to_manifest`` (hash mismatch is a
   hard failure, not a silent skip -- reused rather than reimplemented here).
2. Refuse to run if ``--db-path`` already exists: this is a from-scratch reconstruction, not an
   incremental update, so a stale/foreign file at that path is refused rather than silently
   mutated. (No ``--overwrite`` escape hatch is offered -- callers that want a fresh file can
   just delete the old one themselves, which keeps this script's failure mode unambiguous.)
3. Call ``saltmdb.db.schema.init_db`` to obtain a connection carrying the exact production DDL,
   FTS5 virtual tables, and sync triggers (verified empirically, see "FTS population" below).
4. Insert one ``entities`` row per corpus-export entity, using a single fixed sentinel timestamp
   for ``created_at``/``updated_at``/``last_accessed_at`` and the production
   ``compute_content_hash`` helper for ``content_hash``, so the reconstructed rows follow the
   same conventions production writes use.
5. Insert one ``relations`` row per ``supersedes_edges`` entry, verbatim.
6. Return (and optionally write) a small receipt: entity/relation counts, the manifest's
   ``corpus_root_hash``, the db path, and an informational sha256 of the produced ``.db`` file.

FTS population
--------------
``saltmdb.db.schema.init_db`` creates ``insert_entity_fts``/``update_entity_fts`` triggers that
fire ``AFTER INSERT``/``AFTER UPDATE ON entities WHEN NEW.status != 'archived'``.  Since every
row this script inserts uses ``status = 'raw'``, a plain ``INSERT INTO entities (...)`` is
sufficient -- ``entities_fts`` is populated automatically by those triggers, with no separate FTS
insert step needed.  This was confirmed both by reading ``schema.py`` directly and empirically,
by running a BM25 query against a freshly built database in this module's own verification pass
(see the module's test suite and the manual dry-run recorded in the task report).

Sentinel timestamp
-------------------
``corpus_export.json`` entity rows carry no ``created_at``/``updated_at``/``last_accessed_at``
(those are runtime/session fields, not frozen corpus content), yet the production ``entities``
schema declares all three ``NOT NULL``.  Rather than stamping the real wall-clock time the
reconstruction script happened to run (which would make two reconstructions of the identical
frozen snapshot produce different database content, purely from a field with no signal value
here), every reconstructed row uses one fixed, documented sentinel: ``SENTINEL_TIMESTAMP``
below, an ISO-8601 instant matching this snapshot's run id (``accuracy-bakeoff-20260812``). This
keeps every row's timestamp columns a constant, recognizable marker of "reconstructed corpus
content," never mistaken for a real write time.

DB-file hash is informational only
-----------------------------------
The receipt includes a sha256 of the resulting ``.db`` file's bytes. SQLite does not guarantee
byte-for-byte reproducibility across runs/versions in general (page layout, freelist state, and
vacuum history can differ even when logical row content is identical, and this module does not
force a ``VACUUM`` or otherwise pin SQLite's on-disk layout). Nothing elsewhere in this codebase
treats a database file's own hash as an invariant either (unlike ``corpus_root_hash``, entity
``content_hash``, or artifact ``artifact_fingerprint``, all of which are hashes of well-defined
JSON/text, not of a stateful file format). This hash is therefore recorded for local
before/after diffing convenience only -- it is not a custody guarantee, and downstream code must
never assert equality against it across independent runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "src"))

from bakeoff_state import sign_artifact  # noqa: E402
from build_query_slots import load_export_bound_to_manifest  # noqa: E402
from saltmdb.db.connection import close_connection, write_transaction_retrying  # noqa: E402
from saltmdb.db.schema import init_db  # noqa: E402
from saltmdb.utils.text import compute_content_hash  # noqa: E402

# Matches this snapshot's run id (accuracy-bakeoff-20260812); see the module docstring's
# "Sentinel timestamp" section for why a fixed constant is used instead of wall-clock time.
SENTINEL_TIMESTAMP = "2026-08-12T00:00:00+00:00"

# Used only when corpus_export.json's snapshot_provenance carries no owner_id.
SENTINEL_OWNER_ID = "unknown-snapshot-owner"

_HASH_CHUNK_BYTES = 1024 * 1024


class LexicalSnapshotDbError(ValueError):
    """The frozen export/manifest cannot be reconstructed into a lexical snapshot database."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _insert_entities(
    conn: Any,
    export_entities: list[Mapping[str, Any]],
    entities: Mapping[str, Mapping[str, str]],
    owner_id: str,
) -> int:
    count = 0
    for row in export_entities:
        entity_id = row["entity_id"]
        verified = entities[entity_id]
        title = verified["title"]
        body = verified["body"]
        # Mirrors freeze_live_corpus.py's own chunking fallback rule: an entity with an empty
        # body falls back to its title so full_content is never NOT NULL-violating empty text.
        full_content = body if body else title
        content_hash = compute_content_hash(full_content)
        conn.execute(
            "INSERT INTO entities "
            "(id, created_at, updated_at, last_accessed_at, owner_id, scope, status, "
            " title, full_content, content_hash) "
            "VALUES (?, ?, ?, ?, ?, 'shared', 'raw', ?, ?, ?)",
            (
                entity_id,
                SENTINEL_TIMESTAMP,
                SENTINEL_TIMESTAMP,
                SENTINEL_TIMESTAMP,
                owner_id,
                title,
                full_content,
                content_hash,
            ),
        )
        count += 1
    return count


def _insert_relations(conn: Any, supersedes_edges: list[Mapping[str, Any]]) -> int:
    count = 0
    for edge in supersedes_edges:
        if edge.get("predicate") != "supersedes":
            raise LexicalSnapshotDbError(
                f"corpus export supersedes_edges contains a non-supersedes predicate: {edge.get('predicate')!r}"
            )
        conn.execute(
            "INSERT INTO relations "
            "(id, source_id, target_id, predicate, created_at, valid_from, valid_to, valid_at, invalid_at) "
            "VALUES (?, ?, ?, 'supersedes', ?, ?, ?, ?, ?)",
            (
                edge["id"],
                edge["source_id"],
                edge["target_id"],
                edge["created_at"],
                edge["valid_from"],
                edge["valid_to"],
                edge["valid_at"],
                edge["invalid_at"],
            ),
        )
        count += 1
    return count


def build_snapshot_db(export_path: Path, manifest_path: Path, db_path: Path) -> dict[str, Any]:
    """Deterministically reconstruct a run-private lexical SQLite db from a frozen corpus export.

    Cross-verifies every entity against the signed manifest (via
    ``load_export_bound_to_manifest``), refuses to run if ``db_path`` already exists, then builds
    the database using the exact production ``init_db`` DDL/triggers. Returns a signed
    ``LexicalSnapshotReceipt`` artifact recording entity/relation counts, the bound
    ``corpus_root_hash``, the db path, and an informational db-file sha256.
    """
    if db_path.exists():
        raise LexicalSnapshotDbError(
            f"{db_path} already exists -- refusing to overwrite; this is a from-scratch "
            "reconstruction, not an incremental update. Delete the existing file first."
        )

    entities, corpus_root_hash = load_export_bound_to_manifest(export_path, manifest_path)
    export = json.loads(export_path.read_text(encoding="utf-8"))
    export_entities = export.get("entities")
    if not isinstance(export_entities, list) or not export_entities:
        raise LexicalSnapshotDbError("corpus export entities must be a non-empty list")
    supersedes_edges = export.get("supersedes_edges") or []
    provenance = export.get("snapshot_provenance") or {}
    owner_id = provenance.get("owner_id") or SENTINEL_OWNER_ID

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = init_db(str(db_path))
    try:
        counts: dict[str, int] = {}

        def _write(c: Any) -> None:
            counts["entities"] = _insert_entities(c, export_entities, entities, owner_id)
            counts["relations"] = _insert_relations(c, supersedes_edges)

        write_transaction_retrying(conn, _write)
    finally:
        close_connection(conn)

    receipt_payload = {
        "corpus_root_hash": corpus_root_hash,
        "db_path": str(db_path),
        "entity_count": counts["entities"],
        "relation_count": counts["relations"],
        "sentinel_timestamp": SENTINEL_TIMESTAMP,
        "owner_id": owner_id,
        "db_sha256_informational": _sha256_file(db_path),
    }
    return sign_artifact("LexicalSnapshotReceipt", receipt_payload)


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True, help="Frozen corpus_export.json")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="Signed corpus_representation_manifest.json"
    )
    parser.add_argument(
        "--db-path", type=Path, required=True, help="Output path for the new lexical snapshot db"
    )
    parser.add_argument(
        "--receipt-out",
        type=Path,
        help="Optional path to write the signed LexicalSnapshotReceipt JSON",
    )
    args = parser.parse_args(argv)

    receipt = build_snapshot_db(args.export, args.manifest, args.db_path)
    if args.receipt_out:
        _atomic_write(args.receipt_out, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
