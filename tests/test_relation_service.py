import unittest
import tempfile
import os
import shutil
import sqlite3
import uuid
from saltmdb.db.schema import init_db
from saltmdb.db.connection import write_transaction_retrying
from saltmdb.domain.services.relation_service import (
    resolve_or_create_predicate,
    get_canonical_predicates,
    store_relation,
    bulk_store_relations,
)
from saltmdb.domain.services.memory_service import store_memory


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
        with write_transaction_retrying(self.conn):
            return resolve_or_create_predicate(self.conn, predicate_name, agent_id=agent_id)

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
        with write_transaction_retrying(self.conn):
            self.conn.execute(
                "INSERT INTO predicates (id, name, normalized_name, canonical_id) VALUES (?, 'Old Legacy Name', 'old_legacy_name', NULL)",
                (str(uuid.uuid4()),)
            )

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


if __name__ == "__main__":
    unittest.main()
