import os
import shutil
import tempfile
import unittest

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory


class TestLegacyStoreImmutability(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.conn = init_db(os.path.join(self.temp_dir, "legacy-write.db"))
        self.body = "A complete and sufficiently descriptive administrative memory body."

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self):
        result = store_memory(
            title="Immutable Write Test",
            content=self.body,
            tags=["#original"],
            owner_id="legacy-tests",
            db_connection=self.conn,
        )
        self.assertEqual(result["status"], "ok")
        return result["data"]["id"]

    def test_frozen_update_rejects_before_scd_history_or_mutation(self):
        entity_id = self._store()
        before = self.conn.execute(
            "SELECT title, full_content, owner_id, scope, content_hash, valid_from FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        result = store_memory(
            entity_id=entity_id,
            title="Changed Immutable Title",
            content="A complete and sufficiently descriptive changed body.",
            tags=["#changed"],
            owner_id="legacy-tests",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertEqual(
            self.conn.execute(
                "SELECT title, full_content, owner_id, scope, content_hash, valid_from FROM entities WHERE id = ?",
                (entity_id,),
            ).fetchone(),
            before,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE id LIKE ?", (entity_id + "_h_%",)
            ).fetchone()[0],
            0,
        )

    def test_administrative_weight_update_remains_in_place(self):
        entity_id = self._store()
        result = store_memory(
            entity_id=entity_id,
            title="Immutable Write Test",
            content=self.body,
            owner_id="legacy-tests",
            weight=5,
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            self.conn.execute(
                "SELECT weight, status FROM entities WHERE id = ?", (entity_id,)
            ).fetchone(),
            (5, "raw"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE id LIKE ?", (entity_id + "_h_%",)
            ).fetchone()[0],
            0,
        )

    def test_same_title_implicit_upsert_cannot_bypass_frozen_guard(self):
        entity_id = self._store()
        result = store_memory(
            title="Immutable Write Test",
            content="A complete and sufficiently descriptive same-title replacement body.",
            owner_id="legacy-tests",
            db_connection=self.conn,
        )

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["errors"][0]["code"], "IMMUTABLE_MEMORY")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE id LIKE ?", (entity_id + "_h_%",)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT full_content FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()[0],
            self.body,
        )


if __name__ == "__main__":
    unittest.main()
