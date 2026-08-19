import unittest
import tempfile
import os
import shutil

from saltmdb.db.schema import init_db
from saltmdb.domain.services.memory_service import store_memory


class TestCoreTagSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _store(self, title, is_core=None, tags=None, entity_id=None):
        core_kwargs = {}
        if is_core:
            # Core-memory governance: creating/promoting a core now requires these lifecycle
            # fields -- supplied here so every #core-tag-sync fixture in this file stays valid.
            core_kwargs = {
                "core_reason": "Test fixture core reason describing a hazard for tag-sync coverage.",
                "core_exit_condition": "Test fixture exit condition: the tag-sync test tears down its temp DB.",
            }
        res = store_memory(
            content=f"Seed content for core-tag-sync tests: {title}",
            title=title,
            owner_id="tester",
            is_core=is_core,
            tags=tags,
            entity_id=entity_id,
            db_connection=self.conn,
            **core_kwargs,
        )
        if isinstance(res, dict):
            self.assertEqual(res.get("status"), "ok", f"seed store_memory failed: {res}")
            return res["data"]["id"]
        self.fail(f"store_memory returned a legacy non-envelope result: {res}")

    def _tags_of(self, entity_id):
        rows = self.conn.execute(
            """
            SELECT t.name FROM entity_tags et JOIN tags t ON t.id = et.tag_id
            WHERE et.entity_id = ?
            """,
            (entity_id,),
        ).fetchall()
        return {r[0] for r in rows}

    def test_is_core_true_adds_core_tag(self):
        entity_id = self._store("Core Sync Add Tag Entity", is_core=True)
        self.assertIn("#core", self._tags_of(entity_id))

    def test_is_core_false_has_no_core_tag(self):
        entity_id = self._store("Core Sync No Tag Entity", is_core=False)
        self.assertNotIn("#core", self._tags_of(entity_id))

    def test_flipping_is_core_to_false_removes_core_tag(self):
        entity_id = self._store("Core Sync Flip Off Entity", is_core=True)
        self.assertIn("#core", self._tags_of(entity_id))

        self._store("Core Sync Flip Off Entity", is_core=False, entity_id=entity_id)
        self.assertNotIn("#core", self._tags_of(entity_id))

    def test_explicit_core_tag_is_overridden_by_false_is_core(self):
        entity_id = self._store("Core Sync Override Entity", is_core=False)
        self.assertNotIn("#core", self._tags_of(entity_id))

        # Caller tries to change frozen tags while leaving is_core untouched. The legacy path
        # must reject the mutation and leave the original tag set unchanged.
        rejected = store_memory(
            content="Seed content for core-tag-sync tests: Core Sync Override Entity",
            title="Core Sync Override Entity",
            tags=["#core", "#foo"],
            entity_id=entity_id,
            owner_id="tester",
            db_connection=self.conn,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["errors"][0]["code"], "IMMUTABLE_MEMORY")
        tags = self._tags_of(entity_id)
        self.assertNotIn("#core", tags)
        self.assertNotIn("#foo", tags)

    def test_is_core_true_self_heals_legacy_entity_with_tags_omitted(self):
        entity_id = self._store("Core Sync Self Heal Entity", is_core=False, tags=["#foo"])
        self.assertNotIn("#core", self._tags_of(entity_id))

        # A later write sets is_core=True but never touches tags at all.
        self._store("Core Sync Self Heal Entity", is_core=True, entity_id=entity_id)
        tags = self._tags_of(entity_id)
        self.assertIn("#core", tags)
        self.assertIn("#foo", tags, "unrelated pre-existing tags must survive the self-heal")


if __name__ == "__main__":
    unittest.main()
