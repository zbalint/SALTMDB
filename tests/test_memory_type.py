import unittest
import tempfile
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, UTC

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory, search_memory, scan_memories


class TestMemoryTypeStoreMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _memory_type_of(self, entity_id):
        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_all_five_valid_memory_types_round_trip(self):
        for mt in ("fact", "event", "procedure", "decision", "preference"):
            res = store_memory(
                content=f"Content for memory_type round trip test of type {mt}",
                title=f"Memory Type Round Trip {mt}",
                owner_id="tester",
                memory_type=mt,
                skip_duplicate_check=True,
                db_connection=self.conn,
            )
            self.assertFalse(
                res.startswith("Error"), f"store_memory failed for memory_type={mt}: {res}"
            )
            entity_id = res.split("ID: ")[1].strip()
            self.assertEqual(self._memory_type_of(entity_id), mt)

    def test_invalid_memory_type_returns_error_string_not_exception(self):
        res = store_memory(
            content="Content for invalid memory_type test",
            title="Invalid Memory Type Entity",
            owner_id="tester",
            memory_type="bogus_type",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertIsInstance(res, str)
        self.assertTrue(res.startswith("Error:"), f"expected an Error: string, got: {res}")

    def test_omitting_memory_type_on_fresh_insert_defaults_to_fact(self):
        res = store_memory(
            content="Content for default memory_type test on a brand-new entity",
            title="Default Memory Type Entity",
            owner_id="tester",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._memory_type_of(entity_id), "fact")

    def test_update_omitting_memory_type_preserves_existing_value(self):
        res = store_memory(
            content="Content for memory_type preservation test on update path",
            title="Memory Type Preservation Entity",
            owner_id="tester",
            memory_type="decision",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._memory_type_of(entity_id), "decision")

        update_res = store_memory(
            entity_id=entity_id,
            content="Content for memory_type preservation test on update path",
            title="Memory Type Preservation Entity",
            owner_id="tester",
            memory_type=None,
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(update_res.startswith("Error"))
        self.assertEqual(
            self._memory_type_of(entity_id),
            "decision",
            "omitting memory_type on an update must preserve the existing DB value, not reset to 'fact'",
        )

    def test_update_with_explicit_new_memory_type_rejects_frozen_mutation(self):
        res = store_memory(
            content="Content for explicit memory_type change test on update path",
            title="Memory Type Change Entity",
            owner_id="tester",
            memory_type="fact",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertFalse(res.startswith("Error"))
        entity_id = res.split("ID: ")[1].strip()
        self.assertEqual(self._memory_type_of(entity_id), "fact")

        update_res = store_memory(
            entity_id=entity_id,
            content="Content for explicit memory_type change test on update path",
            title="Memory Type Change Entity",
            owner_id="tester",
            memory_type="event",
            skip_duplicate_check=True,
            db_connection=self.conn,
        )
        self.assertEqual(update_res["status"], "rejected")
        self.assertEqual(update_res["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertEqual(
            self._memory_type_of(entity_id),
            "fact",
            "legacy store_memory must not mutate frozen memory_type in place",
        )


class TestMemoryTypeSearchAndScan(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title, memory_type, is_core=None, tags=None):
        core_kwargs = {}
        if is_core:
            core_kwargs = {
                "core_reason": "Test fixture core reason describing a hazard for memory_type coverage.",
                "core_exit_condition": "Test fixture exit condition: the memory_type test tears down its temp DB.",
            }
        res = store_memory(
            content=f"Seed content for search/scan memory_type tests: {title}",
            title=title,
            owner_id="tester",
            memory_type=memory_type,
            is_core=is_core,
            tags=tags,
            skip_duplicate_check=True,
            db_connection=self.conn,
            **core_kwargs,
        )
        self.assertFalse(res.startswith("Error"), f"seed store_memory failed: {res}")
        return res.split("ID: ")[1].strip()

    def test_search_memory_type_filter_returns_only_matching(self):
        proc_id = self._store("Procedure Seed Entity Alpha", "procedure")
        self._store("Fact Seed Entity Beta", "fact")
        self._store("Event Seed Entity Gamma", "event")

        results = search_memory(
            owner_id="tester",
            memory_type_filter="procedure",
            db_connection=self.conn,
        )
        self.assertTrue(len(results) > 0)
        ids = {r["id"] for r in results}
        self.assertIn(proc_id, ids)
        for r in results:
            self.assertEqual(r["memory_type"], "procedure")

    def test_search_memory_type_filter_composed_with_is_core(self):
        core_proc_id = self._store("Core Procedure Seed", "procedure", is_core=True)
        self._store("Non-Core Procedure Seed", "procedure", is_core=False)
        self._store("Core Fact Seed", "fact", is_core=True)

        results = search_memory(
            owner_id="tester",
            memory_type_filter="procedure",
            is_core=True,
            db_connection=self.conn,
        )
        ids = {r["id"] for r in results}
        self.assertIn(core_proc_id, ids)
        for r in results:
            self.assertEqual(r["memory_type"], "procedure")
            self.assertTrue(r["is_core"])

    def test_search_memory_results_have_correct_memory_type_and_other_fields_intact(self):
        entity_id = self._store("Field Integrity Seed Entity", "decision", is_core=True)

        results = search_memory(
            owner_id="tester",
            memory_type_filter="decision",
            db_connection=self.conn,
        )
        matching = [r for r in results if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        item = matching[0]
        self.assertEqual(item["memory_type"], "decision")
        # Guard against a column-ordinal drift bug corrupting sibling fields.
        self.assertEqual(item["title"], "Field Integrity Seed Entity")
        self.assertTrue(item["is_core"])

    def test_search_memory_all_items_carry_memory_type_key(self):
        self._store("Bulk Seed One", "fact")
        self._store("Bulk Seed Two", "event")
        self._store("Bulk Seed Three", "preference")

        results = search_memory(owner_id="tester", db_connection=self.conn)
        self.assertTrue(len(results) >= 3)
        for r in results:
            self.assertIn("memory_type", r)
            self.assertIn(
                r["memory_type"], ("fact", "event", "procedure", "decision", "preference")
            )

    def test_scan_memories_includes_correct_memory_type(self):
        entity_id = self._store("Scan Memory Type Entity", "preference")

        rows = scan_memories(owner_id="tester", db_connection=self.conn)
        matching = [r for r in rows if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["memory_type"], "preference")


class TestMemoryTypeBackwardCompatAndSchema(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_raw_entity_without_memory_type(self, entity_id, title="Raw Legacy Entity"):
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """
            INSERT INTO entities (id, created_at, updated_at, last_accessed_at, owner_id, scope,
                                   is_core, weight, status, parent_ids, title, full_content, context_id)
            VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'raw', ?, ?, ?, ?)
        """,
            (
                entity_id,
                now,
                now,
                now,
                "tester",
                "shared",
                "[]",
                title,
                f"Full content for {title}",
                None,
            ),
        )
        self.conn.commit()

    def test_raw_insert_without_memory_type_defaults_to_fact_at_db_level(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_memory_type(entity_id)

        row = self.conn.execute(
            "SELECT memory_type FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        self.assertEqual(
            row[0],
            "fact",
            "DB-level DEFAULT 'fact' must apply even when memory_type is omitted from the INSERT column list",
        )

    def test_search_and_scan_do_not_error_on_legacy_row(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_memory_type(entity_id, title="Legacy Row For Search Scan")

        results = search_memory(owner_id="tester", db_connection=self.conn)
        matching = [r for r in results if r["id"] == entity_id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["memory_type"], "fact")

        rows = scan_memories(owner_id="tester", db_connection=self.conn)
        matching_scan = [r for r in rows if r["id"] == entity_id]
        self.assertEqual(len(matching_scan), 1)
        self.assertEqual(matching_scan[0]["memory_type"], "fact")

    def test_check_constraint_rejects_invalid_memory_type_at_db_level(self):
        entity_id = str(uuid.uuid4())
        self._insert_raw_entity_without_memory_type(entity_id, title="Check Constraint Entity")

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE entities SET memory_type = 'not_a_real_value' WHERE id = ?", (entity_id,)
            )

    def test_init_db_second_call_with_existing_memory_type_column_does_not_raise(self):
        # self.conn already came from an init_db() call in setUp against self.db_path; calling
        # init_db() again on the same path must be idempotent even though the memory_type
        # column (and its partial index) already exist.
        second_conn = init_db(self.db_path)
        try:
            cols = [r[1] for r in second_conn.execute("PRAGMA table_info(entities)").fetchall()]
            self.assertIn("memory_type", cols)
        finally:
            second_conn.close()


if __name__ == "__main__":
    unittest.main()
