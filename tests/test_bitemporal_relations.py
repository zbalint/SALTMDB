import unittest
import tempfile
import os
import shutil
from saltmdb.db.schema import init_db
from saltmdb.domain.services.relation_service import (
    store_relation,
    invalidate_relation,
    analyze_dependencies,
    analyze_lineage,
)
from saltmdb.domain.services.memory_service import store_memory


class TestBitemporalRelationsAndCanonicalTags(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

        # Store two entities to use for relation tests
        m1 = store_memory(
            title="[SALTMDB] Entity Alpha",
            content="# Entity Alpha\n\nContent for alpha entity.",
            owner_id="user1",
            db_connection=self.conn,
        )
        m2 = store_memory(
            title="[SALTMDB] Entity Beta",
            content="# Entity Beta\n\nContent for beta entity.",
            owner_id="user1",
            db_connection=self.conn,
        )
        self.id_alpha = m1.split("ID: ")[1].split()[0]
        self.id_beta = m2.split("ID: ")[1].split()[0]

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_schema_has_valid_at_and_invalid_at_columns(self):
        cursor = self.conn.execute("PRAGMA table_info(relations)")
        cols = [row[1] for row in cursor.fetchall()]
        self.assertIn("valid_at", cols)
        self.assertIn("invalid_at", cols)

    def test_store_relation_default_valid_at(self):
        res = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Relation successfully stored"))
        rel_id = res.split("ID: ")[1].rstrip(")")

        row = self.conn.execute(
            "SELECT created_at, valid_from, valid_at FROM relations WHERE id = ?", (rel_id,)
        ).fetchone()
        created_at, valid_from, valid_at = row
        self.assertIsNotNone(valid_at)
        self.assertEqual(valid_at, valid_from)
        self.assertEqual(valid_at, created_at)

    def test_store_relation_explicit_valid_at(self):
        custom_time = "2025-01-01T00:00:00+00:00"
        res = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            valid_at=custom_time,
            db_connection=self.conn,
        )
        self.assertTrue(res.startswith("Relation successfully stored"))
        rel_id = res.split("ID: ")[1].rstrip(")")

        row = self.conn.execute("SELECT valid_at FROM relations WHERE id = ?", (rel_id,)).fetchone()
        self.assertEqual(row[0], custom_time)

    def test_store_relation_duplicate_noop_preserves_valid_at(self):
        custom_time = "2025-01-01T00:00:00+00:00"
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            valid_at=custom_time,
            db_connection=self.conn,
        )
        # Call store_relation again with same tuple and a different valid_at
        res_dup = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            valid_at="2026-12-31T23:59:59+00:00",
            db_connection=self.conn,
        )
        self.assertTrue(res_dup.startswith("Relation already exists (no-op)"))

        row = self.conn.execute(
            "SELECT valid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
            (self.id_alpha, self.id_beta, "depends_on"),
        ).fetchone()
        self.assertEqual(row[0], custom_time)

    def test_invalidate_relation_sets_invalid_at_and_valid_to(self):
        res_store = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        rel_id = res_store.split("ID: ")[1].rstrip(")")

        res_inv = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res_inv.startswith("Relation invalidated"))
        self.assertIn(rel_id, res_inv)

        row = self.conn.execute(
            "SELECT invalid_at, valid_to FROM relations WHERE id = ?", (rel_id,)
        ).fetchone()
        self.assertIsNotNone(row[0])  # invalid_at set
        self.assertIsNotNone(row[1])  # valid_to set to effective_invalid_at
        self.assertEqual(row[0], row[1])

    def test_invalidate_relation_explicit_invalid_at(self):
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        custom_inv_time = "2025-06-15T12:00:00+00:00"
        res_inv = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            invalid_at=custom_inv_time,
            db_connection=self.conn,
        )
        self.assertTrue(res_inv.startswith("Relation invalidated"))
        self.assertIn(custom_inv_time, res_inv)

        row = self.conn.execute(
            "SELECT invalid_at FROM relations WHERE source_id = ? AND target_id = ?",
            (self.id_alpha, self.id_beta),
        ).fetchone()
        self.assertEqual(row[0], custom_inv_time)

    def test_invalidate_relation_default_invalid_at(self):
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        res_inv = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res_inv.startswith("Relation invalidated"))
        row = self.conn.execute(
            "SELECT invalid_at FROM relations WHERE source_id = ? AND target_id = ?",
            (self.id_alpha, self.id_beta),
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_invalidate_relation_twice_is_noop(self):
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        custom_time = "2025-05-01T00:00:00+00:00"
        res1 = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            invalid_at=custom_time,
            db_connection=self.conn,
        )
        self.assertTrue(res1.startswith("Relation invalidated"))

        res2 = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            invalid_at="2026-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.assertTrue(res2.startswith("Relation already invalidated (no-op)"))
        self.assertIn(custom_time, res2)

        row = self.conn.execute(
            "SELECT invalid_at FROM relations WHERE source_id = ? AND target_id = ?",
            (self.id_alpha, self.id_beta),
        ).fetchone()
        self.assertEqual(row[0], custom_time)

    def test_invalidate_relation_errors(self):
        # (a) Tuple that was never stored
        res1 = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="nonexistent_predicate",
            db_connection=self.conn,
        )
        self.assertEqual(res1, "Error: relation not found")

        # (b) Tuple whose edge has valid_to set (system expired / consolidated)
        res_store = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="resolves",
            db_connection=self.conn,
        )
        rel_id = res_store.split("ID: ")[1].rstrip(")")
        self.conn.execute(
            "UPDATE relations SET valid_to = '2025-01-01T00:00:00' WHERE id = ?", (rel_id,)
        )

        res2 = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="resolves",
            db_connection=self.conn,
        )
        self.assertEqual(res2, "Error: relation not found")

    def test_invalidate_relation_canonical_alias_matching(self):
        # references is an alias for canonical predicate elaborates_on
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="elaborates_on",
            db_connection=self.conn,
        )
        # Call invalidate passing alias 'references'
        res_inv = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="references",
            db_connection=self.conn,
        )
        self.assertTrue(res_inv.startswith("Relation invalidated"))
        self.assertIn("elaborates_on", res_inv)

        row = self.conn.execute(
            "SELECT invalid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
            (self.id_alpha, self.id_beta, "elaborates_on"),
        ).fetchone()
        self.assertIsNotNone(row[0])

    def test_invalidated_edge_excluded_from_traversal_after_invalidation_timestamp(self):
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            valid_at="2025-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            invalid_at="2025-06-01T00:00:00+00:00",
            db_connection=self.conn,
        )

        res_dep = analyze_dependencies(
            root_entity_id=self.id_alpha,
            point_in_time="2025-07-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.assertEqual(res_dep["total_dependencies_found"], 0)
        self.assertEqual(len(res_dep["dependencies"]), 1)

        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="consolidated_from",
            valid_at="2025-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="consolidated_from",
            invalid_at="2025-06-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        res_lin = analyze_lineage(
            entity_id=self.id_alpha,
            point_in_time="2025-07-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.assertEqual(len(res_lin["ancestors"]), 1)

    def test_invalidated_edge_included_in_traversal_before_invalidation_timestamp(self):
        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            valid_at="2025-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.conn.execute(
            "UPDATE relations SET valid_from = '2025-01-01T00:00:00+00:00' WHERE source_id = ? AND target_id = ?",
            (self.id_alpha, self.id_beta),
        )
        invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            invalid_at="2025-06-01T00:00:00+00:00",
            db_connection=self.conn,
        )

        res_dep = analyze_dependencies(
            root_entity_id=self.id_alpha,
            point_in_time="2025-03-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.assertEqual(res_dep["total_dependencies_found"], 1)
        self.assertEqual(len(res_dep["dependencies"]), 2)
        dep_ids = {d["id"] for d in res_dep["dependencies"]}
        self.assertIn(self.id_beta, dep_ids)

        store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="consolidated_from",
            valid_at="2025-01-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.conn.execute(
            "UPDATE relations SET valid_from = '2025-01-01T00:00:00+00:00' WHERE source_id = ? AND target_id = ? AND predicate = 'consolidated_from'",
            (self.id_alpha, self.id_beta),
        )
        invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="consolidated_from",
            invalid_at="2025-06-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        res_lin = analyze_lineage(
            entity_id=self.id_alpha,
            point_in_time="2025-03-01T00:00:00+00:00",
            db_connection=self.conn,
        )
        self.assertEqual(len(res_lin["ancestors"]), 2)
        ancestry_ids = {a["id"] for a in res_lin["ancestors"]}
        self.assertIn(self.id_beta, ancestry_ids)

    def test_invalidate_relation_allows_recreating_same_edge(self):
        res1 = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res1.startswith("Relation successfully stored"))

        res_inv = invalidate_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res_inv.startswith("Relation invalidated"))

        res2 = store_relation(
            source_id=self.id_alpha,
            target_id=self.id_beta,
            predicate="depends_on",
            db_connection=self.conn,
        )
        self.assertTrue(res2.startswith("Relation successfully stored"))

        rows = self.conn.execute(
            "SELECT id, valid_to, invalid_at FROM relations WHERE source_id = ? AND target_id = ? AND predicate = ?",
            (self.id_alpha, self.id_beta, "depends_on"),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        active_rows = [r for r in rows if r[1] is None]
        invalid_rows = [r for r in rows if r[1] is not None]
        self.assertEqual(len(active_rows), 1)
        self.assertEqual(len(invalid_rows), 1)

    def test_canonical_tags_seeded_post_init_db(self):
        rows = self.conn.execute(
            "SELECT name, canonical_id FROM tags WHERE name IN ('episodic', 'semantic', 'procedural')"
        ).fetchall()
        names = {r[0] for r in rows}
        self.assertEqual(names, {"episodic", "semantic", "procedural"})
        for r in rows:
            self.assertIsNone(r[1], f"Tag '{r[0]}' canonical_id should be NULL")

    def test_init_db_idempotency_for_seeded_tags(self):
        # Re-run init_db on existing connection/DB
        init_db(self.db_path)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM tags WHERE name IN ('episodic', 'semantic', 'procedural')"
        ).fetchone()[0]
        self.assertEqual(count, 3)

    def test_store_memory_resolves_to_preseeded_canonical_tag(self):
        res = store_memory(
            title="[SALTMDB] Episodic Memory Test",
            content="# Episodic Memory\n\nContent for episodic memory test.",
            tags=["episodic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        entity_id = res.split("ID: ")[1].split()[0]
        row = self.conn.execute(
            "SELECT t.name, t.canonical_id FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?",
            (entity_id,),
        ).fetchone()
        self.assertEqual(row[0], "episodic")
        self.assertIsNone(row[1])

        # Verify only 1 row exists in tags for 'episodic'
        tag_count = self.conn.execute(
            "SELECT COUNT(*) FROM tags WHERE name = 'episodic'"
        ).fetchone()[0]
        self.assertEqual(tag_count, 1)

    def test_memory_type_and_canonical_tag_simultaneous(self):
        res = store_memory(
            title="[SALTMDB] Hybrid Memory Type and Tag",
            content="# Hybrid Memory\n\nContent with memory_type and canonical tag.",
            memory_type="event",
            tags=["episodic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        entity_id = res.split("ID: ")[1].split()[0]
        mtype = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()[0]
        self.assertEqual(mtype, "event")

        tag_name = self.conn.execute(
            "SELECT t.name FROM entity_tags et JOIN tags t ON et.tag_id = t.id WHERE et.entity_id = ?",
            (entity_id,),
        ).fetchone()[0]
        self.assertEqual(tag_name, "episodic")

    def test_store_memory_nudge_suffix(self):
        # (a) Brand-new entity WITH tags -> receives tip suffix
        res1 = store_memory(
            title="[SALTMDB] Brand New Entity With Tags",
            content="# New Entity\n\nSome body text for new entity with tags.",
            tags=["semantic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        self.assertTrue(res1.startswith("Knowledge stored successfully with ID: "))
        self.assertIn(
            "[Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]",
            res1,
        )

        # (b) Brand-new entity WITHOUT tags -> no tip suffix
        res2 = store_memory(
            title="[SALTMDB] Brand New Entity Without Tags",
            content="# New Entity\n\nSome body text for new entity without tags.",
            tags=[],
            owner_id="user1",
            db_connection=self.conn,
        )
        self.assertTrue(res2.startswith("Knowledge stored successfully with ID: "))
        self.assertNotIn("[Tip: consider calling manage_relation", res2)

        # (c) Re-storing existing entity_id (update path) WITH tags -> NO tip suffix
        entity_id_1 = res1.split("ID: ")[1].split()[0]
        res3 = store_memory(
            entity_id=entity_id_1,
            title="[SALTMDB] Brand New Entity With Tags Updated",
            content="# New Entity Updated\n\nUpdated body text for new entity with tags.",
            tags=["semantic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        self.assertTrue(res3.startswith("Knowledge stored successfully with ID: "))
        self.assertNotIn("[Tip: consider calling manage_relation", res3)

    def test_store_memory_duplicate_warning_and_nudge_suffix_coexist(self):
        # First memory
        res1 = store_memory(
            title="[SALTMDB] Quantum Encryption Standard Protocol",
            content="# Quantum Encryption\n\nQuantum key distribution uses polarized photons to establish secure shared keys.",
            tags=["semantic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        res1.split("ID: ")[1].split()[0]

        # Second memory with different content hash and tags to verify success message and tip
        res2 = store_memory(
            title="[SALTMDB] Quantum Key Distribution Protocol Variant",
            content="# Quantum Encryption Variant\n\nQuantum key distribution utilizes polarized photons to establish safe shared keys.",
            tags=["semantic"],
            owner_id="user1",
            db_connection=self.conn,
        )
        prefix = "Knowledge stored successfully with ID: "
        self.assertTrue(res2.startswith(prefix))
        self.assertIn(
            "[Tip: consider calling manage_relation to link this to related entities/concepts you just stored.]",
            res2,
        )


if __name__ == "__main__":
    unittest.main()
