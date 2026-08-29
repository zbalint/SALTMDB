import unittest
import tempfile
import os
import shutil
import sqlite3
import time
import uuid
import json
import re
from datetime import datetime, UTC
from unittest.mock import patch

import sqlite_vec

from saltmdb.db.schema import init_db
from saltmdb.db.connection import write_transaction_retrying
from saltmdb.domain.services.relation_service import (
    resolve_or_create_predicate,
    list_predicates,
    store_relation,
    invalidate_relation,
    bulk_store_relations,
    analyze_dependencies,
    analyze_lineage,
    get_lineage,
    get_related_memories,
    commit_consolidation,
    consolidate_memories,
    bulk_commit_consolidation,
)
from saltmdb.domain.services.memory_service import (
    store_memory,
    detect_orphaned_memories,
    search_tags,
)
from saltmdb.domain.services.cohesion_service import get_fresh_entity_centroids

DIM = 384


def _axis_vector(index: int, dim: int = DIM) -> list:
    """Unit basis vector -- cosine(axis_vector(i), axis_vector(j)) is exactly 1.0 if i == j,
    else exactly 0.0 (orthogonal). Mirrors tests/test_topic_rerank.py's helper of the same
    name/contract."""
    v = [0.0] * dim
    v[index] = 1.0
    return v


def _cons_content(marker: str) -> str:
    """Short markdown content that reliably passes commit_consolidation's quality gate
    (mirrors the proven-passing pattern used in tests/test_consolidation_quality.py) while
    staying unique per call site so Stage A exact-hash-collision checks never false-positive
    across sibling calls in the same test."""
    return (
        f"# Consolidated Record {marker}\n\n"
        f"Synthesized summary combining source facts for the {marker} testing scenario.\n"
        "- Merged detail alpha\n"
        "- Merged detail beta"
    )


def _memory_id(result) -> str:
    if isinstance(result, dict):
        return result["data"]["id"]
    match = re.search(r"ID:\s*([a-f0-9-]+)", result)
    assert match, f"Could not parse entity ID from result: {result!r}"
    return match.group(1)


class TestRelationsUniqueIndexSchema(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unique_index_exists_after_init_db(self):
        rows = self.conn.execute("PRAGMA index_list(relations)").fetchall()
        matching = [r for r in rows if r[1] == "idx_relations_unique_edge"]
        self.assertEqual(
            len(matching), 1, "idx_relations_unique_edge must exist on relations after init_db()"
        )
        self.assertEqual(matching[0][2], 1, "idx_relations_unique_edge must be a UNIQUE index")

        idx_info = self.conn.execute("PRAGMA index_info(idx_relations_unique_edge)").fetchall()
        cols = [r[2] for r in idx_info]
        self.assertEqual(cols, ["source_id", "target_id", "predicate"])

    def test_index_sql_has_partial_where_valid_to_is_null_clause(self):
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_relations_unique_edge'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("WHERE valid_to IS NULL", row[0])

    def _mk_entity(self, title):
        # relations.source_id/target_id carry FK constraints to entities(id), and this class's
        # connection (via init_db -> get_connection) runs with PRAGMA foreign_keys=ON, so manual
        # relation inserts need real entity rows behind them, not arbitrary string literals.
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id="idx_tester",
            db_connection=self.conn,
        )
        return _memory_id(res)

    def test_expired_and_active_row_for_identical_tuple_both_insert(self):
        src = self._mk_entity("Partial Idx Src")
        tgt = self._mk_entity("Partial Idx Tgt")
        past = "2020-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-partial', ?)",
            (str(uuid.uuid4()), src, tgt, past),
        )
        # An active row (valid_to NULL) for the SAME tuple must also succeed under the partial index.
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-partial', NULL)",
            (str(uuid.uuid4()), src, tgt),
        )
        count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=? AND predicate='pred-partial'",
            (src, tgt),
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_two_active_rows_for_identical_tuple_raises_integrity_error(self):
        src = self._mk_entity("Active Idx Src")
        tgt = self._mk_entity("Active Idx Tgt")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-active', NULL)",
            (str(uuid.uuid4()), src, tgt),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-active', NULL)",
                (str(uuid.uuid4()), src, tgt),
            )

    def test_calling_init_db_twice_does_not_raise(self):
        conn2 = init_db(self.db_path)
        try:
            row = conn2.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_relations_unique_edge'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("WHERE valid_to IS NULL", row[0])
        finally:
            conn2.close()


class TestRelationsPartialIndexMigration(unittest.TestCase):
    def test_old_non_partial_index_migrates_to_partial_form_on_init_db(self):
        temp_dir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(temp_dir, "test.db")

            # Mirror how TestRelationsDedupBackfill sets up raw pre-existing state: bypass
            # schema.init_db entirely and hand-create the OLD (pre-alpha.57) non-partial
            # unique index definition on an empty relations table.
            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("""
                CREATE TABLE relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_from DATETIME,
                    valid_to DATETIME
                );
            """)
            raw_conn.execute(
                "CREATE UNIQUE INDEX idx_relations_unique_edge ON relations(source_id, target_id, predicate)"
            )
            raw_conn.commit()
            raw_conn.close()

            conn = init_db(db_path)
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_relations_unique_edge'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertIn(
                    "WHERE valid_to IS NULL",
                    row[0],
                    "init_db() must unconditionally DROP+CREATE the old non-partial index into the partial form",
                )
            finally:
                conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestRelationsDedupBackfill(unittest.TestCase):
    def test_dedup_backfill_keeps_earliest_row_and_index_is_enforced(self):
        temp_dir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(temp_dir, "test.db")

            # Bypass schema.init_db entirely: create a minimal pre-existing 'relations' table
            # (no unique index yet) and insert 3 duplicate (source_id, target_id, predicate)
            # rows in a known insertion order, using a raw sqlite3 connection.
            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("""
                CREATE TABLE relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_from DATETIME,
                    valid_to DATETIME
                );
            """)
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                ("rel-earliest",),
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                ("rel-middle",),
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                ("rel-latest",),
            )
            raw_conn.commit()
            raw_conn.close()

            # Now run the real init_db() against that same file -- this must run the dedup
            # backfill DELETE and then successfully create the unique index.
            conn = init_db(db_path)
            try:
                rows = conn.execute(
                    "SELECT id FROM relations WHERE source_id = 'src-x' AND target_id = 'tgt-x' AND predicate = 'dup_pred'"
                ).fetchall()
                self.assertEqual(
                    len(rows),
                    1,
                    "dedup backfill must collapse all duplicate (source_id, target_id, predicate) rows to exactly one",
                )
                self.assertEqual(
                    rows[0][0],
                    "rel-earliest",
                    "the surviving row must be the earliest-inserted one (by rowid), not an arbitrary one",
                )

                # The unique index must now genuinely be enforced against fresh duplicate inserts.
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                        (str(uuid.uuid4()),),
                    )
            finally:
                conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_dedup_backfill_never_touches_closed_rows(self):
        """Agent API redesign Phase 8 fix: the dedup backfill exists solely to protect the
        PARTIAL unique index (WHERE valid_to IS NULL), so it must never delete a closed
        (valid_to IS NOT NULL) row, even when it shares an identical (source_id, target_id,
        predicate) triple with another closed row or with an active one. Before this fix, an
        unscoped DELETE grouped by the triple alone would silently drop one of a legitimate
        {active, closed} pair -- exactly what §7.1's predicate-vocabulary migration produces
        for a collision-losing alias row that gets renamed+closed onto the active winner's
        triple (see tests/test_predicate_migration.py for the full migration-level regression)."""
        temp_dir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(temp_dir, "test.db")
            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("""
                CREATE TABLE relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_from DATETIME,
                    valid_to DATETIME
                );
            """)
            # An active row and a closed row sharing the identical triple -- a legitimate
            # "current fact" plus "historical record of the same fact" pair, not a duplicate.
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) "
                "VALUES ('rel-active', 'src-y', 'tgt-y', 'dup_pred', NULL)"
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) "
                "VALUES ('rel-closed', 'src-y', 'tgt-y', 'dup_pred', '2020-01-01T00:00:00+00:00')"
            )
            # Two closed rows sharing an identical triple from different points in time.
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) "
                "VALUES ('rel-closed-1', 'src-z', 'tgt-z', 'dup_pred', '2019-01-01T00:00:00+00:00')"
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) "
                "VALUES ('rel-closed-2', 'src-z', 'tgt-z', 'dup_pred', '2021-01-01T00:00:00+00:00')"
            )
            raw_conn.commit()
            raw_conn.close()

            conn = init_db(db_path)
            try:
                surviving_ids = {
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM relations WHERE id LIKE 'rel-%'"
                    ).fetchall()
                }
                self.assertEqual(
                    surviving_ids,
                    {"rel-active", "rel-closed", "rel-closed-1", "rel-closed-2"},
                    "no closed row may ever be dropped by the dedup backfill",
                )
            finally:
                conn.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestStoreRelationDedup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

        res1 = store_memory(
            content="Source entity content for relation dedup tests",
            title="Relation Dedup Source",
            owner_id="tester",
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for relation dedup tests",
            title="Relation Dedup Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        res3 = store_memory(
            content="Second target entity content for relation repoint tests",
            title="Relation Dedup Repoint Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        self.id1 = _memory_id(res1)
        self.id2 = _memory_id(res2)
        self.id3 = _memory_id(res3)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _relation_row(self, source_id, target_id, predicate):
        return self.conn.execute(
            "SELECT id, valid_to, invalid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
            (source_id, target_id, predicate),
        ).fetchone()

    def _mk_vector_entity(self, title: str, vector: list, status: str = "raw") -> str:
        """Same pattern as TestCommitConsolidationCohesionGate._mk_vector_entity: a bare
        entities row plus a single matching entity_chunk_embeddings row, bypassing
        store_memory's async chunk-embed trigger so this test controls the centroid directly
        (needed to force a deterministic, real -- not unresolved-centroid -- gate rejection)."""
        entity_id = str(uuid.uuid4())
        content_hash = f"hash-{entity_id}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'tester', ?, ?, ?, ?)",
            (entity_id, now, now, now, status, title, f"content body for {title}", content_hash),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, ?)",
            (f"{entity_id}::0", entity_id, sqlite_vec.serialize_float32(vector), content_hash),
        )
        self.conn.commit()
        return entity_id

    def _relation_count(self, source_id, target_id, predicate=None):
        if predicate:
            return self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
                (source_id, target_id, predicate),
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        ).fetchone()[0]

    def test_duplicate_call_is_noop_and_reports_same_existing_id(self):
        res1 = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", res1)
        self.assertFalse(res1.startswith("Error"))
        id_in_res1 = _memory_id(res1)

        res2 = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertIn("already exists", res2)
        self.assertFalse(res2.startswith("Error"))
        id_in_res2 = _memory_id(res2)

        self.assertEqual(
            id_in_res1,
            id_in_res2,
            "the dup no-op must report the SAME existing relation id as the original insert",
        )
        self.assertEqual(self._relation_count(self.id1, self.id2, "related_to"), 1)

    def test_same_pair_different_predicates_both_persist(self):
        store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )
        store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertEqual(
            self._relation_count(self.id1, self.id2),
            2,
            "two different predicates between the same source/target pair must NOT be deduped against each other",
        )

    def test_bulk_store_relations_marks_duplicate_status(self):
        store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )

        results = bulk_store_relations(
            relations=[{"source_id": self.id1, "target_id": self.id2, "predicate": "related_to"}],
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "duplicate")
        self.assertEqual(self._relation_count(self.id1, self.id2, "related_to"), 1)

    def test_bulk_store_relations_invalidate_per_item(self):
        store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )

        results = bulk_store_relations(
            relations=[
                {
                    "source_id": self.id1,
                    "target_id": self.id2,
                    "predicate": "related_to",
                    "invalidate": True,
                }
            ],
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(results[0]["action"], "invalidate")

        row = self._relation_row(self.id1, self.id2, "related_to")
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])

    def test_bulk_store_relations_invalidate_batch_default(self):
        store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )
        store_relation(
            source_id=self.id1,
            target_id=self.id3,
            predicate="part_of",
            db_connection=self.conn,
        )

        results = bulk_store_relations(
            relations=[
                {"source_id": self.id1, "target_id": self.id2, "predicate": "related_to"},
                {"source_id": self.id1, "target_id": self.id3, "predicate": "part_of"},
            ],
            invalidate=True,
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["status"] == "success" for item in results), results)
        self.assertTrue(all(item["action"] == "invalidate" for item in results), results)

    def test_bulk_store_relations_invalidate_not_found_aborts_batch(self):
        results = bulk_store_relations(
            relations=[
                {"source_id": self.id1, "target_id": self.id2, "predicate": "related_to"},
                {
                    "source_id": self.id2,
                    "target_id": self.id3,
                    "predicate": "part_of",
                    "invalidate": True,
                },
            ],
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("relation not found", results[0]["error"])
        self.assertIsNone(
            self._relation_row(self.id1, self.id2, "related_to"),
            "a failing invalidate must roll back the earlier create",
        )

    def test_bulk_store_relations_rejects_aliased_predicate_and_reports_error(self):
        # Phase 6 write-time gate (plan §5.8): store_relation now rejects a drifted alias
        # spelling outright instead of silently canonicalizing it, and bulk_store_relations
        # aborts the whole batch (all-or-nothing) on any "Error"-prefixed per-item result.
        results = bulk_store_relations(
            relations=[{"source_id": self.id1, "target_id": self.id2, "predicate": "references"}],
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("NONCANONICAL_PREDICATE", results[0]["error"])
        self.assertEqual(
            self._relation_count(self.id1, self.id2),
            0,
            "a rejected bulk item must leave no relation stored",
        )

    def test_store_relation_already_exists_message_references_active_row_not_expired_one(self):
        # Manually plant an EXPIRED row for the tuple first (stale/historical data).
        expired_id = str(uuid.uuid4())
        past = "2020-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_to) "
            "VALUES (?, ?, ?, 'depends_on', ?, ?, ?)",
            (expired_id, self.id1, self.id2, past, past, past),
        )

        active_res = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertIn("successfully stored", active_res)
        active_id = _memory_id(active_res)
        self.assertNotEqual(active_id, expired_id)

        dup_res = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertIn("already exists", dup_res)
        reported_id = _memory_id(dup_res)
        self.assertEqual(
            reported_id,
            active_id,
            "the already-exists no-op message must reference the ACTIVE row's ID, not the expired one",
        )
        self.assertNotEqual(reported_id, expired_id)

    def test_repoint_same_source_predicate_different_target_invalidates_old_edge(self):
        # Cold-start agent-experience review, Issue C: manually repointing a
        # RELATION_GATE_STRONG_PREDICATES edge (same source + predicate, new target) must
        # invalidate the old target's edge, not leave it as permanent duplicate graph debt.
        first = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="elaborates_on",
            override_justification="unresolved-centroid test fixtures, deliberate override for coverage",
            db_connection=self.conn,
        )
        self.assertTrue(first.startswith("Relation successfully stored"), first)

        repoint = store_relation(
            source_id=self.id1,
            target_id=self.id3,
            predicate="elaborates_on",
            override_justification="unresolved-centroid test fixtures, deliberate override for coverage",
            db_connection=self.conn,
        )
        self.assertTrue(repoint.startswith("Relation successfully stored"), repoint)

        old_row = self._relation_row(self.id1, self.id2, "elaborates_on")
        new_row = self._relation_row(self.id1, self.id3, "elaborates_on")
        self.assertIsNotNone(old_row)
        self.assertIsNotNone(new_row)
        self.assertIsNotNone(old_row[1], "old edge's valid_to must be set (invalidated)")
        self.assertIsNotNone(old_row[2], "old edge's invalid_at must be set (invalidated)")
        self.assertIsNone(new_row[1], "new edge must remain active (valid_to IS NULL)")
        self.assertIsNone(new_row[2], "new edge must remain active (invalid_at IS NULL)")

    def test_repoint_not_auto_invalidated_for_many_to_many_scoped_predicate(self):
        # related_to is NOT in RELATION_GATE_STRONG_PREDICATES -- it's legitimately many-to-many,
        # so a second same-source/same-predicate edge to a different target must NOT invalidate
        # the first; both are independently valid relations, not a repoint.
        first = store_relation(
            source_id=self.id1,
            target_id=self.id2,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertTrue(first.startswith("Relation successfully stored"), first)

        second = store_relation(
            source_id=self.id1,
            target_id=self.id3,
            predicate="related_to",
            db_connection=self.conn,
        )
        self.assertTrue(second.startswith("Relation successfully stored"), second)

        row1 = self._relation_row(self.id1, self.id2, "related_to")
        row2 = self._relation_row(self.id1, self.id3, "related_to")
        self.assertIsNone(row1[1], "many-to-many predicate: first edge must remain active")
        self.assertIsNone(row2[1], "many-to-many predicate: second edge must remain active")

    def test_repoint_rejected_by_gate_leaves_old_edge_untouched(self):
        # Regression test for the data-loss ordering bug caught during adversarial plan review:
        # if the new edge's gate check rejects, the OLD edge must be left completely untouched --
        # the invalidate step must never fire before every gate has already passed. Uses
        # controlled axis-aligned vectors (not the plain setUp entities) so the rejection is a
        # real, deterministic REJECT_LOW_RELATION_SIMILARITY rather than relying on incidental
        # embedding-timing behavior of plain store_memory-created entities.
        source = self._mk_vector_entity("Repoint Gate Source", _axis_vector(0))
        old_target = self._mk_vector_entity("Repoint Gate Old Target", _axis_vector(0))  # sim=1.0
        new_target = self._mk_vector_entity(
            "Repoint Gate New Target", _axis_vector(1)
        )  # orthogonal -> sim=0.0

        first = store_relation(
            source_id=source,
            target_id=old_target,
            predicate="elaborates_on",
            db_connection=self.conn,
        )
        self.assertTrue(first.startswith("Relation successfully stored"), first)

        rejected = store_relation(
            source_id=source,
            target_id=new_target,
            predicate="elaborates_on",
            db_connection=self.conn,  # no override_justification -> gate must reject
        )
        self.assertTrue(rejected.startswith("Error: REJECT_LOW_RELATION_SIMILARITY"), rejected)

        old_row = self._relation_row(source, old_target, "elaborates_on")
        new_row = self._relation_row(source, new_target, "elaborates_on")
        self.assertIsNotNone(old_row)
        self.assertIsNone(old_row[1], "rejected repoint must NOT invalidate the old edge")
        self.assertIsNone(old_row[2], "rejected repoint must NOT invalidate the old edge")
        self.assertIsNone(new_row, "rejected repoint must not create a new edge either")


class TestResolveOrCreatePredicate(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _resolve(self, predicate_name, agent_id=None):
        # resolve_or_create_predicate's contract requires an open write transaction around it
        # (mirrors how store_relation actually invokes it).
        def _write(c):
            return resolve_or_create_predicate(c, predicate_name, agent_id=agent_id)

        return write_transaction_retrying(self.conn, _write)

    def test_all_seed_predicates_exist_after_init_db(self):
        rows = self.conn.execute("SELECT name FROM predicates").fetchall()
        names = {r[0] for r in rows}
        expected = {
            "resolves",
            "depends_on",
            "references",
            "elaborates_on",
            "consolidated_from",
            "supersedes",
            "relates_to",
        }
        self.assertTrue(expected.issubset(names))

    def test_relates_to_and_references_alias_to_related_to(self):
        # Phase 6 reversed-behavior regression (plan §3.17/§5.8): relates_to/references now
        # alias onto related_to, NOT elaborates_on (their pre-Phase-6 target).
        self.assertEqual(self._resolve("relates_to"), "related_to")
        self.assertEqual(self._resolve("references"), "related_to")

        row_relates_to = self.conn.execute(
            "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id WHERE p.name = 'relates_to'"
        ).fetchone()
        self.assertIsNotNone(row_relates_to)
        self.assertEqual(row_relates_to[0], "related_to")

        row_references = self.conn.execute(
            "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id WHERE p.name = 'references'"
        ).fetchone()
        self.assertIsNotNone(row_references)
        self.assertEqual(row_references[0], "related_to")

    def test_repeated_calls_on_unrecognized_predicate_both_return_none_and_create_nothing(self):
        # INVERTED CONTRACT (Phase 6): resolve_or_create_predicate no longer creates predicate
        # rows at all -- an unrecognized name resolves to None on every call, not just the first.
        first = self._resolve("brand_new_predicate_idem")
        second = self._resolve("brand_new_predicate_idem")
        self.assertIsNone(first)
        self.assertIsNone(second)

        rows = self.conn.execute(
            "SELECT id FROM predicates WHERE name = 'brand_new_predicate_idem'"
        ).fetchall()
        self.assertEqual(
            len(rows),
            0,
            "resolve_or_create_predicate must never insert a predicate row under the closed "
            "vocabulary's inverted (read-only) contract",
        )

    def test_alias_input_returns_canonical_name_not_alias_name(self):
        resolved = self._resolve("relates_to")
        self.assertEqual(resolved, "related_to")
        self.assertNotEqual(resolved, "relates_to")

    def test_normalization_dash_and_space_variants_resolve_to_depends_on(self):
        self.assertEqual(self._resolve("Depends-On"), "depends_on")
        self.assertEqual(self._resolve("depends on"), "depends_on")

    def test_normalized_name_fallback_preserves_original_row_name(self):
        # Manually insert a dirty legacy row whose 'name' column was never normalized to
        # snake_case, only its 'normalized_name' column was.
        def _write(c):
            c.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, 'Old Legacy Name', 'old_legacy_name', NULL)",
                (str(uuid.uuid4()),),
            )

        write_transaction_retrying(self.conn, _write)

        resolved = self._resolve("OLD LEGACY NAME")
        self.assertEqual(
            resolved,
            "Old Legacy Name",
            "normalized_name fallback must return the row's ORIGINAL name string unchanged, not silently rename it",
        )

    def test_unrecognized_predicate_resolves_to_none_and_is_not_created(self):
        # INVERTED CONTRACT (Phase 6): a name outside the closed 51-name universe and absent
        # from the predicates table resolves to None -- it is never auto-created.
        resolved = self._resolve("totally_new_predicate_xyz")
        self.assertIsNone(resolved)

        row = self.conn.execute(
            "SELECT id FROM predicates WHERE name = 'totally_new_predicate_xyz'"
        ).fetchone()
        self.assertIsNone(
            row, "resolve_or_create_predicate must never create a row for an unrecognized name"
        )

    def test_empty_or_punctuation_only_input_returns_none(self):
        self.assertIsNone(self._resolve("   "))
        self.assertIsNone(self._resolve("!!!"))

    def test_store_relation_with_degenerate_predicate_is_rejected_as_unknown(self):
        # Phase 6 write-time gate (plan §5.8): a predicate that normalizes to empty (e.g.
        # '!!!') is now classified "unknown" and rejected outright, never stored raw.
        res1 = store_memory(
            content="Source entity content for degenerate predicate test",
            title="Degenerate Predicate Source",
            owner_id="tester",
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for degenerate predicate test",
            title="Degenerate Predicate Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        id1 = _memory_id(res1)
        id2 = _memory_id(res2)

        result = store_relation(
            source_id=id1, target_id=id2, predicate="!!!", db_connection=self.conn
        )
        self.assertTrue(result.startswith("Error: UNKNOWN_PREDICATE"))

        row = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2)
        ).fetchone()
        self.assertIsNone(
            row, "a rejected write-time-gated predicate must leave no relation row stored"
        )

    def test_store_relation_rejects_seeded_alias_predicate_naming_canonical_form(self):
        # Phase 6 write-time gate (plan §5.8): store_relation no longer silently substitutes a
        # drifted alias spelling -- it rejects the call and names the canonical replacement,
        # matching the manage_relation adapter's own pre-flight gate (mcp/tools.py).
        res1 = store_memory(
            content="Source entity content for alias surfacing test",
            title="Alias Surfacing Source",
            owner_id="tester",
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for alias surfacing test",
            title="Alias Surfacing Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        id1 = _memory_id(res1)
        id2 = _memory_id(res2)

        result = store_relation(
            source_id=id1, target_id=id2, predicate="relates_to", db_connection=self.conn
        )
        self.assertTrue(result.startswith("Error: NONCANONICAL_PREDICATE"))
        self.assertIn(
            "related_to",
            result,
            "the canonical replacement name must still be surfaced so the caller knows how to retry",
        )
        self.assertIn("relates_to", result)

        row = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2)
        ).fetchone()
        self.assertIsNone(row, "a rejected alias submission must leave no relation row stored")

    def test_non_aliased_predicate_normalization_does_not_add_canonicalization_note(self):
        res1 = store_memory(
            content="Source entity content for non-aliased normalization test",
            title="Non-Aliased Normalization Source",
            owner_id="tester",
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for non-aliased normalization test",
            title="Non-Aliased Normalization Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        id1 = _memory_id(res1)
        id2 = _memory_id(res2)

        result = store_relation(
            source_id=id1, target_id=id2, predicate="Depends-On", db_connection=self.conn
        )
        self.assertNotIn(
            "canonicalized",
            result,
            "pure case/format normalization must not be reported as a canonicalization note",
        )

    def test_invalidate_relation_degenerate_predicate_does_not_add_canonicalization_note(self):
        # invalidate_relation is READ-side only (never write-time-gated, per manage_relation's
        # own contract: "this gate applies only to creating a new edge, never to
        # invalidate=True") -- so a degenerate predicate '!!!' can still be looked up and
        # invalidated on an existing row even though store_relation could no longer create one.
        # The row is planted directly via SQL (store_relation would now reject '!!!' outright).
        res1 = store_memory(
            content="Source entity content for invalidate degenerate predicate test",
            title="Invalidate Degenerate Source",
            owner_id="tester",
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for invalidate degenerate predicate test",
            title="Invalidate Degenerate Target",
            owner_id="tester",
            db_connection=self.conn,
        )
        id1 = _memory_id(res1)
        id2 = _memory_id(res2)

        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES (?, ?, ?, '!!!', ?, ?, ?)",
            (str(uuid.uuid4()), id1, id2, now, now, now),
        )

        result = invalidate_relation(
            source_id=id1, target_id=id2, predicate="!!!", db_connection=self.conn
        )
        self.assertIn("Relation invalidated", result)
        self.assertNotIn(
            "canonicalized",
            result,
            "a degenerate predicate that falls back to the raw input on both sides must NOT be "
            "reported as canonicalized",
        )


class TestListPredicates(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_db_excludes_aliased_predicates(self):
        results = list_predicates(db_connection=self.conn)
        names = {r["name"] for r in results}
        self.assertEqual(
            names,
            {
                "elaborates_on",
                "related_to",
                "resolves",
                "depends_on",
                "verifies",
                "corrects",
                "caused_by",
                "derived_from",
                "distinguishes_from",
                "part_of",
                "contradicts",
                "supersedes",
                "consolidated_from",
                "revises",
                "similar_to",
            },
            "relates_to/references must be excluded from canonical predicates since they alias "
            "related_to; the closed universe's 15 canonical names (11 agent-selectable + 3 "
            "reserved + 1 legacy-read-only) must all be present with no aliases mixed in",
        )

    def test_query_filters_to_matching_predicate(self):
        results = list_predicates(query="depend", db_connection=self.conn)
        names = {r["name"] for r in results}
        self.assertEqual(names, {"depends_on"})

    def test_limit_bounds_result_count(self):
        def _write(c):
            for i in range(60):
                c.execute(
                    "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                    (str(uuid.uuid4()), f"seeded_predicate_{i}", f"seeded_predicate_{i}"),
                )

        write_transaction_retrying(self.conn, _write)

        results = list_predicates(db_connection=self.conn)
        self.assertEqual(
            len(results), 50, "default limit must cap unfiltered results at 50, not return all rows"
        )

    def test_explicit_limit_overrides_default(self):
        def _write(c):
            for i in range(20):
                c.execute(
                    "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                    (
                        str(uuid.uuid4()),
                        f"explicit_limit_predicate_{i}",
                        f"explicit_limit_predicate_{i}",
                    ),
                )

        write_transaction_retrying(self.conn, _write)

        results = list_predicates(limit=5, db_connection=self.conn)
        self.assertEqual(len(results), 5)


class TestSearchTags(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_limit_is_fifty(self):
        def _write(c):
            for i in range(60):
                c.execute(
                    "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                    (str(uuid.uuid4()), f"#seeded_tag_{i}", f"seededtag{i}"),
                )

        write_transaction_retrying(self.conn, _write)

        results = search_tags(db_connection=self.conn)
        self.assertEqual(
            len(results),
            50,
            "default limit must cap unfiltered tag results at 50, not return all rows",
        )

    def test_explicit_limit_overrides_default(self):
        def _write(c):
            for i in range(20):
                c.execute(
                    "INSERT INTO tags (id, name, normalized_name, canonical_id) VALUES (?, ?, ?, NULL)",
                    (str(uuid.uuid4()), f"#explicit_limit_tag_{i}", f"explicitlimittag{i}"),
                )

        write_transaction_retrying(self.conn, _write)

        results = search_tags(limit=5, db_connection=self.conn)
        self.assertEqual(len(results), 5)


class TestCommitConsolidationRepointing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk(self, title, owner_id="agent_c"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            db_connection=self.conn,
        )
        return _memory_id(res)

    def _active_relations(self, source_id=None, target_id=None, predicate=None):
        clauses, params = ["valid_to IS NULL"], []
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        if predicate:
            clauses.append("predicate = ?")
            params.append(predicate)
        return self.conn.execute(
            f"SELECT id, source_id, target_id, predicate FROM relations WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()

    def test_single_parent_rejects_before_relation_mutation(self):
        p1 = self._mk("Repoint P1")
        x = self._mk("Repoint X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)

        orig_row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'",
            (p1, x),
        ).fetchone()
        self.assertIsNotNone(orig_row)
        orig_id = orig_row[0]
        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

        res = commit_consolidation(
            parent_ids=[p1],
            title="C Basic Repoint",
            content=_cons_content("basic-repoint"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("REJECT_PARENT_COUNT", res)

        row = self.conn.execute(
            "SELECT source_id, valid_to FROM relations WHERE id=?", (orig_id,)
        ).fetchone()
        self.assertEqual(row, (p1, None))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )

    def test_semantic_edges_remain_active_and_are_reported_in_worklist(self):
        p1 = self._mk("Dedup P1")
        p2 = self._mk("Dedup P2")
        x = self._mk("Dedup X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=p2, target_id=x, predicate="depends_on", db_connection=self.conn)

        result = consolidate_memories(
            parent_ids=[p1, p2],
            title="C Dedup",
            content=_cons_content("dedup"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        c_id = result["data"]["entity_id"]

        active = self._active_relations(target_id=x, predicate="depends_on")
        self.assertEqual(
            len(active), 2, "both semantic edges remain active on their archived parents"
        )
        self.assertEqual({row[1] for row in active}, {p1, p2})
        worklist = result["data"]["orphaned_relations"]
        self.assertEqual(len(worklist), 2)
        self.assertEqual({item["originating_parent"] for item in worklist}, {p1, p2})
        self.assertEqual({item["other_endpoint"] for item in worklist}, {x})
        self.assertEqual(
            len(
                self.conn.execute(
                    "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on' AND valid_to IS NULL",
                    (c_id, x),
                ).fetchall()
            ),
            0,
        )

        old_rows = self.conn.execute(
            "SELECT source_id, valid_to FROM relations WHERE target_id=? AND predicate='depends_on' AND source_id IN (?, ?)",
            (x, p1, p2),
        ).fetchall()
        self.assertEqual(
            len(old_rows),
            2,
            "both P1's and P2's original rows must still exist and remain active",
        )
        for _src, valid_to in old_rows:
            self.assertIsNone(valid_to)

    def test_inter_parent_semantic_edge_remains_active_without_self_loop_copy(self):
        p1 = self._mk("SelfLoop P1")
        p2 = self._mk("SelfLoop P2")
        store_relation(source_id=p1, target_id=p2, predicate="depends_on", db_connection=self.conn)

        orig_row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'",
            (p1, p2),
        ).fetchone()
        self.assertIsNotNone(orig_row)

        result = consolidate_memories(
            parent_ids=[p1, p2],
            title="C Self Loop",
            content=_cons_content("self-loop"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        c_id = result["data"]["entity_id"]

        row = self.conn.execute(
            "SELECT valid_to FROM relations WHERE id=?", (orig_row[0],)
        ).fetchone()
        self.assertIsNone(row[0], "semantic inter-parent edge remains active historical truth")
        self.assertEqual(len(result["data"]["orphaned_relations"]), 1)
        self.assertEqual(result["data"]["orphaned_relations"][0]["other_endpoint"], p2)

        self_loop_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=?", (c_id, c_id)
        ).fetchone()[0]
        self.assertEqual(self_loop_count, 0, "no C->C self-loop row should ever be created")

    def test_consolidated_parent_is_rejected_without_touching_lineage_edges(self):
        a = self._mk("MultiGen A")
        b = self._mk("MultiGen B")
        d = self._mk("MultiGen D")

        res1 = commit_consolidation(
            parent_ids=[a, b],
            title="C1 MultiGen",
            content=_cons_content("multigen-c1"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", res1)
        c1 = _memory_id(res1)

        c1_to_a = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='consolidated_from'",
            (c1, a),
        ).fetchone()
        c1_to_b = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='consolidated_from'",
            (c1, b),
        ).fetchone()
        self.assertIsNotNone(c1_to_a)
        self.assertIsNotNone(c1_to_b)

        res2 = commit_consolidation(
            parent_ids=[c1, d],
            title="C2 MultiGen",
            content=_cons_content("multigen-c2"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("INACTIVE_PARENT", res2)

        c1_to_a_after = self.conn.execute(
            "SELECT source_id, valid_to FROM relations WHERE id=?", (c1_to_a[0],)
        ).fetchone()
        c1_to_b_after = self.conn.execute(
            "SELECT source_id, valid_to FROM relations WHERE id=?", (c1_to_b[0],)
        ).fetchone()
        self.assertEqual(
            c1_to_a_after[0],
            c1,
            "C1's own consolidated_from edge to A must NOT be repointed by the 2nd consolidation",
        )
        self.assertIsNone(
            c1_to_a_after[1], "C1's consolidated_from edge to A must remain active (not expired)"
        )
        self.assertEqual(c1_to_b_after[0], c1)
        self.assertIsNone(c1_to_b_after[1])

        new_edge_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE predicate='consolidated_from' AND target_id=? AND valid_to IS NULL",
            (c1,),
        ).fetchone()[0]
        self.assertEqual(new_edge_count, 0, "rejected consolidation must not add a descendant edge")

    def test_exclusion_is_predicate_scoped_not_parent_scoped(self):
        a = self._mk("PredScope A")
        b = self._mk("PredScope B")
        y = self._mk("PredScope Y")

        store_relation(source_id=a, target_id=y, predicate="depends_on", db_connection=self.conn)
        result = consolidate_memories(
            parent_ids=[a, b],
            title="C PredScope",
            content=_cons_content("predscope"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        c_id = result["data"]["entity_id"]
        old_row = self.conn.execute(
            "SELECT valid_to FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'",
            (a, y),
        ).fetchone()
        self.assertIsNone(old_row[0], "semantic edge remains active on archived parent")
        self.assertEqual(result["data"]["orphaned_relations"][0]["originating_parent"], a)
        self.assertEqual(result["data"]["orphaned_relations"][0]["other_endpoint"], y)

        new_row_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on' AND valid_to IS NULL",
            (c_id, y),
        ).fetchone()[0]
        self.assertEqual(
            new_row_count,
            0,
            "semantic edges must not be repointed to C3",
        )

    def test_bulk_commit_consolidation_rollback_leaves_no_relations_repointed(self):
        p1 = self._mk("Bulk P1")
        x = self._mk("Bulk X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)
        orig_row = self.conn.execute(
            "SELECT id, valid_to FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'",
            (p1, x),
        ).fetchone()
        self.assertIsNone(orig_row[1])

        p2 = self._mk("Bulk P2 (forced reject)")
        # Second item uses the exact fluff phrase that TC-CONS-01 (test_consolidation_quality.py)
        # proves triggers a REJECT from the quality gate, forcing the whole batch to roll back.
        batch = [
            {
                "parent_ids": [p1],
                "title": "Bulk Item 1 (would succeed alone)",
                "content": _cons_content("bulk-item1"),
            },
            {
                "parent_ids": [p2],
                "title": "Bulk Item 2 (forced reject)",
                "content": "consolidated these files.",
            },
        ]
        results = bulk_commit_consolidation(consolidations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

        row_after = self.conn.execute(
            "SELECT valid_to FROM relations WHERE id=?", (orig_row[0],)
        ).fetchone()
        self.assertIsNone(
            row_after[0], "batch rollback must leave item 1's relation un-repointed/un-expired"
        )

        consolidated_count = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE status='consolidated'"
        ).fetchone()[0]
        self.assertEqual(
            consolidated_count,
            0,
            "no consolidated entity should exist after an all-or-nothing rollback",
        )


class TestAnalyzeLineageRewrite(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk(self, title, owner_id="agent_c"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            db_connection=self.conn,
        )
        return _memory_id(res)

    def test_lineage_chain_and_no_parent_ids_key(self):
        a = self._mk("Lineage A")
        b = self._mk("Lineage B")

        res1 = commit_consolidation(
            parent_ids=[a, b],
            title="Lineage C1",
            content=_cons_content("lineage-c1"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        c1 = _memory_id(res1)
        result = analyze_lineage(entity_id=c1, db_connection=self.conn)
        self.assertNotIn("error", result)
        ancestors = result["ancestors"]
        by_id = {entry["id"]: entry for entry in ancestors}

        self.assertIn(a, by_id)
        self.assertEqual(by_id[a]["generation_depth"], 1)
        self.assertIn(b, by_id)
        self.assertEqual(by_id[b]["generation_depth"], 1)

        for entry in ancestors:
            self.assertNotIn(
                "parent_ids", entry, "per-ancestor dicts must not contain a parent_ids key"
            )

    def test_cycle_guard_terminates(self):
        x = self._mk("Cycle X")
        y = self._mk("Cycle Y")
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
            "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
            (str(uuid.uuid4()), x, y, now, now),
        )
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
            "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
            (str(uuid.uuid4()), y, x, now, now),
        )
        result = analyze_lineage(entity_id=x, db_connection=self.conn)
        self.assertNotIn("error", result)
        self.assertIn("ancestors", result)
        self.assertLessEqual(
            len(result["ancestors"]),
            12,
            "cycle guard (path-based dedup + depth<10 cap) must bound traversal, not loop forever",
        )

    def test_diamond_ancestry_dedupes_to_shallowest_depth(self):
        # Simpler adequate substitute for a full multi-branch consolidation tree: manually wire
        # up C2 -> C1 -> Z (Z at depth 2 via C1) AND a direct C2 -> Z edge (Z at depth 1),
        # simulating an alternate, shorter lineage path reaching the same ancestor.
        c2 = self._mk("Diamond C2")
        c1 = self._mk("Diamond C1")
        z = self._mk("Diamond Z")
        now = datetime.now(UTC).isoformat()
        for src, tgt in ((c2, c1), (c1, z), (c2, z)):
            self.conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
                "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
                (str(uuid.uuid4()), src, tgt, now, now),
            )

        result = analyze_lineage(entity_id=c2, db_connection=self.conn)
        ancestors = result["ancestors"]
        z_occurrences = [entry for entry in ancestors if entry["id"] == z]
        self.assertEqual(
            len(z_occurrences), 1, "Z must appear exactly once, deduped, not once per path"
        )
        self.assertEqual(
            z_occurrences[0]["generation_depth"],
            1,
            "diamond ancestry must dedupe to the SHALLOWEST depth",
        )


class TestPhase3LineageGraph(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk(self, title):
        result = store_memory(
            content=f"Graph lineage content for {title}",
            title=title,
            owner_id="phase3_graph",
            db_connection=self.conn,
        )
        return _memory_id(result)

    def _edge(self, source_id, target_id, predicate, *, valid_from=None, valid_at=None):
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations "
            "(id, source_id, target_id, predicate, created_at, valid_from, valid_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                source_id,
                target_id,
                predicate,
                now,
                valid_from or now,
                valid_at,
            ),
        )

    def test_descendants_from_archived_parent_find_active_absorbing_node(self):
        archived_parent = self._mk("Archived parent")
        absorbing = self._mk("Active successor")
        self._edge(absorbing, archived_parent, "supersedes")
        self.conn.execute(
            "UPDATE entities SET status='archived', valid_to=? WHERE id=?",
            (datetime.now(UTC).isoformat(), archived_parent),
        )

        result = get_lineage(
            entity_id=archived_parent,
            direction="descendants",
            db_connection=self.conn,
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["direction"], "descendants")
        successor = next(node for node in result["nodes"] if node["id"] == absorbing)
        self.assertEqual(successor["status"], "raw")
        self.assertEqual(successor["depth"], 1)
        self.assertEqual(result["edges"][0]["predicate"], "supersedes")

    def test_lineage_walks_all_lifecycle_predicates_and_preserves_status(self):
        newest = self._mk("Newest")
        revised = self._mk("Revised")
        superseded = self._mk("Superseded")
        consolidated = self._mk("Consolidated")
        self._edge(newest, revised, "revises")
        self._edge(revised, superseded, "supersedes")
        self._edge(superseded, consolidated, "consolidated_from")
        self.conn.execute(
            "UPDATE entities SET status='archived' WHERE id IN (?, ?, ?)",
            (revised, superseded, consolidated),
        )

        result = get_lineage(entity_id=newest, direction="ancestors", db_connection=self.conn)

        self.assertEqual(
            [edge["predicate"] for edge in result["edges"]],
            ["revises", "supersedes", "consolidated_from"],
        )
        by_id = {node["id"]: node for node in result["nodes"]}
        self.assertEqual(by_id[revised]["status"], "archived")
        self.assertEqual(by_id[consolidated]["depth"], 3)

    def test_lineage_honors_max_depth_and_bitemporal_valid_at(self):
        root = self._mk("PIT root")
        hidden = self._mk("Future successor")
        old = self._mk("Old successor")
        future = "2099-01-01T00:00:00+00:00"
        self._edge(hidden, root, "revises", valid_at=future)
        self._edge(old, root, "supersedes", valid_from="2019-01-01T00:00:00+00:00")

        result = get_lineage(
            entity_id=root,
            direction="descendants",
            max_depth=1,
            point_in_time="2020-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )

        ids = {node["id"] for node in result["nodes"]}
        self.assertIn(old, ids)
        self.assertNotIn(hidden, ids)
        self.assertEqual(result["total"], 1)

    def test_lineage_cycle_is_bounded_and_related_graph_has_named_contract(self):
        a = self._mk("Cycle A")
        b = self._mk("Cycle B")
        self._edge(b, a, "revises")
        self._edge(a, b, "supersedes")

        result = get_lineage(
            entity_id=a, direction="descendants", max_depth=20, db_connection=self.conn
        )
        self.assertLessEqual(len(result["edges"]), 2)
        related = get_related_memories(entity_id=a, db_connection=self.conn)
        self.assertIn("related_memories", related)
        self.assertIn("dependencies", related)


class TestRelationPointInTime(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk(self, title, owner_id="agent_c"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            db_connection=self.conn,
        )
        return _memory_id(res)

    def _now(self):
        return datetime.now(UTC).isoformat()

    def test_analyze_dependencies_excludes_edge_created_after_point_in_time(self):
        s = self._mk("PIT Deps S")
        t = self._mk("PIT Deps T")
        pit_before = self._now()
        time.sleep(1.1)
        store_relation(source_id=s, target_id=t, predicate="depends_on", db_connection=self.conn)

        before_result = analyze_dependencies(
            root_entity_id=s, point_in_time=pit_before, db_connection=self.conn
        )
        self.assertEqual(before_result["total_dependencies_found"], 0)

        now_result = analyze_dependencies(root_entity_id=s, db_connection=self.conn)
        self.assertEqual(now_result["total_dependencies_found"], 1)

    def test_analyze_dependencies_nodes_have_no_redundant_path_field(self):
        root = self._mk("Path Removal Root")
        mid = self._mk("Path Removal Mid")
        leaf = self._mk("Path Removal Leaf")
        store_relation(
            source_id=root, target_id=mid, predicate="depends_on", db_connection=self.conn
        )
        store_relation(
            source_id=mid, target_id=leaf, predicate="depends_on", db_connection=self.conn
        )

        result = analyze_dependencies(root_entity_id=root, db_connection=self.conn)
        for node in result["dependencies"]:
            self.assertNotIn(
                "path", node, "nodes must not carry a redundant pre-joined ancestor path string"
            )
        for edge in result["edges"]:
            self.assertNotIn(
                "path", edge, "edges must not carry a redundant pre-joined ancestor path string"
            )

    def test_analyze_dependencies_exposes_edges_with_reconstructable_hierarchy(self):
        root = self._mk("Hierarchy Root")
        mid = self._mk("Hierarchy Mid")
        leaf = self._mk("Hierarchy Leaf")
        store_relation(
            source_id=root, target_id=mid, predicate="depends_on", db_connection=self.conn
        )
        store_relation(
            source_id=mid, target_id=leaf, predicate="depends_on", db_connection=self.conn
        )

        result = analyze_dependencies(root_entity_id=root, db_connection=self.conn)
        self.assertIn("edges", result)
        for edge in result["edges"]:
            self.assertIn("source_id", edge)
            self.assertIn("target_id", edge)
            self.assertIn("depth", edge)
            self.assertIn("predicate", edge)

        # Walk edges from the leaf back to the root via target_id -> source_id to prove the
        # ancestor chain is fully reconstructable without any pre-joined path string.
        by_target = {e["target_id"]: e for e in result["edges"]}
        chain = [leaf]
        current = leaf
        while current in by_target:
            current = by_target[current]["source_id"]
            chain.append(current)
        self.assertEqual(chain, [leaf, mid, root])

    def test_analyze_dependencies_cycle_guard_still_functions_after_path_field_removed_from_output(
        self,
    ):
        a = self._mk("Cycle Guard A")
        b = self._mk("Cycle Guard B")
        c = self._mk("Cycle Guard C")
        store_relation(source_id=a, target_id=b, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=b, target_id=c, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=c, target_id=a, predicate="depends_on", db_connection=self.conn)

        result = analyze_dependencies(root_entity_id=a, max_depth=10, db_connection=self.conn)
        self.assertNotIn("error", result)
        node_ids = {n["id"] for n in result["dependencies"]}
        self.assertEqual(
            node_ids,
            {a, b, c},
            "cycle guard (SQL-level path column) must still terminate traversal correctly",
        )

    def test_analyze_dependencies_diamond_dedupes_edges(self):
        # Diamond: root -> a, root -> b, a -> c, b -> c. The relation root->c doesn't exist
        # directly, but c is reached via two distinct paths (root->a->c and root->b->c), which
        # previously caused every relation downstream of the convergence point to be emitted
        # once per incoming path.
        root = self._mk("Diamond Deps Root")
        a = self._mk("Diamond Deps A")
        b = self._mk("Diamond Deps B")
        c = self._mk("Diamond Deps C")
        store_relation(source_id=root, target_id=a, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=root, target_id=b, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=a, target_id=c, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=b, target_id=c, predicate="depends_on", db_connection=self.conn)

        result = analyze_dependencies(root_entity_id=root, max_depth=10, db_connection=self.conn)
        self.assertNotIn("error", result)

        relation_ids = [e["relation_id"] for e in result["edges"]]
        self.assertEqual(
            len(relation_ids),
            len(set(relation_ids)),
            "each relation must appear at most once in edges, even when reached via "
            "multiple converging paths",
        )
        self.assertEqual(len(result["edges"]), 4, "diamond has exactly 4 distinct relations")
        self.assertEqual(result["total_dependencies_found"], 4)

    def test_analyze_dependencies_direction_defaults_to_outbound_unchanged(self):
        # Cold-start review Issue B: analyze_dependencies' own default must stay outbound-only,
        # byte-for-byte the same behavior as before direction existed -- only get_related_memories
        # (the higher-level, agent-facing tool) gets the new both-directions default.
        a = self._mk("Outbound Default A")
        b = self._mk("Outbound Default B")
        store_relation(source_id=a, target_id=b, predicate="depends_on", db_connection=self.conn)

        from_source = analyze_dependencies(root_entity_id=a, db_connection=self.conn)
        self.assertEqual(from_source["total_dependencies_found"], 1)

        from_target = analyze_dependencies(root_entity_id=b, db_connection=self.conn)
        self.assertEqual(
            from_target["total_dependencies_found"],
            0,
            "unchanged default: an entity that is only ever a relation's target must still "
            "report zero dependencies when direction is omitted",
        )

    def test_analyze_dependencies_direction_inbound_surfaces_target_only_entity(self):
        # The verified bug: an entity that is only ever a relation's target previously always
        # reported zero dependencies via analyze_dependencies, regardless of real inbound edges.
        a = self._mk("Inbound Fix A")
        b = self._mk("Inbound Fix B")
        store_relation(source_id=a, target_id=b, predicate="depends_on", db_connection=self.conn)

        result = analyze_dependencies(
            root_entity_id=b, direction="inbound", db_connection=self.conn
        )
        self.assertEqual(result["total_dependencies_found"], 1)
        edge = result["edges"][0]
        self.assertEqual(edge["source_id"], a)
        self.assertEqual(edge["target_id"], b)
        node_ids = {n["id"] for n in result["dependencies"]}
        self.assertEqual(node_ids, {a, b})

    def test_analyze_dependencies_direction_both_unions_outbound_and_inbound(self):
        # root has one outbound edge (root -> downstream) and is also the target of one inbound
        # edge (upstream -> root) -- direction="both" must surface both, not just one.
        upstream = self._mk("Both Union Upstream")
        root = self._mk("Both Union Root")
        downstream = self._mk("Both Union Downstream")
        store_relation(
            source_id=upstream, target_id=root, predicate="depends_on", db_connection=self.conn
        )
        store_relation(
            source_id=root, target_id=downstream, predicate="depends_on", db_connection=self.conn
        )

        result = analyze_dependencies(
            root_entity_id=root, direction="both", db_connection=self.conn
        )
        self.assertEqual(result["total_dependencies_found"], 2)
        node_ids = {n["id"] for n in result["dependencies"]}
        self.assertEqual(node_ids, {upstream, root, downstream})

    def test_analyze_dependencies_direction_both_mixed_direction_cycle_terminates_safely(self):
        # Regression test flagged by adversarial plan review: direction="both" runs two
        # independent single-direction traversals, each keeping its own untouched cycle guard.
        # A cycle reachable by mixing directions (root -> a outbound, then a -> root inbound,
        # i.e. the same a<->root pair linked both ways) must not cause runaway recursion or a
        # crash in either guard, even though neither guard is aware of the other's traversal.
        root = self._mk("Mixed Cycle Root")
        a = self._mk("Mixed Cycle A")
        store_relation(source_id=root, target_id=a, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=a, target_id=root, predicate="depends_on", db_connection=self.conn)

        result = analyze_dependencies(
            root_entity_id=root, direction="both", max_depth=10, db_connection=self.conn
        )
        self.assertNotIn("error", result)
        node_ids = {n["id"] for n in result["dependencies"]}
        self.assertEqual(node_ids, {root, a})
        # Both edges are real, distinct relation rows (root->a and a->root) -- both must be
        # reported, and each exactly once (no duplicate/runaway rows from either guard).
        self.assertEqual(result["total_dependencies_found"], 2)

    def test_get_related_memories_default_direction_surfaces_target_only_entity(self):
        # Repro of the original cold-start report's exact observed symptom, at the
        # get_related_memories tool level: entity B was only ever a relation's target and
        # get_related_memories(entity_id=B) always reported zero relations, indistinguishable
        # from "this entity has no relations." Now defaults to direction="both".
        a = self._mk("Tool-Level Inbound Fix A")
        b = self._mk("Tool-Level Inbound Fix B")
        store_relation(source_id=a, target_id=b, predicate="elaborates_on", db_connection=self.conn)

        result = get_related_memories(entity_id=b, db_connection=self.conn)
        self.assertEqual(result["total_related_found"], 1)
        related_ids = {n["id"] for n in result["related_memories"]}
        self.assertEqual(related_ids, {a, b})

    def test_analyze_dependencies_preserves_truthful_edge_history_without_repointing(self):
        p1 = self._mk("PIT Cons P1")
        p2 = self._mk("PIT Cons P2")
        x = self._mk("PIT Cons X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)

        pit_before_consolidation = self._now()
        time.sleep(1.1)

        res = commit_consolidation(
            parent_ids=[p1, p2],
            title="PIT Cons C",
            content=_cons_content("pit-deps-cons"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        c_id = _memory_id(res)

        historical = analyze_dependencies(
            root_entity_id=p1, point_in_time=pit_before_consolidation, db_connection=self.conn
        )
        self.assertEqual(
            historical["total_dependencies_found"],
            1,
            "edge valid at pit_before_consolidation must still appear",
        )

        current_from_p1 = analyze_dependencies(root_entity_id=p1, db_connection=self.conn)
        self.assertEqual(
            current_from_p1["total_dependencies_found"],
            1,
            "semantic edge remains active on archived parent; no repointing closes it",
        )

        current_from_c = analyze_dependencies(root_entity_id=c_id, db_connection=self.conn)
        # C only has lifecycle edges to its parents.  The semantic neighbour is not fabricated
        # on C, while point-in-time history and the archived parent still expose the true edge.
        dep_ids = {n["id"] for n in current_from_c["dependencies"]}
        self.assertIn(x, dep_ids, "lineage traversal may truthfully reach X through archived P1")
        direct_replacement = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on' AND valid_to IS NULL",
            (c_id, x),
        ).fetchone()
        self.assertIsNone(
            direct_replacement, "no fabricated direct replacement edge C->X may appear"
        )

    def test_analyze_lineage_excludes_edge_created_after_point_in_time(self):
        a = self._mk("PIT Lineage A")
        b = self._mk("PIT Lineage B")
        pit_before = self._now()
        time.sleep(1.1)
        res = commit_consolidation(
            parent_ids=[a, b],
            title="PIT Lineage C1",
            content=_cons_content("pit-lineage-c1"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        c1 = _memory_id(res)

        historical = analyze_lineage(
            entity_id=c1, point_in_time=pit_before, db_connection=self.conn
        )
        self.assertEqual(
            historical["total_ancestors"],
            0,
            "consolidated_from edges created after pit_before must not appear",
        )

        current = analyze_lineage(entity_id=c1, db_connection=self.conn)
        self.assertEqual(current["total_ancestors"], 2)
        self.assertNotIn(
            "ancestry_tree",
            current,
            "ancestry_tree must not be duplicated alongside ancestors in the response",
        )

    def test_analyze_lineage_shows_manually_expired_edge_at_earlier_pit_not_at_now(self):
        # analyze_lineage's own edges (predicate='consolidated_from') are, by design, never
        # expired through the normal commit_consolidation repointing path -- that exclusion is
        # exactly the multi-generation fix covered above -- so there is no real production flow
        # that expires a consolidated_from edge. To still exercise point-in-time filtering for
        # lineage symmetrically with the dependencies case above, this manually expires a
        # consolidated_from edge via raw SQL as a substitute.
        z = self._mk("PIT Lineage Manual Z")
        w = self._mk("PIT Lineage Manual W")
        rel_id = str(uuid.uuid4())
        t0 = self._now()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
            "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
            (rel_id, z, w, t0, t0),
        )
        pit_while_active = self._now()
        time.sleep(1.1)
        expire_at = self._now()
        self.conn.execute("UPDATE relations SET valid_to = ? WHERE id = ?", (expire_at, rel_id))

        historical = analyze_lineage(
            entity_id=z, point_in_time=pit_while_active, db_connection=self.conn
        )
        self.assertEqual(
            historical["total_ancestors"],
            1,
            "edge was active as of pit_while_active, must still show",
        )

        current = analyze_lineage(entity_id=z, db_connection=self.conn)
        self.assertEqual(
            current["total_ancestors"], 0, "now-expired edge must not appear with no point_in_time"
        )

    def test_omitting_point_in_time_matches_pre_round_default_behavior(self):
        s = self._mk("PIT Default S")
        t = self._mk("PIT Default T")
        store_relation(source_id=s, target_id=t, predicate="depends_on", db_connection=self.conn)

        deps_result = analyze_dependencies(root_entity_id=s, db_connection=self.conn)
        self.assertIn("root", deps_result)
        self.assertIn("dependencies", deps_result)
        self.assertIn("point_in_time", deps_result)
        self.assertEqual(deps_result["total_dependencies_found"], 1)

        lineage_result = analyze_lineage(entity_id=s, db_connection=self.conn)
        self.assertIn("ancestors", lineage_result)
        self.assertIn("point_in_time", lineage_result)


class TestOrphanDetectionWithExpiredRelations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk(self, title, owner_id="orphan_tester"):
        res = store_memory(
            content=f"Raw content body for entity {title}",
            title=title,
            owner_id=owner_id,
            db_connection=self.conn,
        )
        return _memory_id(res)

    def test_entity_whose_only_relation_is_expired_is_flagged_orphan(self):
        e1 = self._mk("Orphan E1")
        e2 = self._mk("Orphan E2")
        store_relation(source_id=e1, target_id=e2, predicate="depends_on", db_connection=self.conn)
        self.conn.execute(
            "UPDATE relations SET valid_to = ? WHERE source_id = ? AND target_id = ?",
            ("2020-01-01T00:00:00+00:00", e1, e2),
        )
        result = detect_orphaned_memories(owner_id="orphan_tester", db_connection=self.conn)
        orphan_ids = {o["id"] for o in result["orphaned_memories"]}
        self.assertIn(
            e1, orphan_ids, "an entity whose only relation is expired must be flagged as an orphan"
        )
        self.assertIn(e2, orphan_ids)
        self.assertNotIn("details", result)
        self.assertNotIn("orphans_detected", result)

    def test_entity_with_active_relation_is_not_flagged_orphan(self):
        e3 = self._mk("Orphan E3")
        e4 = self._mk("Orphan E4")
        store_relation(source_id=e3, target_id=e4, predicate="depends_on", db_connection=self.conn)
        result = detect_orphaned_memories(owner_id="orphan_tester", db_connection=self.conn)
        orphan_ids = {o["id"] for o in result["orphaned_memories"]}
        self.assertNotIn(
            e3, orphan_ids, "an entity with an active relation must NOT be flagged as an orphan"
        )
        self.assertNotIn(e4, orphan_ids)
        self.assertNotIn("details", result)
        self.assertNotIn("orphans_detected", result)

    def test_total_orphans_matches_orphaned_memories_length(self):
        self._mk("Orphan E5")
        result = detect_orphaned_memories(owner_id="orphan_tester", db_connection=self.conn)
        self.assertEqual(result["total_orphans"], len(result["orphaned_memories"]))


class TestCommitConsolidationCohesionGate(unittest.TestCase):
    """Memory-core rework Phase 3, Part A -- the pairwise cohesion gate (see plans/ and SALTMDB
    memory `5c09effa`). Parents' chunk vectors are inserted directly into
    entity_chunk_embeddings (axis-aligned, exact cosine similarity by construction), mirroring
    tests/test_topic_rerank.py's / tests/test_cohesion_service.py's pattern, so cohesive vs
    incohesive parent sets are hand-computable rather than relying on real-model variance."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk_vector_entity(self, title: str, vector: list, status: str = "raw") -> tuple[str, str]:
        """Inserts a bare `entities` row plus a single matching entity_chunk_embeddings row
        (bypassing store_memory's async chunk-embed trigger entirely), so this test class
        controls every parent's centroid directly -- a single chunk's centroid is exactly that
        chunk's own (already-unit) vector."""
        entity_id = str(uuid.uuid4())
        content_hash = f"hash-{entity_id}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', ?, ?, ?, ?)",
            (entity_id, now, now, now, status, title, f"content body for {title}", content_hash),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, ?)",
            (f"{entity_id}::0", entity_id, sqlite_vec.serialize_float32(vector), content_hash),
        )
        self.conn.commit()
        return entity_id, content_hash

    def test_commit_consolidation_rejects_incohesive_parent_set_without_override(self):
        a, _ = self._mk_vector_entity("Incohesive A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Incohesive B", _axis_vector(1))  # orthogonal -> min_sim=0.0

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Incohesive",
            content=_cons_content("incohesive"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error: REJECT_LOW_COHESION"), res)

    def test_commit_consolidation_rejects_when_a_parent_has_no_usable_centroid(self):
        a, _ = self._mk_vector_entity("Unresolvable Pair A", _axis_vector(0))
        # b has empty full_content and no chunk rows -> unresolved ("no embeddable content").
        b = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'raw', 'Unresolvable Pair B', '', ?)",
            (b, now, now, now, "empty-hash"),
        )
        self.conn.commit()

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Unresolvable",
            content=_cons_content("unresolvable"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Error: REJECT_LOW_COHESION"), res)
        self.assertIn("unresolved", res)

    def test_commit_consolidation_accepts_incohesive_parent_set_with_valid_override_justification(
        self,
    ):
        a, _ = self._mk_vector_entity("Override A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Override B", _axis_vector(1))

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Override",
            content=_cons_content("override"),
            owner_id="agent_c",
            db_connection=self.conn,
            override_justification=(
                "deliberately merging unrelated axis-0/axis-1 test fixtures for override coverage"
            ),
        )
        self.assertIn("Successfully committed", res, res)
        consolidated_id = res.split("ID: ")[1].strip()

        content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (consolidated_id,)
        ).fetchone()[0]
        self.assertIn("[Consolidation Override]", content)

        events = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_gate_override'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        event_data = json.loads(events[0][0])
        self.assertEqual(event_data["consolidated_id"], consolidated_id)

    def test_commit_consolidation_rejects_override_justification_below_minimum_length(self):
        a, _ = self._mk_vector_entity("Short Override A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Short Override B", _axis_vector(1))

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Short Override",
            content=_cons_content("short-override"),
            owner_id="agent_c",
            db_connection=self.conn,
            override_justification="too short",
        )
        self.assertTrue(res.startswith("Error: REJECT_LOW_COHESION"), res)

    def test_commit_consolidation_rejects_single_parent_with_zero_side_effects(self):
        a, _ = self._mk_vector_entity("Solo Parent", _axis_vector(0))
        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        res = commit_consolidation(
            parent_ids=[a],
            title="C Solo",
            content=_cons_content("solo"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("REJECT_PARENT_COUNT", res, res)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id = ?", (a,)).fetchone()[0], "raw"
        )

    def test_commit_consolidation_rejects_single_unscorable_parent_with_zero_side_effects(self):
        """Single-parent consolidation is rejected before cohesion/embedding work regardless of
        whether the parent has usable content."""
        a = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'raw', 'Solo Unresolvable', '', ?)",
            (a, now, now, now, "empty-hash-solo"),
        )
        self.conn.commit()

        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        res = commit_consolidation(
            parent_ids=[a],
            title="C Solo Unresolvable",
            content=_cons_content("solo-unresolvable"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("REJECT_PARENT_COUNT", res, res)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM entities WHERE id = ?", (a,)).fetchone()[0], "raw"
        )

    def test_commit_consolidation_accepts_override_for_active_unscorable_parent(self):
        """P1 regression (Codex review bf4qtkp7j / 7a5eba85): get_fresh_entity_centroids used to
        drop observed_state entirely for a parent whose row read succeeded but embedding failed
        (e.g. no embeddable content), so commit's TOCTOU revalidation hard-rejected even a valid
        override_justification with observed=None before any merge or audit event. An
        active-but-unscorable parent must remain override-eligible and auditable."""
        a, _ = self._mk_vector_entity("Scorable Partner", _axis_vector(0))
        b = str(uuid.uuid4())  # active parent, no embeddable content -> unresolved but not archived
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'raw', 'Active Unscorable', '', ?)",
            (b, now, now, now, "empty-hash-active"),
        )
        self.conn.commit()

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Active Unscorable Override",
            content=_cons_content("active-unscorable-override"),
            owner_id="agent_c",
            db_connection=self.conn,
            override_justification=(
                "merging despite one parent having no scorable content, override intentional"
            ),
        )
        self.assertIn("Successfully committed", res, res)
        consolidated_id = res.split("ID: ")[1].strip()

        events = self.conn.execute(
            "SELECT content FROM events WHERE type = 'consolidation_gate_override'"
        ).fetchall()
        self.assertEqual(len(events), 1)
        event_data = json.loads(events[0][0])
        self.assertEqual(event_data["consolidated_id"], consolidated_id)

        b_status = self.conn.execute("SELECT status FROM entities WHERE id = ?", (b,)).fetchone()[0]
        self.assertEqual(b_status, "archived", "override must still archive the unscorable parent")

    def test_commit_consolidation_rejects_override_for_archived_unscorable_parent(self):
        """Contrast case for the fix above: an archived parent must stay hard-rejected even with
        a valid override_justification -- observed_state is never recorded for it, so TOCTOU
        revalidation cannot be satisfied and the merge must not proceed."""
        a, _ = self._mk_vector_entity("Scorable Partner Archived Case", _axis_vector(0))
        b = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'archived', 'Archived Unscorable', '', ?)",
            (b, now, now, now, "empty-hash-archived"),
        )
        self.conn.commit()

        res = commit_consolidation(
            parent_ids=[a, b],
            title="C Archived Unscorable Override",
            content=_cons_content("archived-unscorable-override"),
            owner_id="agent_c",
            db_connection=self.conn,
            override_justification=(
                "attempting to merge despite one parent already being archived, should fail"
            ),
        )
        self.assertTrue(res.startswith("Error"), res)

        consolidated_count = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE title = 'C Archived Unscorable Override'"
        ).fetchone()[0]
        self.assertEqual(consolidated_count, 0)

    def test_commit_consolidation_rolls_back_when_audit_event_insertion_fails(self):
        a, _ = self._mk_vector_entity("Audit Fail A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Audit Fail B", _axis_vector(1))

        with patch(
            "saltmdb.domain.services.relation_service.log_event",
            return_value="Error: simulated audit failure",
        ):
            res = commit_consolidation(
                parent_ids=[a, b],
                title="C Audit Fail",
                content=_cons_content("audit-fail"),
                owner_id="agent_c",
                db_connection=self.conn,
                override_justification=(
                    "deliberately merging unrelated fixtures to exercise the audit-failure "
                    "rollback path"
                ),
            )
        self.assertTrue(res.startswith("Error"), res)

        a_status = self.conn.execute("SELECT status FROM entities WHERE id = ?", (a,)).fetchone()[0]
        b_status = self.conn.execute("SELECT status FROM entities WHERE id = ?", (b,)).fetchone()[0]
        self.assertEqual(a_status, "raw")
        self.assertEqual(b_status, "raw")

        consolidated_count = self.conn.execute(
            "SELECT COUNT(*) FROM entities WHERE title = 'C Audit Fail'"
        ).fetchone()[0]
        self.assertEqual(consolidated_count, 0)

    def test_commit_consolidation_revalidates_against_the_state_that_produced_the_centroid(self):
        a, _ = self._mk_vector_entity("Reval A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Reval B", _axis_vector(0))  # identical -> cohesive

        real_get_centroids = get_fresh_entity_centroids

        def _racy_get_centroids(entity_ids, conn, db_path):
            result = real_get_centroids(entity_ids, conn, db_path)
            # Simulate a concurrent edit landing in the gap between centroid computation and
            # the in-transaction revalidation read inside _do_commit.
            conn.execute("UPDATE entities SET content_hash = 'mutated-mid-call' WHERE id = ?", (a,))
            conn.commit()
            return result

        with patch(
            "saltmdb.domain.services.relation_service.get_fresh_entity_centroids",
            side_effect=_racy_get_centroids,
        ):
            res = commit_consolidation(
                parent_ids=[a, b],
                title="C Reval Race",
                content=_cons_content("reval-race"),
                owner_id="agent_c",
                db_connection=self.conn,
            )
        self.assertTrue(res.startswith("Error"), res)
        a_status = self.conn.execute("SELECT status FROM entities WHERE id = ?", (a,)).fetchone()[0]
        self.assertEqual(a_status, "raw", "parent must not be archived by the aborted commit")

        # Inverse: content_hash changes BEFORE centroid computation runs at all -- the "new"
        # content is what gets embedded/snapshotted, so nothing changes in the gap and this must
        # succeed normally.
        c, _ = self._mk_vector_entity("Reval C", _axis_vector(0))
        d, _ = self._mk_vector_entity("Reval D", _axis_vector(0))
        self.conn.execute(
            "UPDATE entities SET content_hash = 'changed-before-call' WHERE id = ?", (c,)
        )
        # vec0 virtual tables don't support UPDATE against a partition-key-scoped predicate
        # (confirmed: "UPDATE on partition key columns are not supported yet") -- DELETE +
        # re-INSERT instead, mirroring write_entity_chunk_embeddings' own re-embed pattern.
        self.conn.execute("DELETE FROM entity_chunk_embeddings WHERE entity_id = ?", (c,))
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, ?)",
            (f"{c}::0", c, sqlite_vec.serialize_float32(_axis_vector(0)), "changed-before-call"),
        )
        self.conn.commit()
        res2 = commit_consolidation(
            parent_ids=[c, d],
            title="C Reval No Race",
            content=_cons_content("reval-no-race"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("Successfully committed", res2, res2)

    def test_commit_consolidation_rejects_consolidated_entity_as_inactive_parent(self):
        consolidated_id, _ = self._mk_vector_entity(
            "Already Consolidated Parent", _axis_vector(0), status="consolidated"
        )
        fresh_raw, _ = self._mk_vector_entity("Refresh Raw Evidence", _axis_vector(0))

        before_entities = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        res = commit_consolidation(
            parent_ids=[consolidated_id, fresh_raw],
            title="C Refresh Consolidated",
            content=_cons_content("refresh-consolidated"),
            owner_id="agent_c",
            db_connection=self.conn,
        )
        self.assertIn("INACTIVE_PARENT", res, res)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], before_entities
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM entities WHERE id = ?", (consolidated_id,)
            ).fetchone()[0],
            "consolidated",
        )

    def test_bulk_commit_consolidation_per_item_override_does_not_leak_to_other_items(self):
        a1, _ = self._mk_vector_entity("Bulk Leak A1", _axis_vector(0))
        a2, _ = self._mk_vector_entity("Bulk Leak A2", _axis_vector(1))  # incohesive with a1

        b1, _ = self._mk_vector_entity("Bulk Leak B1", _axis_vector(5))
        b2, _ = self._mk_vector_entity("Bulk Leak B2", _axis_vector(5))  # cohesive, no override

        batch = [
            {
                "parent_ids": [a1, a2],
                "title": "Bulk Leak Item A",
                "content": _cons_content("bulk-leak-a"),
                "override_justification": (
                    "deliberately merging unrelated axis-0/axis-1 test fixtures"
                ),
            },
            {
                "parent_ids": [b1, b2],
                "title": "Bulk Leak Item B",
                "content": _cons_content("bulk-leak-b"),
            },
        ]
        results = bulk_commit_consolidation(consolidations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 2, results)
        self.assertEqual(results[0]["status"], "success", results)
        self.assertEqual(results[1]["status"], "success", results)

        item_a_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (results[0]["entity_id"],)
        ).fetchone()[0]
        item_b_content = self.conn.execute(
            "SELECT full_content FROM entities WHERE id = ?", (results[1]["entity_id"],)
        ).fetchone()[0]
        self.assertIn("[Consolidation Override]", item_a_content)
        self.assertNotIn("[Consolidation Override]", item_b_content)

    def test_bulk_commit_consolidation_precomputes_centroids_before_write_transaction(self):
        a, _ = self._mk_vector_entity("Bulk Precompute A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Bulk Precompute B", _axis_vector(0))

        real_get_centroids = get_fresh_entity_centroids
        call_count = {"n": 0}

        def _counting_get_centroids(entity_ids, conn, db_path):
            call_count["n"] += 1
            return real_get_centroids(entity_ids, conn, db_path)

        def _raising_write_transaction(conn, fn):
            raise RuntimeError("write_transaction_retrying should not run yet in this assertion")

        with (
            patch(
                "saltmdb.domain.services.relation_service.get_fresh_entity_centroids",
                side_effect=_counting_get_centroids,
            ),
            patch(
                "saltmdb.domain.services.relation_service.write_transaction_retrying",
                side_effect=_raising_write_transaction,
            ),
        ):
            results = bulk_commit_consolidation(
                consolidations=[
                    {
                        "parent_ids": [a, b],
                        "title": "Bulk Precompute Item",
                        "content": _cons_content("bulk-precompute"),
                    }
                ],
                db_connection=self.conn,
            )

        # write_transaction_retrying raising is caught by bulk_commit_consolidation's own
        # try/except and reported as a top-level error -- but the centroid precompute must
        # already have run exactly once by that point, proving it happens BEFORE the write
        # transaction opens, not inside it.
        self.assertEqual(results[0]["status"], "error", results)
        self.assertEqual(call_count["n"], 1)

    def test_bulk_commit_consolidation_resolves_and_dedupes_parent_ids_before_building_union(self):
        a, _ = self._mk_vector_entity("Bulk Union Dedup A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Bulk Union Dedup B", _axis_vector(0))
        c, _ = self._mk_vector_entity("Bulk Union Dedup C", _axis_vector(0))

        real_get_centroids = get_fresh_entity_centroids
        captured = {}

        def _capturing_get_centroids(entity_ids, conn, db_path):
            captured["ids"] = list(entity_ids)
            return real_get_centroids(entity_ids, conn, db_path)

        batch = [
            {
                # References `a` by its raw canonical UUID.
                "parent_ids": [a, b],
                "title": "Bulk Union Item 1",
                "content": _cons_content("bulk-union-1"),
            },
            {
                # References the SAME entity `a` via its title (an alias-equivalent reference,
                # resolve_entity_id's title-resolution fallback), alongside a disjoint parent
                # `c` -- item 2 doesn't share a consolidation TARGET with item 1, only the raw
                # *reference* to `a` overlaps, isolating this test from the separate
                # archived-shared-parent race covered by the "second_item_rejects" test below
                # (this batch is expected to abort on that unrelated, separately-tested race --
                # this test only cares about what union_ids get_fresh_entity_centroids saw).
                "parent_ids": ["Bulk Union Dedup A", c],
                "title": "Bulk Union Item 2",
                "content": _cons_content("bulk-union-2"),
            },
        ]

        with patch(
            "saltmdb.domain.services.relation_service.get_fresh_entity_centroids",
            side_effect=_capturing_get_centroids,
        ):
            bulk_commit_consolidation(consolidations=batch, db_connection=self.conn)

        union_ids = captured["ids"]
        self.assertEqual(
            len(union_ids), len(set(union_ids)), "union must not contain duplicate ids"
        )
        self.assertIn(a, union_ids)
        self.assertNotIn(
            "Bulk Union Dedup A",
            union_ids,
            "raw title alias must be resolved before the union is built, not passed through verbatim",
        )

    def test_bulk_commit_consolidation_second_item_rejects_when_first_item_archived_shared_parent(
        self,
    ):
        shared, _ = self._mk_vector_entity("Bulk Shared Parent", _axis_vector(0))
        b1, _ = self._mk_vector_entity("Bulk Shared B1", _axis_vector(0))
        b2, _ = self._mk_vector_entity("Bulk Shared B2", _axis_vector(0))

        batch = [
            {
                "parent_ids": [shared, b1],
                "title": "Bulk Shared Item 1",
                "content": _cons_content("bulk-shared-1"),
            },
            {
                "parent_ids": [shared, b2],
                "title": "Bulk Shared Item 2",
                "content": _cons_content("bulk-shared-2"),
            },
        ]
        results = bulk_commit_consolidation(consolidations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["status"], "error", results)

        # All-or-nothing: item 1's own archiving must have been rolled back too.
        shared_status = self.conn.execute(
            "SELECT status FROM entities WHERE id = ?", (shared,)
        ).fetchone()[0]
        self.assertEqual(shared_status, "raw")

    def test_get_fresh_entity_centroids_self_loads_extension_on_injected_connection(self):
        a, _ = self._mk_vector_entity("Self Load A", _axis_vector(0))
        raw_conn = sqlite3.connect(self.db_path)
        try:
            centroids, unresolved, _observed_state = get_fresh_entity_centroids(
                [a], raw_conn, self.db_path
            )
        finally:
            raw_conn.close()
        self.assertIn(a, centroids)
        self.assertEqual(unresolved, {})

    def test_get_fresh_entity_centroids_excludes_archived_entities_even_via_fallback(self):
        entity_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'archived', ?, ?, ?)",
            (entity_id, now, now, now, "Archived Entity", "some archived content", "archived-hash"),
        )
        self.conn.commit()

        centroids, unresolved, observed_state = get_fresh_entity_centroids(
            [entity_id], self.conn, self.db_path
        )
        self.assertNotIn(entity_id, centroids)
        self.assertIn(entity_id, unresolved)
        self.assertIn("archived", unresolved[entity_id].lower())
        self.assertNotIn(entity_id, observed_state)


class TestStoreRelationGovernanceGate(unittest.TestCase):
    """Memory-core rework Phase 5 -- the manage_relation governance gate on store_relation (see
    plans/structured-finding-matsumoto.md and SALTMDB memory `5c09effa`/`6490fe88`). Reuses
    TestCommitConsolidationCohesionGate's axis-vector fixture pattern so cohesive vs incohesive
    pairs are hand-computable rather than relying on real-model variance."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mk_vector_entity(self, title: str, vector: list, status: str = "raw") -> tuple[str, str]:
        entity_id = str(uuid.uuid4())
        content_hash = f"hash-{entity_id}"
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', ?, ?, ?, ?)",
            (entity_id, now, now, now, status, title, f"content body for {title}", content_hash),
        )
        self.conn.execute(
            "INSERT INTO entity_chunk_embeddings"
            "(id, entity_id, embedding, chunk_index, char_start, char_end, content_hash)"
            " VALUES (?, ?, ?, 0, 0, 10, ?)",
            (f"{entity_id}::0", entity_id, sqlite_vec.serialize_float32(vector), content_hash),
        )
        self.conn.commit()
        return entity_id, content_hash

    def _override_events(self) -> list:
        rows = self.conn.execute(
            "SELECT content FROM events WHERE type = 'relation_gate_override'"
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def test_store_relation_rejects_low_similarity_strong_predicate(self):
        a, _ = self._mk_vector_entity("Low Sim A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Low Sim B", _axis_vector(1))  # orthogonal -> sim=0.0

        res = store_relation(
            source_id=a, target_id=b, predicate="elaborates_on", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Error: REJECT_LOW_RELATION_SIMILARITY"), res)

        row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND target_id = ?", (a, b)
        ).fetchone()
        self.assertIsNone(row)

    def test_store_relation_override_low_similarity_stores_and_logs_audit_event(self):
        a, _ = self._mk_vector_entity("Override Sim A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Override Sim B", _axis_vector(1))

        res = store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            owner_id="agent_override",
            override_justification="deliberately linking orthogonal test fixtures for coverage",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Relation successfully stored"), res)

        events = self._override_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["source_id"], a)
        self.assertEqual(events[0]["target_id"], b)
        self.assertEqual(events[0]["predicate"], "elaborates_on")
        self.assertIn("low_similarity", events[0]["violations"])
        self.assertAlmostEqual(events[0]["similarity"], 0.0, places=4)

    def test_store_relation_passes_gate_silently_on_high_similarity(self):
        a, _ = self._mk_vector_entity("High Sim A", _axis_vector(0))
        b, _ = self._mk_vector_entity("High Sim B", _axis_vector(0))  # identical -> sim=1.0

        res = store_relation(
            source_id=a, target_id=b, predicate="resolves", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Relation successfully stored"), res)
        self.assertEqual(self._override_events(), [])

    def test_store_relation_weak_predicate_bypasses_gate(self):
        a, _ = self._mk_vector_entity("Weak Predicate A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Weak Predicate B", _axis_vector(1))

        res = store_relation(
            source_id=a, target_id=b, predicate="depends_on", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Relation successfully stored"), res)
        self.assertEqual(self._override_events(), [])

    def test_store_relation_similar_to_is_rejected_as_legacy_readonly(self):
        # Phase 6 write-time gate (plan §5.8): similar_to is now legacy/read-only -- it can no
        # longer be created via store_relation at all, so the old similarity-gate-bypass
        # behavior for this predicate is moot; the write-time gate now rejects it outright,
        # before the D3 similarity gate would ever run.
        a, _ = self._mk_vector_entity("Similar To A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Similar To B", _axis_vector(1))

        res = store_relation(
            source_id=a, target_id=b, predicate="similar_to", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Error: LEGACY_READONLY_PREDICATE"), res)
        self.assertEqual(self._override_events(), [])

        row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND target_id = ?", (a, b)
        ).fetchone()
        self.assertIsNone(row)

    def test_store_relation_gate_checks_canonical_predicate_not_alias(self):
        """Phase 6 regression: the closed-vocabulary write-time gate must run on the raw
        submitted predicate BEFORE the D3 similarity gate ever executes -- 'references' aliases
        to 'related_to' (Phase 6 reversed the old elaborates_on target, plan §3.17/§5.8), so an
        alias submission is rejected as NONCANONICAL_PREDICATE, never reaching (and therefore
        never surfacing) a REJECT_LOW_RELATION_SIMILARITY failure."""
        a, _ = self._mk_vector_entity("Alias Gate A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Alias Gate B", _axis_vector(1))

        res = store_relation(
            source_id=a, target_id=b, predicate="references", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Error: NONCANONICAL_PREDICATE"), res)
        self.assertIn("related_to", res)
        self.assertNotIn("REJECT_LOW_RELATION_SIMILARITY", res)

    def test_store_relation_rejects_contradictory_predicate_pair(self):
        a, _ = self._mk_vector_entity("Contradiction A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Contradiction B", _axis_vector(0))  # sim=1.0, isolates test

        # 'supersedes' is now a RESERVED predicate (plan §5.8): store_relation refuses to create
        # one directly, exactly like the real lifecycle tool (supersede_memory) does, which
        # writes it via a hardcoded literal INSERT rather than store_relation. Seed the same way
        # here to simulate that pre-existing edge without going through the (now-closed) gate.
        seed_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)"
            " VALUES (?, ?, ?, 'supersedes', ?, ?, ?)",
            (seed_id, a, b, now, now, now),
        )
        self.conn.commit()

        res = store_relation(
            source_id=a, target_id=b, predicate="elaborates_on", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Error: REJECT_CONTRADICTORY_PREDICATE"), res)

        override_res = store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            override_justification="deliberately allowing a contradictory pair for test coverage",
            db_connection=self.conn,
        )
        self.assertTrue(override_res.startswith("Relation successfully stored"), override_res)

        active_predicates = {
            r[0]
            for r in self.conn.execute(
                "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ? AND valid_to IS NULL",
                (a, b),
            ).fetchall()
        }
        self.assertEqual(active_predicates, {"supersedes", "elaborates_on"})

    def test_store_relation_unresolved_entity_forces_gate_failure(self):
        a, _ = self._mk_vector_entity("Unresolved Partner", _axis_vector(0))
        b = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, created_at, updated_at, last_accessed_at, owner_id, status, title,"
            " full_content, content_hash)"
            " VALUES (?, ?, ?, ?, 'agent_c', 'raw', 'Unresolved B', '', ?)",
            (b, now, now, now, "empty-hash"),
        )
        self.conn.commit()

        res = store_relation(
            source_id=a, target_id=b, predicate="resolves", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Error: REJECT_LOW_RELATION_SIMILARITY"), res)
        self.assertIn("unresolved", res)

        override_res = store_relation(
            source_id=a,
            target_id=b,
            predicate="resolves",
            override_justification="forcing a relation to an unscorable entity for coverage",
            db_connection=self.conn,
        )
        self.assertTrue(override_res.startswith("Relation successfully stored"), override_res)

    def test_store_relation_gate_only_ever_writes_relations_and_events(self):
        a, _ = self._mk_vector_entity("Invariant A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Invariant B", _axis_vector(1))

        def _entities_snapshot():
            return self.conn.execute(
                "SELECT id, status, weight, is_core, updated_at FROM entities ORDER BY id"
            ).fetchall()

        before = _entities_snapshot()
        res = store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            override_justification="invariant check override, no entity row should ever change",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Relation successfully stored"), res)
        after = _entities_snapshot()
        self.assertEqual(before, after)

    def test_store_relation_gate_only_ever_writes_relations_and_events_on_clean_pass(self):
        """Companion to the override-path invariance test above: the same "gate only ever
        writes relations and events" claim must also hold on the high-similarity clean-pass
        path, where no override_justification is supplied at all."""
        a, _ = self._mk_vector_entity("Invariant Clean Pass A", _axis_vector(0))
        b, _ = self._mk_vector_entity(
            "Invariant Clean Pass B", _axis_vector(0)
        )  # identical -> min_sim=1.0

        def _entities_snapshot():
            return self.conn.execute(
                "SELECT id, status, weight, is_core, updated_at FROM entities ORDER BY id"
            ).fetchall()

        before = _entities_snapshot()
        res = store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Relation successfully stored"), res)
        after = _entities_snapshot()
        self.assertEqual(before, after)

    def test_store_relation_override_audit_event_uses_supplied_owner_id(self):
        a, _ = self._mk_vector_entity("Owner Supplied A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Owner Supplied B", _axis_vector(1))

        store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            owner_id="agent_custom_owner",
            override_justification="checking supplied owner_id propagates to the audit event",
            db_connection=self.conn,
        )

        event = self.conn.execute(
            "SELECT agent_id FROM events WHERE type = 'relation_gate_override'"
        ).fetchone()
        self.assertEqual(event[0], "agent_custom_owner")

    def test_bulk_store_relations_uses_batch_owner_for_override_audit_event(self):
        """A daemon/MCP batch default must reach the relation-gate audit event."""
        a, _ = self._mk_vector_entity("Bulk Owner A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Bulk Owner B", _axis_vector(1))

        results = bulk_store_relations(
            relations=[
                {
                    "source_id": a,
                    "target_id": b,
                    "predicate": "elaborates_on",
                    "override_justification": "bulk relation owner attribution regression coverage",
                }
            ],
            owner_id="agent_batch_owner",
            db_connection=self.conn,
        )

        self.assertEqual(results[0]["status"], "success", results)
        event = self.conn.execute(
            "SELECT agent_id FROM events WHERE type = 'relation_gate_override'"
        ).fetchone()
        self.assertEqual(event[0], "agent_batch_owner")

    def test_store_relation_override_audit_event_defaults_to_system_owner(self):
        a, _ = self._mk_vector_entity("Owner Default A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Owner Default B", _axis_vector(1))

        store_relation(
            source_id=a,
            target_id=b,
            predicate="elaborates_on",
            override_justification="checking omitted owner_id defaults the audit event to system",
            db_connection=self.conn,
        )

        event = self.conn.execute(
            "SELECT agent_id FROM events WHERE type = 'relation_gate_override'"
        ).fetchone()
        self.assertEqual(event[0], "system")

    def test_store_relation_gate_skipped_for_existing_duplicate_edge(self):
        """[R2 fix #4] regression: an existing active identical edge is a no-op, checked BEFORE
        the gate -- re-submitting it (even a low-similarity strong-predicate edge accepted
        before this gate existed) must never demand an override or emit an audit event."""
        a, _ = self._mk_vector_entity("Dup Edge A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Dup Edge B", _axis_vector(1))  # low sim, seeded directly

        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)"
            " VALUES (?, ?, ?, 'elaborates_on', ?, ?, ?)",
            (str(uuid.uuid4()), a, b, now, now, now),
        )
        self.conn.commit()

        res = store_relation(
            source_id=a, target_id=b, predicate="elaborates_on", db_connection=self.conn
        )
        self.assertTrue(res.startswith("Relation already exists (no-op)"), res)
        self.assertEqual(self._override_events(), [])

    def test_bulk_store_relations_gate_applies_per_item_and_aborts_batch(self):
        a, _ = self._mk_vector_entity("Bulk Gate A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Bulk Gate B", _axis_vector(1))
        c, _ = self._mk_vector_entity("Bulk Gate C", _axis_vector(2))
        d, _ = self._mk_vector_entity("Bulk Gate D", _axis_vector(3))

        batch = [
            {"source_id": a, "target_id": b, "predicate": "depends_on"},  # ungated, would pass
            {"source_id": c, "target_id": d, "predicate": "elaborates_on"},  # low sim, no override
        ]
        results = bulk_store_relations(relations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["status"], "error", results)

        # All-or-nothing: item 1's own edge must have been rolled back too.
        row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id = ? AND target_id = ?", (a, b)
        ).fetchone()
        self.assertIsNone(row)

    def test_bulk_store_relations_duplicate_item_resolves_as_no_op_alongside_passing_item(self):
        """[R2 fix #4] extension: a batch item that duplicates an already-active edge resolves
        as a no-op (not a gate failure), so a batch mixing it with a genuinely passing item
        still succeeds as a whole."""
        a, _ = self._mk_vector_entity("Bulk Dup A", _axis_vector(0))
        b, _ = self._mk_vector_entity("Bulk Dup B", _axis_vector(1))  # low sim, seeded directly
        e, _ = self._mk_vector_entity("Bulk Passing E", _axis_vector(2))
        f, _ = self._mk_vector_entity("Bulk Passing F", _axis_vector(2))  # identical -> sim=1.0

        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_at)"
            " VALUES (?, ?, ?, 'elaborates_on', ?, ?, ?)",
            (str(uuid.uuid4()), a, b, now, now, now),
        )
        self.conn.commit()

        batch = [
            {"source_id": a, "target_id": b, "predicate": "elaborates_on"},  # duplicate -> no-op
            {"source_id": e, "target_id": f, "predicate": "resolves"},  # passes gate cleanly
        ]
        results = bulk_store_relations(relations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 2, results)
        self.assertEqual(results[0]["status"], "duplicate", results)
        self.assertEqual(results[1]["status"], "success", results)
        self.assertEqual(self._override_events(), [])


if __name__ == "__main__":
    unittest.main()
