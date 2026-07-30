import unittest
import tempfile
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, UTC

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory, scan_memories, VALID_DOMAINS


class TestDomainStoreMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _domain_of(self, entity_id):
        row = self.conn.execute(
            "SELECT domain FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_all_five_valid_domains_round_trip(self):
        for d in VALID_DOMAINS:
            res = store_memory(
                content=f"Content for domain round trip test of domain {d}",
                title=f"Domain Round Trip {d}",
                owner_id="tester",
                domain=d,
                skip_duplicate_check=True,
                db_connection=self.conn,
            )
            self.assertFalse(res.startswith("Error"), f"store_memory failed for domain={d}: {res}")
            entity_id = res.split("ID: ")[1].strip()
            self.assertEqual(self._domain_of(entity_id), d)

    def test_invalid_domain_returns_error_string_not_exception(self):
        res = store_memory(
            content="Content for invalid domain test",
            title="Invalid Domain Entity",
            owner_id="tester",
            domain="NotARealDomain",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Error"), f"expected an Error string, got: {res}")
        self.assertIn("domain", res)

    def test_omitting_domain_on_fresh_insert_stores_null(self):
        res = store_memory(
            content="Content for omitted domain test on a brand-new entity",
            title="Omitted Domain Entity",
            owner_id="tester",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertIsNone(
            self._domain_of(entity_id),
            "omitting domain on a brand-new insert must store NULL, not any default value"
        )

    def test_update_omitting_domain_preserves_existing_value(self):
        res = store_memory(
            content="Content for domain preservation test on update path",
            title="Domain Preservation Entity",
            owner_id="tester",
            domain="Business",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._domain_of(entity_id), "Business")

        update_res = store_memory(
            entity_id=entity_id,
            content="Content for domain preservation test on update path",
            title="Domain Preservation Entity",
            owner_id="tester",
            domain=None,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(update_res.startswith("Error"))
        self.assertEqual(
            self._domain_of(entity_id), "Business",
            "omitting domain on an update must preserve the existing DB value, not clear it"
        )

    def test_update_with_explicit_new_domain_changes_stored_value(self):
        res = store_memory(
            content="Content for explicit domain change test on update path",
            title="Domain Change Entity",
            owner_id="tester",
            domain="CADET",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._domain_of(entity_id), "CADET")

        update_res = store_memory(
            entity_id=entity_id,
            content="Content for explicit domain change test on update path",
            title="Domain Change Entity",
            owner_id="tester",
            domain="Homelab",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(update_res.startswith("Error"))
        self.assertEqual(
            self._domain_of(entity_id), "Homelab",
            "an update explicitly supplying a new domain must overwrite the old value"
        )


class TestDomainSearchAndScan(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title, domain=None, is_core=None, tags=None):
        res = store_memory(
            content=f"Seed content for search/scan domain tests: {title}",
            title=title,
            owner_id="tester",
            domain=domain,
            is_core=is_core,
            tags=tags,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"), f"seed store_memory failed: {res}")
        return res.split("ID: ")[1].strip()

    def test_search_domain_filter_returns_only_matching(self):
        saltmdb_id = self._store("SALTMDB Seed Entity Alpha", domain="SALTMDB")
        self._store("CADET Seed Entity Beta", domain="CADET")
        self._store("Business Seed Entity Gamma", domain="Business")

        results = search_memory(
            owner_id="tester",
            domain_filter="SALTMDB",
            db_connection=self.conn,
        )
        self.assertTrue(len(results) > 0)
        ids = {r["id"] for r in results}
        self.assertIn(saltmdb_id, ids)
        for r in results:
            self.assertEqual(r["domain"], "SALTMDB")

    def test_search_domain_filter_composed_with_is_core(self):
        core_cadet_id = self._store("Core CADET Seed", domain="CADET", is_core=True)
        self._store("Non-Core CADET Seed", domain="CADET", is_core=False)
        self._store("Core Business Seed", domain="Business", is_core=True)

        results = search_memory(
            owner_id="tester",
            domain_filter="CADET",
            is_core=True,
            db_connection=self.conn,
        )
        ids = {r["id"] for r in results}
        self.assertIn(core_cadet_id, ids)
        for r in results:
            self.assertEqual(r["domain"], "CADET")
            self.assertTrue(r["is_core"])

    def test_search_memory_results_have_correct_domain_and_other_fields_intact(self):
        entity_id = self._store("Field Integrity Seed Entity", domain="Homelab", is_core=True)

        results = search_memory(
            owner_id="tester",
            domain_filter="Homelab",
            db_connection=self.conn,
        )
        matching = [r for r in results if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        item = matching[0]
        self.assertEqual(item["domain"], "Homelab")
        # Guard against a column-ordinal drift bug corrupting sibling fields.
        self.assertEqual(item["title"], "Field Integrity Seed Entity")
        self.assertTrue(item["is_core"])
        self.assertIn("weight", item)
        self.assertIn("memory_type", item)
        self.assertEqual(item["memory_type"], "fact")

    def test_search_memory_all_items_carry_domain_key_possibly_null(self):
        self._store("Bulk Seed One", domain="General")
        self._store("Bulk Seed Two", domain=None)
        self._store("Bulk Seed Three", domain="SALTMDB")

        results = search_memory(owner_id="tester", db_connection=self.conn)
        self.assertTrue(len(results) >= 3)
        for r in results:
            self.assertIn("domain", r)
        # At least one seeded row legitimately has domain=None; confirm the key isn't
        # forced non-null.
        domains_seen = {r["domain"] for r in results}
        self.assertIn(None, domains_seen)

    def test_scan_memories_includes_correct_domain(self):
        entity_id = self._store("Scan Domain Entity", domain="Business")

        rows = scan_memories(owner_id="tester", db_connection=self.conn)
        matching = [r for r in rows if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["domain"], "Business")

    def test_search_memory_invalid_domain_filter_returns_error(self):
        self._store("Business Entity", domain="Business")
        res = search_memory(
            owner_id="tester",
            domain_filter="business",
            db_connection=self.conn,
        )
        self.assertIsInstance(res, list)
        self.assertEqual(len(res), 1)
        self.assertIn("error", res[0])
        self.assertTrue(res[0]["error"].startswith("Error"))
        self.assertIn("domain must be one of", res[0]["error"])

    def test_search_memory_valid_exact_case_domain_filter_returns_results(self):
        bus_id = self._store("Business Entity Valid Case", domain="Business")
        results = search_memory(
            owner_id="tester",
            domain_filter="Business",
            db_connection=self.conn,
        )
        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)
        ids = {r["id"] for r in results}
        self.assertIn(bus_id, ids)


class TestDomainBackwardCompatAndSchema(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_raw_entity_without_domain(self, entity_id, title="Raw Legacy Entity"):
        now = datetime.now(UTC).isoformat()
        self.conn.execute("""
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope,
                                   is_core, weight, status, parent_ids, title, full_content, context_id)
            VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'raw', ?, ?, ?, ?)
        """, (entity_id, now, now, now, "tester", "shared", "[]", title, f"Full content for {title}", None))
        self.conn.commit()

    def test_raw_insert_without_domain_is_null_at_db_level(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_domain(entity_id)

        row = self.conn.execute(
            "SELECT domain FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertIsNone(
            row[0],
            "domain has no DB-level DEFAULT, so omitting it from the INSERT column list must yield NULL"
        )

    def test_search_and_scan_do_not_error_on_legacy_row(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_domain(entity_id, title="Legacy Row For Search Scan")

        results = search_memory(owner_id="tester", db_connection=self.conn)
        matching = [r for r in results if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        self.assertIsNone(matching[0]["domain"])

        rows = scan_memories(owner_id="tester", db_connection=self.conn)
        matching_scan = [r for r in rows if r["id"] == entity_id]
        self.assertEqual(len(matching_scan), 1)
        self.assertIsNone(matching_scan[0]["domain"])

    def test_no_check_constraint_rejects_bogus_domain_at_db_level_but_service_layer_still_does(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_domain(entity_id, title="No Check Constraint Entity")

        # Unlike memory_type, domain has no CHECK constraint -- a raw SQL UPDATE with a bogus
        # value must succeed without raising sqlite3.IntegrityError.
        try:
            self.conn.execute(
                "UPDATE entities SET domain = 'totally_bogus_value' WHERE id = ?",
                (entity_id,)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.fail("domain column must NOT have a DB-level CHECK constraint")

        row = self.conn.execute(
            "SELECT domain FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(row[0], "totally_bogus_value")

        # Enforcement instead lives at the service layer.
        res = store_memory(
            content="Content proving service-layer domain enforcement",
            title="Service Layer Enforcement Entity",
            owner_id="tester",
            domain="totally_bogus_value",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Error"), f"expected an Error string, got: {res}")

    def test_init_db_second_call_with_existing_domain_column_does_not_raise(self):
        # self.conn already came from an init_db() call in setUp against self.db_path; calling
        # init_db() again on the same path must be idempotent even though the domain column
        # (and its partial index) already exist.
        second_conn = init_db(self.db_path)
        try:
            cols = [r[1] for r in second_conn.execute("PRAGMA table_info(entities)").fetchall()]
            self.assertIn("domain", cols)
        finally:
            second_conn.close()


if __name__ == "__main__":
    unittest.main()
