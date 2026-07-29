import unittest
import tempfile
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, UTC
from saltmdb.db.schema import init_db
from saltmdb.db.connection import write_transaction_retrying
from saltmdb.domain.services.relation_service import (
    resolve_or_create_predicate,
    get_canonical_predicates,
    store_relation,
    bulk_store_relations,
    analyze_dependencies,
    analyze_lineage,
    commit_consolidation,
    bulk_commit_consolidation,
)
from saltmdb.domain.services.memory_service import store_memory, detect_orphaned_memories


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
        self.assertEqual(len(matching), 1, "idx_relations_unique_edge must exist on relations after init_db()")
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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

    def test_expired_and_active_row_for_identical_tuple_both_insert(self):
        src = self._mk_entity("Partial Idx Src")
        tgt = self._mk_entity("Partial Idx Tgt")
        past = "2020-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-partial', ?)",
            (str(uuid.uuid4()), src, tgt, past)
        )
        # An active row (valid_to NULL) for the SAME tuple must also succeed under the partial index.
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-partial', NULL)",
            (str(uuid.uuid4()), src, tgt)
        )
        count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=? AND predicate='pred-partial'", (src, tgt)
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_two_active_rows_for_identical_tuple_raises_integrity_error(self):
        src = self._mk_entity("Active Idx Src")
        tgt = self._mk_entity("Active Idx Tgt")
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-active', NULL)",
            (str(uuid.uuid4()), src, tgt)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate, valid_to) VALUES (?, ?, ?, 'pred-active', NULL)",
                (str(uuid.uuid4()), src, tgt)
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
                    "WHERE valid_to IS NULL", row[0],
                    "init_db() must unconditionally DROP+CREATE the old non-partial index into the partial form"
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
                ("rel-earliest",)
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                ("rel-middle",)
            )
            raw_conn.execute(
                "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                ("rel-latest",)
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
                    len(rows), 1,
                    "dedup backfill must collapse all duplicate (source_id, target_id, predicate) rows to exactly one"
                )
                self.assertEqual(
                    rows[0][0], "rel-earliest",
                    "the surviving row must be the earliest-inserted one (by rowid), not an arbitrary one"
                )

                # The unique index must now genuinely be enforced against fresh duplicate inserts.
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO relations (id, source_id, target_id, predicate) VALUES (?, 'src-x', 'tgt-x', 'dup_pred')",
                        (str(uuid.uuid4()),)
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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for relation dedup tests",
            title="Relation Dedup Target",
            owner_id="tester",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.id1 = res1.split("ID: ")[1].strip()
        self.id2 = res2.split("ID: ")[1].strip()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _relation_count(self, source_id, target_id, predicate=None):
        if predicate:
            return self.conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
                (source_id, target_id, predicate)
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id = ? AND target_id = ?",
            (source_id, target_id)
        ).fetchone()[0]

    def test_duplicate_call_is_noop_and_reports_same_existing_id(self):
        res1 = store_relation(source_id=self.id1, target_id=self.id2, predicate="dup_test_predicate", db_connection=self.conn)
        self.assertIn("successfully stored", res1)
        self.assertFalse(res1.startswith("Error"))
        id_in_res1 = res1.split("ID: ")[1].rstrip(")").strip()

        res2 = store_relation(source_id=self.id1, target_id=self.id2, predicate="dup_test_predicate", db_connection=self.conn)
        self.assertIn("already exists", res2)
        self.assertFalse(res2.startswith("Error"))
        id_in_res2 = res2.split("ID: ")[1].rstrip(")").strip()

        self.assertEqual(id_in_res1, id_in_res2, "the dup no-op must report the SAME existing relation id as the original insert")
        self.assertEqual(self._relation_count(self.id1, self.id2, "dup_test_predicate"), 1)

    def test_same_pair_different_predicates_both_persist(self):
        store_relation(source_id=self.id1, target_id=self.id2, predicate="predicate_alpha", db_connection=self.conn)
        store_relation(source_id=self.id1, target_id=self.id2, predicate="predicate_beta", db_connection=self.conn)
        self.assertEqual(
            self._relation_count(self.id1, self.id2), 2,
            "two different predicates between the same source/target pair must NOT be deduped against each other"
        )

    def test_bulk_store_relations_marks_duplicate_status(self):
        store_relation(source_id=self.id1, target_id=self.id2, predicate="already_there", db_connection=self.conn)

        results = bulk_store_relations(
            relations=[{"source_id": self.id1, "target_id": self.id2, "predicate": "already_there"}],
            db_connection=self.conn,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "duplicate")
        self.assertEqual(self._relation_count(self.id1, self.id2, "already_there"), 1)

    def test_store_relation_already_exists_message_references_active_row_not_expired_one(self):
        # Manually plant an EXPIRED row for the tuple first (stale/historical data).
        expired_id = str(uuid.uuid4())
        past = "2020-01-01T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from, valid_to) "
            "VALUES (?, ?, ?, 'stale_lookup_pred', ?, ?, ?)",
            (expired_id, self.id1, self.id2, past, past, past)
        )

        active_res = store_relation(source_id=self.id1, target_id=self.id2, predicate="stale_lookup_pred", db_connection=self.conn)
        self.assertIn("successfully stored", active_res)
        active_id = active_res.split("ID: ")[1].rstrip(")").strip()
        self.assertNotEqual(active_id, expired_id)

        dup_res = store_relation(source_id=self.id1, target_id=self.id2, predicate="stale_lookup_pred", db_connection=self.conn)
        self.assertIn("already exists", dup_res)
        reported_id = dup_res.split("ID: ")[1].rstrip(")").strip()
        self.assertEqual(
            reported_id, active_id,
            "the already-exists no-op message must reference the ACTIVE row's ID, not the expired one"
        )
        self.assertNotEqual(reported_id, expired_id)


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
            "resolves", "depends_on", "references", "elaborates_on",
            "consolidated_from", "supersedes", "relates_to",
        }
        self.assertTrue(expected.issubset(names))

    def test_relates_to_and_references_alias_to_elaborates_on(self):
        self.assertEqual(self._resolve("relates_to"), "elaborates_on")
        self.assertEqual(self._resolve("references"), "elaborates_on")

        row_relates_to = self.conn.execute(
            "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id WHERE p.name = 'relates_to'"
        ).fetchone()
        self.assertIsNotNone(row_relates_to)
        self.assertEqual(row_relates_to[0], "elaborates_on")

        row_references = self.conn.execute(
            "SELECT c.name FROM predicates p JOIN predicates c ON c.id = p.canonical_id WHERE p.name = 'references'"
        ).fetchone()
        self.assertIsNotNone(row_references)
        self.assertEqual(row_references[0], "elaborates_on")

    def test_idempotent_repeated_calls_return_same_canonical_name(self):
        first = self._resolve("brand_new_predicate_idem")
        second = self._resolve("brand_new_predicate_idem")
        self.assertEqual(first, second)

        rows = self.conn.execute(
            "SELECT id FROM predicates WHERE name = 'brand_new_predicate_idem'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "repeated resolution of the same input must not create duplicate predicate rows")

    def test_alias_input_returns_canonical_name_not_alias_name(self):
        resolved = self._resolve("relates_to")
        self.assertEqual(resolved, "elaborates_on")
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
                (str(uuid.uuid4()),)
            )
        write_transaction_retrying(self.conn, _write)

        resolved = self._resolve("OLD LEGACY NAME")
        self.assertEqual(
            resolved, "Old Legacy Name",
            "normalized_name fallback must return the row's ORIGINAL name string unchanged, not silently rename it"
        )

    def test_unrecognized_predicate_is_auto_created(self):
        resolved = self._resolve("totally_new_predicate_xyz")
        self.assertEqual(resolved, "totally_new_predicate_xyz")

        row = self.conn.execute(
            "SELECT id FROM predicates WHERE name = 'totally_new_predicate_xyz'"
        ).fetchone()
        self.assertIsNotNone(row, "an unrecognized predicate must be auto-created (non-blocking), not rejected")

    def test_new_predicate_creation_populates_normalized_name(self):
        self._resolve("another_fresh_predicate")
        row = self.conn.execute(
            "SELECT normalized_name FROM predicates WHERE name = 'another_fresh_predicate'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "another_fresh_predicate")

    def test_empty_or_punctuation_only_input_returns_none(self):
        self.assertIsNone(self._resolve("   "))
        self.assertIsNone(self._resolve("!!!"))

    def test_store_relation_with_degenerate_predicate_still_succeeds_and_stores_raw_string(self):
        res1 = store_memory(
            content="Source entity content for degenerate predicate test",
            title="Degenerate Predicate Source",
            owner_id="tester",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        res2 = store_memory(
            content="Target entity content for degenerate predicate test",
            title="Degenerate Predicate Target",
            owner_id="tester",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        id1 = res1.split("ID: ")[1].strip()
        id2 = res2.split("ID: ")[1].strip()

        result = store_relation(source_id=id1, target_id=id2, predicate="!!!", db_connection=self.conn)
        self.assertIn("successfully stored", result, "a degenerate predicate must not block relation storage")
        self.assertFalse(result.startswith("Error"))

        row = self.conn.execute(
            "SELECT predicate FROM relations WHERE source_id = ? AND target_id = ?", (id1, id2)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row[0], "!!!",
            "when resolve_or_create_predicate returns None, the raw input string must be stored as-is (the 'or predicate' fallback)"
        )


class TestGetCanonicalPredicates(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_db_excludes_aliased_predicates(self):
        results = get_canonical_predicates(db_connection=self.conn)
        names = {r["name"] for r in results}
        self.assertEqual(
            names,
            {"resolves", "depends_on", "elaborates_on", "consolidated_from", "supersedes"},
            "relates_to/references must be excluded from canonical predicates since they alias elaborates_on"
        )

    def test_query_filters_to_matching_predicate(self):
        results = get_canonical_predicates(query="depend", db_connection=self.conn)
        names = {r["name"] for r in results}
        self.assertEqual(names, {"depends_on"})


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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

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
            f"SELECT id, source_id, target_id, predicate FROM relations WHERE {' AND '.join(clauses)}", params
        ).fetchall()

    def test_basic_repoint_original_row_preserved_and_new_active_row_created(self):
        p1 = self._mk("Repoint P1")
        x = self._mk("Repoint X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)

        orig_row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'", (p1, x)
        ).fetchone()
        self.assertIsNotNone(orig_row)
        orig_id = orig_row[0]

        res = commit_consolidation(
            parent_ids=[p1], title="C Basic Repoint", content=_cons_content("basic-repoint"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res)
        c_id = res.split("ID: ")[1].strip()

        row = self.conn.execute("SELECT source_id, valid_to FROM relations WHERE id=?", (orig_id,)).fetchone()
        self.assertEqual(row[0], p1, "original row's source_id must remain unchanged (still P1)")
        self.assertIsNotNone(row[1], "original row's valid_to must now be set (expired)")

        new_active = self._active_relations(source_id=c_id, target_id=x, predicate="depends_on")
        self.assertEqual(len(new_active), 1, "a NEW active C -> X row must exist")

    def test_dedup_two_parents_same_target_produces_single_active_row(self):
        p1 = self._mk("Dedup P1")
        p2 = self._mk("Dedup P2")
        x = self._mk("Dedup X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)
        store_relation(source_id=p2, target_id=x, predicate="depends_on", db_connection=self.conn)

        res = commit_consolidation(
            parent_ids=[p1, p2], title="C Dedup", content=_cons_content("dedup"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res)
        c_id = res.split("ID: ")[1].strip()

        active = self._active_relations(target_id=x, predicate="depends_on")
        self.assertEqual(len(active), 1, "exactly one active C->X row must exist, not two")
        self.assertEqual(active[0][1], c_id)

        old_rows = self.conn.execute(
            "SELECT source_id, valid_to FROM relations WHERE target_id=? AND predicate='depends_on' AND source_id IN (?, ?)",
            (x, p1, p2)
        ).fetchall()
        self.assertEqual(len(old_rows), 2, "both P1's and P2's original rows must still exist (expired, not deleted)")
        for _src, valid_to in old_rows:
            self.assertIsNotNone(valid_to)

    def test_self_loop_between_both_consolidated_parents_expires_with_no_self_loop_row(self):
        p1 = self._mk("SelfLoop P1")
        p2 = self._mk("SelfLoop P2")
        store_relation(source_id=p1, target_id=p2, predicate="depends_on", db_connection=self.conn)

        orig_row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'", (p1, p2)
        ).fetchone()
        self.assertIsNotNone(orig_row)

        res = commit_consolidation(
            parent_ids=[p1, p2], title="C Self Loop", content=_cons_content("self-loop"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res)
        c_id = res.split("ID: ")[1].strip()

        row = self.conn.execute("SELECT valid_to FROM relations WHERE id=?", (orig_row[0],)).fetchone()
        self.assertIsNotNone(row[0], "original P1->P2 row must still be expired (history preserved)")

        self_loop_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=?", (c_id, c_id)
        ).fetchone()[0]
        self.assertEqual(self_loop_count, 0, "no C->C self-loop row should ever be created")

    def test_multi_generation_consolidated_from_edges_are_not_repointed(self):
        a = self._mk("MultiGen A")
        b = self._mk("MultiGen B")
        d = self._mk("MultiGen D")

        res1 = commit_consolidation(
            parent_ids=[a, b], title="C1 MultiGen", content=_cons_content("multigen-c1"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res1)
        c1 = res1.split("ID: ")[1].strip()

        c1_to_a = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='consolidated_from'", (c1, a)
        ).fetchone()
        c1_to_b = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='consolidated_from'", (c1, b)
        ).fetchone()
        self.assertIsNotNone(c1_to_a)
        self.assertIsNotNone(c1_to_b)

        res2 = commit_consolidation(
            parent_ids=[c1, d], title="C2 MultiGen", content=_cons_content("multigen-c2"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res2)
        c2 = res2.split("ID: ")[1].strip()

        c1_to_a_after = self.conn.execute("SELECT source_id, valid_to FROM relations WHERE id=?", (c1_to_a[0],)).fetchone()
        c1_to_b_after = self.conn.execute("SELECT source_id, valid_to FROM relations WHERE id=?", (c1_to_b[0],)).fetchone()
        self.assertEqual(c1_to_a_after[0], c1, "C1's own consolidated_from edge to A must NOT be repointed by the 2nd consolidation")
        self.assertIsNone(c1_to_a_after[1], "C1's consolidated_from edge to A must remain active (not expired)")
        self.assertEqual(c1_to_b_after[0], c1)
        self.assertIsNone(c1_to_b_after[1])

        new_edge_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=? AND predicate='consolidated_from' AND valid_to IS NULL",
            (c2, c1)
        ).fetchone()[0]
        self.assertEqual(new_edge_count, 1, "a new C2 -consolidated_from-> C1 edge must exist")

    def test_exclusion_is_predicate_scoped_not_parent_scoped(self):
        a = self._mk("PredScope A")
        b = self._mk("PredScope B")
        d2 = self._mk("PredScope D2")
        y = self._mk("PredScope Y")

        res1 = commit_consolidation(
            parent_ids=[a, b], title="C1 PredScope", content=_cons_content("predscope-c1"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res1)
        c1 = res1.split("ID: ")[1].strip()

        # Give C1 an unrelated, non-consolidated_from edge.
        store_relation(source_id=c1, target_id=y, predicate="depends_on", db_connection=self.conn)
        c1_to_y_row = self.conn.execute(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'", (c1, y)
        ).fetchone()
        self.assertIsNotNone(c1_to_y_row)

        res3 = commit_consolidation(
            parent_ids=[c1, d2], title="C3 PredScope", content=_cons_content("predscope-c3"),
            owner_id="agent_c", db_connection=self.conn
        )
        self.assertIn("Successfully committed", res3)
        c3 = res3.split("ID: ")[1].strip()

        old_row = self.conn.execute("SELECT valid_to FROM relations WHERE id=?", (c1_to_y_row[0],)).fetchone()
        self.assertIsNotNone(old_row[0], "the depends_on edge (NOT consolidated_from) must be expired by the re-merge")

        new_row_count = self.conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on' AND valid_to IS NULL",
            (c3, y)
        ).fetchone()[0]
        self.assertEqual(
            new_row_count, 1,
            "the depends_on edge must be repointed to C3 -> Y, proving the exclusion is predicate-scoped (consolidated_from only), not parent-scoped"
        )

    def test_bulk_commit_consolidation_rollback_leaves_no_relations_repointed(self):
        p1 = self._mk("Bulk P1")
        x = self._mk("Bulk X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)
        orig_row = self.conn.execute(
            "SELECT id, valid_to FROM relations WHERE source_id=? AND target_id=? AND predicate='depends_on'", (p1, x)
        ).fetchone()
        self.assertIsNone(orig_row[1])

        p2 = self._mk("Bulk P2 (forced reject)")
        # Second item uses the exact fluff phrase that TC-CONS-01 (test_consolidation_quality.py)
        # proves triggers a REJECT from the quality gate, forcing the whole batch to roll back.
        batch = [
            {"parent_ids": [p1], "title": "Bulk Item 1 (would succeed alone)", "content": _cons_content("bulk-item1")},
            {"parent_ids": [p2], "title": "Bulk Item 2 (forced reject)", "content": "consolidated these files."},
        ]
        results = bulk_commit_consolidation(consolidations=batch, db_connection=self.conn)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

        row_after = self.conn.execute("SELECT valid_to FROM relations WHERE id=?", (orig_row[0],)).fetchone()
        self.assertIsNone(row_after[0], "batch rollback must leave item 1's relation un-repointed/un-expired")

        consolidated_count = self.conn.execute("SELECT COUNT(*) FROM entities WHERE status='consolidated'").fetchone()[0]
        self.assertEqual(consolidated_count, 0, "no consolidated entity should exist after an all-or-nothing rollback")


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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

    def test_three_generation_chain_and_no_parent_ids_key(self):
        a = self._mk("Lineage A")
        b = self._mk("Lineage B")
        d = self._mk("Lineage D")

        res1 = commit_consolidation(
            parent_ids=[a, b], title="Lineage C1", content=_cons_content("lineage-c1"),
            owner_id="agent_c", db_connection=self.conn
        )
        c1 = res1.split("ID: ")[1].strip()
        res2 = commit_consolidation(
            parent_ids=[c1, d], title="Lineage C2", content=_cons_content("lineage-c2"),
            owner_id="agent_c", db_connection=self.conn
        )
        c2 = res2.split("ID: ")[1].strip()

        result = analyze_lineage(entity_id=c2, db_connection=self.conn)
        self.assertNotIn("error", result)
        ancestors = result["ancestors"]
        by_id = {entry["id"]: entry for entry in ancestors}

        self.assertIn(c1, by_id)
        self.assertEqual(by_id[c1]["generation_depth"], 1)
        self.assertIn(d, by_id)
        self.assertEqual(by_id[d]["generation_depth"], 1)
        self.assertIn(a, by_id)
        self.assertEqual(by_id[a]["generation_depth"], 2)
        self.assertIn(b, by_id)
        self.assertEqual(by_id[b]["generation_depth"], 2)

        for entry in ancestors:
            self.assertNotIn("parent_ids", entry, "per-ancestor dicts must not contain a parent_ids key")

    def test_cycle_guard_terminates(self):
        x = self._mk("Cycle X")
        y = self._mk("Cycle Y")
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
            "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
            (str(uuid.uuid4()), x, y, now, now)
        )
        self.conn.execute(
            "INSERT INTO relations (id, source_id, target_id, predicate, created_at, valid_from) "
            "VALUES (?, ?, ?, 'consolidated_from', ?, ?)",
            (str(uuid.uuid4()), y, x, now, now)
        )
        result = analyze_lineage(entity_id=x, db_connection=self.conn)
        self.assertNotIn("error", result)
        self.assertIn("ancestors", result)
        self.assertLessEqual(
            len(result["ancestors"]), 12,
            "cycle guard (path-based dedup + depth<10 cap) must bound traversal, not loop forever"
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
                (str(uuid.uuid4()), src, tgt, now, now)
            )

        result = analyze_lineage(entity_id=c2, db_connection=self.conn)
        ancestors = result["ancestors"]
        z_occurrences = [entry for entry in ancestors if entry["id"] == z]
        self.assertEqual(len(z_occurrences), 1, "Z must appear exactly once, deduped, not once per path")
        self.assertEqual(z_occurrences[0]["generation_depth"], 1, "diamond ancestry must dedupe to the SHALLOWEST depth")


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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

    def _now(self):
        return datetime.now(UTC).isoformat()

    def test_analyze_dependencies_excludes_edge_created_after_point_in_time(self):
        s = self._mk("PIT Deps S")
        t = self._mk("PIT Deps T")
        pit_before = self._now()
        time.sleep(1.1)
        store_relation(source_id=s, target_id=t, predicate="depends_on", db_connection=self.conn)

        before_result = analyze_dependencies(root_entity_id=s, point_in_time=pit_before, db_connection=self.conn)
        self.assertEqual(before_result["total_dependencies_found"], 0)

        now_result = analyze_dependencies(root_entity_id=s, db_connection=self.conn)
        self.assertEqual(now_result["total_dependencies_found"], 1)

    def test_analyze_dependencies_shows_expired_edge_at_earlier_pit_and_repointed_edge_at_now(self):
        p1 = self._mk("PIT Cons P1")
        x = self._mk("PIT Cons X")
        store_relation(source_id=p1, target_id=x, predicate="depends_on", db_connection=self.conn)

        pit_before_consolidation = self._now()
        time.sleep(1.1)

        res = commit_consolidation(
            parent_ids=[p1], title="PIT Cons C", content=_cons_content("pit-deps-cons"),
            owner_id="agent_c", db_connection=self.conn
        )
        c_id = res.split("ID: ")[1].strip()

        historical = analyze_dependencies(root_entity_id=p1, point_in_time=pit_before_consolidation, db_connection=self.conn)
        self.assertEqual(historical["total_dependencies_found"], 1, "edge valid at pit_before_consolidation must still appear")

        current_from_p1 = analyze_dependencies(root_entity_id=p1, db_connection=self.conn)
        self.assertEqual(current_from_p1["total_dependencies_found"], 0, "expired edge must not appear with no point_in_time (now)")

        current_from_c = analyze_dependencies(root_entity_id=c_id, db_connection=self.conn)
        # Root C now also carries its own outgoing 'consolidated_from' edge to P1 (created by
        # commit_consolidation itself), so total_dependencies_found is 2 (C->P1, C->X), not 1 --
        # assert on the specific repointed node rather than the raw total.
        dep_ids = {n["id"] for n in current_from_c["dependencies"]}
        self.assertIn(x, dep_ids, "the new repointed edge C->X must appear at now")
        x_node = next(n for n in current_from_c["dependencies"] if n["id"] == x)
        self.assertEqual(x_node["depth"], 1)

    def test_analyze_lineage_excludes_edge_created_after_point_in_time(self):
        a = self._mk("PIT Lineage A")
        b = self._mk("PIT Lineage B")
        pit_before = self._now()
        time.sleep(1.1)
        res = commit_consolidation(
            parent_ids=[a, b], title="PIT Lineage C1", content=_cons_content("pit-lineage-c1"),
            owner_id="agent_c", db_connection=self.conn
        )
        c1 = res.split("ID: ")[1].strip()

        historical = analyze_lineage(entity_id=c1, point_in_time=pit_before, db_connection=self.conn)
        self.assertEqual(historical["total_ancestors"], 0, "consolidated_from edges created after pit_before must not appear")

        current = analyze_lineage(entity_id=c1, db_connection=self.conn)
        self.assertEqual(current["total_ancestors"], 2)

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
            (rel_id, z, w, t0, t0)
        )
        pit_while_active = self._now()
        time.sleep(1.1)
        expire_at = self._now()
        self.conn.execute("UPDATE relations SET valid_to = ? WHERE id = ?", (expire_at, rel_id))

        historical = analyze_lineage(entity_id=z, point_in_time=pit_while_active, db_connection=self.conn)
        self.assertEqual(historical["total_ancestors"], 1, "edge was active as of pit_while_active, must still show")

        current = analyze_lineage(entity_id=z, db_connection=self.conn)
        self.assertEqual(current["total_ancestors"], 0, "now-expired edge must not appear with no point_in_time")

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
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        return res.split("ID: ")[1].strip()

    def test_entity_whose_only_relation_is_expired_is_flagged_orphan(self):
        e1 = self._mk("Orphan E1")
        e2 = self._mk("Orphan E2")
        store_relation(source_id=e1, target_id=e2, predicate="depends_on", db_connection=self.conn)
        self.conn.execute(
            "UPDATE relations SET valid_to = ? WHERE source_id = ? AND target_id = ?",
            ("2020-01-01T00:00:00+00:00", e1, e2)
        )
        result = detect_orphaned_memories(owner_id="orphan_tester", db_connection=self.conn)
        orphan_ids = {o["id"] for o in result["orphaned_memories"]}
        self.assertIn(e1, orphan_ids, "an entity whose only relation is expired must be flagged as an orphan")
        self.assertIn(e2, orphan_ids)

    def test_entity_with_active_relation_is_not_flagged_orphan(self):
        e3 = self._mk("Orphan E3")
        e4 = self._mk("Orphan E4")
        store_relation(source_id=e3, target_id=e4, predicate="depends_on", db_connection=self.conn)
        result = detect_orphaned_memories(owner_id="orphan_tester", db_connection=self.conn)
        orphan_ids = {o["id"] for o in result["orphaned_memories"]}
        self.assertNotIn(e3, orphan_ids, "an entity with an active relation must NOT be flagged as an orphan")
        self.assertNotIn(e4, orphan_ids)


if __name__ == "__main__":
    unittest.main()
